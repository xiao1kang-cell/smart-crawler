"""采集编排 + 任务队列。

队列即 crawl_jobs 表：
  enqueue()     —— 入队一条 pending 任务（scheduler / API 调用）
  claim_job()   —— worker 原子领取最旧 pending 任务
  execute_job() —— 执行已领取的任务：采集 → 清洗入库 → 促销识别 → 收尾
  run_site()    —— 入队 + 立即执行（CLI 同步路径，保持向后兼容）
"""
from __future__ import annotations

import re
import traceback
from datetime import datetime

from sqlalchemy import case, or_, update

from .antiban import BlockedError, in_cooldown, set_cooldown
from .billing import record_usage
from .crawl_diagnostics import (
    classify_exception,
    record_failure,
    zero_products_failure,
)
from .crawlers.registry import get_crawler
from .db import session_scope
from .models import Category, CrawlJob, Product, Promotion, Site

_PROMO_KEYWORDS = re.compile(
    r"\b("
    r"sale|deal|discount|promo|promotion|coupon|clearance|save|off|"
    r"black\s*friday|cyber\s*monday|flash|limited|特价|促销|优惠|折扣|券"
    r")\b",
    re.IGNORECASE,
)

HIGH_PRIORITY_TRIGGERS = ("manual", "admin_quality_rerun", "admin_retry")


def enqueue(site_name: str, trigger: str = "manual",
            requested_by_workspace_id: int | None = None,
            requested_by_user_id: int | None = None) -> int:
    """入队一条采集任务，返回 job_id。"""
    with session_scope() as s:
        if not s.query(Site).filter(Site.site == site_name).first():
            raise ValueError(f"站点不存在: {site_name}")
        job = CrawlJob(site=site_name, status="pending", trigger=trigger,
                       created_at=datetime.utcnow(),
                       requested_by_workspace_id=requested_by_workspace_id,
                       requested_by_user_id=requested_by_user_id)
        s.add(job)
        s.flush()
        return job.id


def claim_job(worker_id: str,
              trigger_allowlist: tuple[str, ...] | None = None) -> int | None:
    """worker 原子领取最旧的 pending 任务，返回 job_id 或 None。"""
    with session_scope() as s:
        priority = case(
            (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), 0),
            (CrawlJob.trigger == "tracking_add", 1),
            else_=2,
        )
        query = s.query(CrawlJob).filter(CrawlJob.status == "pending")
        if trigger_allowlist:
            query = query.filter(CrawlJob.trigger.in_(trigger_allowlist))
        high_priority_touched_at = case(
            (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), CrawlJob.created_at),
            else_=datetime(1970, 1, 1),
        )
        job = query.order_by(priority, high_priority_touched_at.desc(),
                             CrawlJob.id).first()
        if job is None:
            return None
        # 乐观锁：仅当仍为 pending 时领取，防多 worker 抢同一任务
        res = s.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job.id, CrawlJob.status == "pending")
            .values(status="running", worker=worker_id,
                    started_at=datetime.utcnow()))
        return job.id if res.rowcount == 1 else None


def _record_crawl_usage(*, workspace_id, products_count, duration_sec,
                        api_calls, browser_opens) -> None:
    """网页/后台采集：写一行 Usage（api_key_id=None → 只记录，不扣额度）。

    计费失败绝不中断采集收尾。
    """
    try:
        record_usage(
            api_key_id=None,
            workspace_id=workspace_id,
            endpoint="/crawl/job",
            record_count=products_count,
            credits_used=max(1, min(products_count, 10_000)),
            bytes_returned=0,
            duration_ms=int((duration_sec or 0) * 1000),
            api_calls=api_calls,
            browser_opens=browser_opens,
            pages_fetched=api_calls + browser_opens,
        )
    except Exception:
        pass


