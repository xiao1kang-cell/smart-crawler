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
from .pipeline import parse_dt, to_price
from .price_sources import enrich_products_from_site_config

_PROMO_KEYWORDS = re.compile(
    r"\b("
    r"sale|deal|discount|promo|promotion|coupon|clearance|save|off|"
    r"black\s*friday|cyber\s*monday|flash|limited|"
    r"rabatt|aktion|angebot|gutschein|"
    r"remise|soldes|réduction|reduction|"
    r"sconto|offerta|descuento|oferta|cupon|cupón|"
    r"korting|aanbieding|promoção|promocao|desconto|"
    r"rabat|zniżka|znizka|wyprzedaż|wyprzedaz|"
    r"特价|促销|优惠|折扣|券"
    r")\b",
    re.IGNORECASE,
)
_PROMO_ATTR_KEY = re.compile(
    r"(promo|promotion|coupon|discount|deal|sale|offer|badge|label|"
    r"savings|saving)",
    re.IGNORECASE,
)
_PERCENT_DISCOUNT_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:off|discount|save)?", re.I)
_PROMO_NAME_KEYS = (
    "promotion_name", "promo_name", "campaign_name", "offer_name",
    "coupon_name", "deal_name", "sale_name", "badge", "label",
)
_PROMO_TYPE_KEYS = (
    "promotion_type", "promo_type", "offer_type", "coupon_type",
    "deal_type", "discount_type",
)
_PROMO_THRESHOLD_KEYS = (
    "threshold", "promotion_threshold", "promo_threshold",
    "minimum_order", "minimum_order_value", "min_order", "min_spend",
    "condition", "conditions", "coupon_condition", "offer_condition",
)
_PROMO_START_KEYS = (
    "start_time", "starts_at", "start_at", "start_date",
    "promotion_start", "promo_start", "valid_from", "validfrom", "from_date",
)
_PROMO_END_KEYS = (
    "end_time", "ends_at", "end_at", "end_date",
    "promotion_end", "promo_end", "valid_until", "valid_to",
    "valid_through", "validthrough", "to_date",
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
        site = s.query(Site).filter(Site.site == site_name).first()
        products, price_source_stats = enrich_products_from_site_config(
            site, result.products, counter=getattr(crawler, "counter", None))
        if price_source_stats.get("applied"):
            result.notes.append(
                "configured_price_source: "
                f"matched={price_source_stats['matched']}, "
                f"updated={price_source_stats['updated']}, "
                f"rows={price_source_stats['rows']}"
            )
        elif price_source_stats.get("error"):
            result.notes.append(
                f"configured_price_source_failed: {price_source_stats['error']}")
        stats = upsert_products(s, site_name, products)
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
        if produced > 0 and job.failure_code:
            job.status = "partial"
        ws_id = job.requested_by_workspace_id

    counter = getattr(crawler, "counter", None)
    _record_crawl_usage(
        workspace_id=ws_id,
        products_count=stats["inserted"] + stats["updated"],
        duration_sec=duration,
        api_calls=getattr(counter, "api_calls", 0),
        browser_opens=getattr(counter, "browser_opens", 0),
    )
    payload = {
        "job_id": job_id, "site": site_name, "status": job.status,
        "products": stats["inserted"] + stats["updated"], "new": stats["new"],
        "promotions": promo_count, "notes": result.notes,
        "duration_sec": round(duration, 1),
    }
    if job.status != "success":
        payload["error"] = (
            job.failure_detail or job.error or "; ".join(result.notes[-3:])
            or "本次抓取未产出有效商品"
        )
        payload["failure_code"] = job.failure_code
        payload["failure_stage"] = job.failure_stage
        payload["retryable"] = job.retryable
        payload["suggested_action"] = job.suggested_action
    return payload


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
    """促销识别 —— 价格降价 + 站点标签/属性里的明确促销活动。"""
    s.query(Promotion).filter(Promotion.site == site_name).delete()
    rows = (s.query(Product)
            .filter(Product.site == site_name)
            .filter(or_(
                Product.original_price > Product.sale_price,
                Product.label.isnot(None),
                Product.tags.isnot(None),
                Product.attributes.isnot(None),
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
        if discount is None and label:
            discount = _discount_from_text(label)
        meta_entries = _promotion_meta_entries(p)
        if not has_price_promo and not label and not meta_entries:
            continue
        meta_entries = meta_entries or [_promotion_meta(p)]
        img = p.image_urls[0] if p.image_urls else None
        for meta in meta_entries:
            meta_label = meta.get("_label") or label
            meta_original = meta.get("original_price")
            meta_promo_price = meta.get("promotion_price")
            meta_discount = meta.get("discount_percent")
            promo_type = meta.get("promotion_type") or (
                "price_promotion" if has_price_promo
                else _promotion_type_from_text(str(meta_label or ""))
            )
            promo_name = meta.get("promotion_name") or meta_label or promo_type
            s.add(Promotion(
                sku=p.sku, site=site_name,
                promotion_type=promo_type,
                promotion_name=str(promo_name)[:160],
                original_price=meta_original if meta_original is not None else p.original_price,
                promotion_price=meta_promo_price if meta_promo_price is not None else p.sale_price,
                discount_percent=meta_discount if meta_discount is not None else discount,
                threshold=meta.get("threshold"),
                start_time=meta.get("start_time"),
                end_time=meta.get("end_time"),
                product_title=p.title, product_image=img,
            ))
            count += 1
    return count


def _promotion_label(product: Product) -> str | None:
    values = _promotion_values(product)
    for value in values:
        text = value.strip()
        if text and _PROMO_KEYWORDS.search(text):
            return text[:120]
    return None


def _promotion_values(product: Product) -> list[str]:
    values = []
    if product.label:
        values.append(str(product.label))
    tags = product.tags or []
    if isinstance(tags, str):
        values.append(tags)
    elif isinstance(tags, (list, tuple, set)):
        values.extend(str(v) for v in tags if v)
    elif isinstance(tags, dict):
        for key, value in tags.items():
            if _PROMO_ATTR_KEY.search(str(key)) or (
                isinstance(value, str) and _PROMO_KEYWORDS.search(value)
            ):
                values.append(str(value))
    attrs = product.attributes or {}
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            if value in (None, "", [], {}):
                continue
            if _PROMO_ATTR_KEY.search(str(key)):
                if isinstance(value, (list, tuple, set)):
                    values.extend(str(v) for v in value if v not in (None, ""))
                elif isinstance(value, dict):
                    values.extend(str(v) for v in value.values()
                                  if isinstance(v, str) and _PROMO_KEYWORDS.search(v))
                else:
                    values.append(str(value))
            elif isinstance(value, str) and _PROMO_KEYWORDS.search(value):
                values.append(value)
            elif isinstance(value, (list, tuple, set)):
                values.extend(str(v) for v in value
                              if isinstance(v, str) and _PROMO_KEYWORDS.search(v))
    elif isinstance(attrs, str) and _PROMO_KEYWORDS.search(attrs):
        values.append(attrs)
    return values


def _promotion_meta(product: Product) -> dict:
    data = _flatten_promo_sources(product)
    label = _promotion_label(product)
    return _promotion_meta_from_mapping(data, label)


def _promotion_meta_from_mapping(data: dict[str, object],
                                 label: str | None = None) -> dict:
    threshold = _first_meta_value(data, _PROMO_THRESHOLD_KEYS)
    if threshold is None and label:
        threshold = _threshold_from_text(label)
    promo_type = _first_meta_value(data, _PROMO_TYPE_KEYS)
    if promo_type is None and label:
        promo_type = _promotion_type_from_text(label)
    return {
        "promotion_name": _first_meta_value(data, _PROMO_NAME_KEYS),
        "promotion_type": promo_type,
        "threshold": str(threshold).strip()[:160] if threshold not in (None, "") else None,
        "start_time": _first_meta_datetime(data, _PROMO_START_KEYS),
        "end_time": _first_meta_datetime(data, _PROMO_END_KEYS),
        "original_price": _first_meta_price(data, (
            "original_price", "pre_price", "was_price", "high_price",
            "highprice", "regular_price", "list_price", "compare_at_price",
            "msrp", "rrp",
        )),
        "promotion_price": _first_meta_price(data, (
            "promotion_price", "post_price", "sale_price", "price",
            "low_price", "lowprice", "offer_price", "discount_price",
            "final_price", "current_price",
        )),
        "discount_percent": _first_discount_percent(data, label),
        "_label": label,
    }


def _promotion_meta_entries(product: Product) -> list[dict]:
    entries = []
    for item in _explicit_promotion_items(product):
        data = _flatten_mapping(item)
        label = _entry_label(item)
        meta = _promotion_meta_from_mapping(data, label)
        if not meta.get("promotion_name") and label:
            meta["promotion_name"] = label
        if _promotion_meta_has_signal(meta):
            entries.append(meta)
    return entries


def _explicit_promotion_items(product: Product) -> list[dict]:
    items: list[dict] = []

    def scan(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key or "").lower()
                if isinstance(item, list) and (
                    _PROMO_ATTR_KEY.search(key_text)
                    or key_text in {"promotions", "promotion", "offers", "coupons", "deals"}
                ):
                    for child in item:
                        if isinstance(child, dict):
                            items.append(child)
                elif isinstance(item, dict):
                    scan(item)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict) and _entry_label(child):
                    items.append(child)

    scan(product.attributes or {})
    scan(product.tags or {})
    return items


def _promotion_meta_has_signal(meta: dict) -> bool:
    original = meta.get("original_price")
    promo = meta.get("promotion_price")
    if original is not None and promo is not None and original > promo:
        return True
    for key in ("promotion_name", "promotion_type", "threshold",
                "start_time", "end_time", "discount_percent"):
        if meta.get(key) not in (None, "", [], {}):
            return True
    label = str(meta.get("_label") or "")
    return bool(label and _PROMO_KEYWORDS.search(label))


def _entry_label(item: dict) -> str | None:
    for key in (*_PROMO_NAME_KEYS, "name", "title", "label", "text", "description"):
        value = item.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()[:160]
    text = " ".join(str(v) for v in item.values()
                    if isinstance(v, (str, int, float)) and v not in (None, ""))
    return text[:160] if text and _PROMO_KEYWORDS.search(text) else None


def _flatten_promo_sources(product: Product) -> dict[str, object]:
    out: dict[str, object] = {}

    def add_mapping(prefix: str, value) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            key_text = str(key or "").strip().lower()
            if not key_text:
                continue
            out[key_text] = item
            out[f"{prefix}_{key_text}"] = item

    add_mapping("attr", product.attributes or {})
    add_mapping("tag", product.tags or {})
    if product.label:
        out["label"] = product.label
    return out


def _flatten_mapping(value: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, item in value.items():
        key_text = str(key or "").strip().lower()
        if not key_text:
            continue
        full_key = f"{prefix}_{key_text}" if prefix else key_text
        if isinstance(item, dict):
            out.update(_flatten_mapping(item, full_key))
        else:
            out[key_text] = item
            out[full_key] = item
    return out


def _first_meta_value(data: dict[str, object], keys: tuple[str, ...]):
    for key in keys:
        for candidate in (key, f"promo_{key}", f"promotion_{key}", f"coupon_{key}"):
            if candidate in data and data[candidate] not in (None, "", [], {}):
                value = data[candidate]
                if isinstance(value, (list, tuple, set)):
                    value = next((v for v in value if v not in (None, "")), None)
                if isinstance(value, dict):
                    value = value.get("label") or value.get("name") or value.get("value")
                if value not in (None, ""):
                    return value
    return None


def _first_meta_datetime(data: dict[str, object], keys: tuple[str, ...]):
    value = _first_meta_value(data, keys)
    return parse_dt(value)


def _first_meta_price(data: dict[str, object], keys: tuple[str, ...]) -> float | None:
    value = _first_meta_value(data, keys)
    return to_price(value)


def _first_discount_percent(data: dict[str, object], label: str | None = None) -> int | None:
    value = _first_meta_value(data, ("discount_percent", "discount", "saving", "savings"))
    if isinstance(value, (int, float)) and 0 < float(value) < 100:
        return round(float(value))
    text_value = str(value or label or "")
    return _discount_from_text(text_value)


def _promotion_type_from_text(text: str | None) -> str:
    lowered = (text or "").lower()
    if (
        "coupon" in lowered or "code" in lowered or "gutschein" in lowered
        or "cupón" in lowered or "cupon" in lowered or "券" in lowered
    ):
        return "coupon"
    if "bundle" in lowered or "buy" in lowered:
        return "bundle"
    if "clearance" in lowered or "wyprzeda" in lowered or "soldes" in lowered:
        return "clearance"
    if (
        "sale" in lowered or "price" in lowered or "off" in lowered
        or "rabatt" in lowered or "remise" in lowered
        or "réduction" in lowered or "reduction" in lowered
        or "sconto" in lowered or "descuento" in lowered
        or "korting" in lowered or "desconto" in lowered
        or "rabat" in lowered or "zniżka" in lowered or "znizka" in lowered
    ):
        return "price_promotion"
    return "site_promotion"


def _threshold_from_text(text: str | None) -> str | None:
    raw = text or ""
    patterns = (
        r"(?:orders?|order|spend|minimum|min\.?)\s*(?:over|above|of|from|>=)?\s*([$€£¥]?\s*\d[\d\s\u00a0.,]*)",
        r"(?:满|订单满)\s*([$€£¥]?\s*\d[\d\s\u00a0.,]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            amount = match.group(1).strip()
            return f"orders over {amount}"
    return None


def _discount_from_text(text: str) -> int | None:
    match = _PERCENT_DISCOUNT_RE.search(text or "")
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            value = None
        if value is not None and 0 < value < 100:
            return round(value)
    return None
