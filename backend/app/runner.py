"""采集编排 + 任务队列。

队列即 crawl_jobs 表：
  enqueue()     —— 入队一条 pending 任务（scheduler / API 调用）
  claim_job()   —— worker 原子领取最旧 pending 任务
  execute_job() —— 执行已领取的任务：采集 → 清洗入库 → 促销识别 → 收尾
  run_site()    —— 入队 + 立即执行（CLI 同步路径，保持向后兼容）
"""
from __future__ import annotations

import traceback
from datetime import datetime

from sqlalchemy import update

from .antiban import BlockedError, in_cooldown, set_cooldown
from .crawlers.registry import get_crawler
from .db import session_scope
from .models import Category, CrawlJob, Product, Promotion, Site


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


def claim_job(worker_id: str) -> int | None:
    """worker 原子领取最旧的 pending 任务，返回 job_id 或 None。"""
    with session_scope() as s:
        job = (s.query(CrawlJob).filter(CrawlJob.status == "pending")
               .order_by(CrawlJob.id).first())
        if job is None:
            return None
        # 乐观锁：仅当仍为 pending 时领取，防多 worker 抢同一任务
        res = s.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job.id, CrawlJob.status == "pending")
            .values(status="running", worker=worker_id,
                    started_at=datetime.utcnow()))
        return job.id if res.rowcount == 1 else None


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
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "blocked"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"熔断：{exc}（站点已进入冷却期）"
        return {"job_id": job_id, "site": site_name, "status": "blocked",
                "error": str(exc)}
    except Exception as exc:                     # 采集失败 —— C-005
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
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

    return {
        "job_id": job_id, "site": site_name, "status": "success",
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
    """促销识别 —— F1-020：原价 > 售价即判定为价格促销。"""
    s.query(Promotion).filter(Promotion.site == site_name).delete()
    rows = (s.query(Product)
            .filter(Product.site == site_name)
            .filter(Product.original_price > Product.sale_price)
            .all())
    for p in rows:
        discount = None
        if p.original_price:
            discount = round((p.original_price - p.sale_price) / p.original_price * 100)
        img = p.image_urls[0] if p.image_urls else None
        s.add(Promotion(
            sku=p.sku, site=site_name, promotion_type="price_promotion",
            promotion_name=None, original_price=p.original_price,
            promotion_price=p.sale_price, discount_percent=discount,
            product_title=p.title, product_image=img,
        ))
    return len(rows)