def execute_job(job_id: int) -> dict:
    """执行一条已领取的任务。"""
    with session_scope() as s:
        job = s.get(CrawlJob, job_id)
        if job is None:
            raise ValueError(f"任务不存在: {job_id}")
        site = s.query(Site).filter(Site.site == job.site).first()
        site_name = job.site
        if job.status != "running":              # CLI 直跑路径：补置 running
            job.status = "running"
            job.started_at = datetime.utcnow()
        crawler = get_crawler(site)
        crawler.job_id = job_id

    # 站点冷却中 —— 跳过，不再去打（反封禁）
    if in_cooldown(site_name):
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "skipped"
            job.finished_at = datetime.utcnow()
            job.error = "站点处于封禁冷却期，本次跳过"
        return {"job_id": job_id, "site": site_name, "status": "skipped"}

    started = datetime.utcnow()
    try:
        result = crawler.crawl()
    except BlockedError as exc:                  # 熔断 —— 站点封锁
        set_cooldown(site_name)
        info = classify_exception(exc)
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "blocked"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"熔断：{exc}（站点已进入冷却期）"
            record_failure(s, site=site_name, job_id=job_id, info=info)
        return {"job_id": job_id, "site": site_name, "status": "blocked",
                "error": str(exc)}
    except Exception as exc:                     # 采集失败 —— C-005
        info = classify_exception(exc)
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
            record_failure(s, site=site_name, job_id=job_id, info=info)
            _fsite = s.query(Site).filter(Site.site == site_name).first()
            if _fsite and _fsite.track_status != "paused":
                _fsite.track_status = "error"
        return {"job_id": job_id, "site": site_name, "status": "failed",
                "error": str(exc)}

    with session_scope() as s:
        from .pipeline import upsert_products
        stats = upsert_products(s, site_name, result.products)
        _save_categories(s, site_name, result.categories)
        s.flush()
        promo_count = _detect_promotions(s, site_name)

        job = s.get(CrawlJob, job_id)
        job.status = "success"
        job.finished_at = datetime.utcnow()
        job.duration_sec = (datetime.utcnow() - started).total_seconds()
        job.products_count = stats["inserted"] + stats["updated"]
        job.new_count = stats["new"]
        job.promotion_count = promo_count
        total = stats["total"] or 1
        job.success_rate = round(
            (stats["inserted"] + stats["updated"]) / total * 100, 1)
        duration = job.duration_sec

        site = s.query(Site).filter(Site.site == site_name).first()
        site.last_crawled = datetime.utcnow()
        site.updated_at = datetime.utcnow()
        produced = stats["inserted"] + stats["updated"]
        if site.track_status != "paused":
            site.track_status = "error" if produced == 0 else "tracking"
        if produced == 0:
            job.status = "failed"
            if job.failure_code:
                job.error = job.failure_detail or "本次抓取未产出有效商品"
            else:
                job.error = "; ".join(result.notes[-3:]) if result.notes else (
                    "本次抓取未产出有效商品")
        if produced == 0 and not job.failure_code:
            info = zero_products_failure(
                site_name,
                "; ".join(result.notes[-3:]) if result.notes else "",
            )
            record_failure(s, site=site_name, job_id=job_id, info=info)
        ws_id = job.requested_by_workspace_id

    counter = getattr(crawler, "counter", None)
    _record_crawl_usage(
        workspace_id=ws_id,
        products_count=stats["inserted"] + stats["updated"],
        duration_sec=duration,
        api_calls=getattr(counter, "api_calls", 0),
        browser_opens=getattr(counter, "browser_opens", 0),
    )
    return {
        "job_id": job_id, "site": site_name, "status": job.status,
        "products": stats["inserted"] + stats["updated"], "new": stats["new"],
        "promotions": promo_count, "notes": result.notes,
        "duration_sec": round(duration, 1),
    }


def run_site(site_name: str) -> dict:
    """入队 + 立即执行（CLI 同步路径）。"""
    job_id = enqueue(site_name)
    return execute_job(job_id)


def run_brand(brand: str) -> list[dict]:
    """采集某品牌全部站点。"""
    with session_scope() as s:
        names = [r.site for r in s.query(Site).filter(Site.brand == brand)]
    return [run_site(n) for n in names]


def _save_categories(s, site_name: str, cats: list[dict]) -> None:
    if not cats:
        return
    s.query(Category).filter(Category.site == site_name).delete()
    for c in cats:
        s.add(Category(**c))


def _detect_promotions(s, site_name: str) -> int:
    """促销识别 —— 价格降价 + 站点标签里的明确促销活动。"""
    s.query(Promotion).filter(Promotion.site == site_name).delete()
    rows = (s.query(Product)
            .filter(Product.site == site_name)
            .filter(or_(
                Product.original_price > Product.sale_price,
                Product.label.isnot(None),
                Product.tags.isnot(None),
            ))
            .all())
    count = 0
    for p in rows:
        has_price_promo = (
            p.original_price is not None and p.sale_price is not None
            and p.original_price > p.sale_price
        )
        label = _promotion_label(p)
        if not has_price_promo and not label:
            continue
        discount = None
        if has_price_promo and p.original_price:
            discount = round((p.original_price - p.sale_price) / p.original_price * 100)
        img = p.image_urls[0] if p.image_urls else None
        s.add(Promotion(
            sku=p.sku, site=site_name,
            promotion_type="price_promotion" if has_price_promo else "site_promotion",
            promotion_name=label, original_price=p.original_price,
            promotion_price=p.sale_price, discount_percent=discount,
            product_title=p.title, product_image=img,
        ))
        count += 1
    return count


def _promotion_label(product: Product) -> str | None:
    values = []
    if product.label:
        values.append(str(product.label))
    tags = product.tags or []
    if isinstance(tags, str):
        values.append(tags)
    elif isinstance(tags, (list, tuple, set)):
        values.extend(str(v) for v in tags if v)
    elif isinstance(tags, dict):
        values.extend(str(v) for v in tags.values() if v)
    for value in values:
        text = value.strip()
        if text and _PROMO_KEYWORDS.search(text):
            return text[:120]
    return None
