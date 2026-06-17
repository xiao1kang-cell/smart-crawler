"""超管后台 · spine 管理端点(队列/数据集/计费/健康/审计)。

全部经 _require_super_admin。写操作经 audit.record_audit 埋点。
与现有 routes.py 的 /api/admin/* 并列,不碰它们。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import csv
import io
import time
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from .. import spine_queue
from ..audit import record_audit
from ..crawl_diagnostics import job_timeout_failure, record_failure
from ..currency import currency_for_site
from ..db import get_db
from ..price_sources import enrich_products_from_site_config
from ..models import (
    AdminAuditLog,
    ApiKey,
    Category,
    CrawlJob,
    Dataset,
    ExtractedRecord,
    OnDemandJob,
    PriceHistory,
    Product,
    Promotion,
    ProxyEndpoint,
    ProxyHealth,
    ProxyPoolConfig,
    ProxyPoolMember,
    ProxyRule,
    RawSnapshot,
    Review,
    Site,
    SpineJob,
    Trend,
    Usage,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceSite,
)
from ..spine_queue import HEARTBEAT_INTERVAL, _backoff
from .routes import (
    require_user,
    _ANTI_BOT_FAILURE_CODES,
    _build_data_quality_payload,
    _require_super_admin,
)

router = APIRouter(prefix="/api/admin/spine", tags=["admin · spine"])

_STUCK_SEC = 600
_CRAWL_STUCK_SEC = 1800
_ONDEMAND_STUCK_SEC = 1800
_INVENTORY_CACHE_TTL = 30
_INVENTORY_CACHE: dict | None = None
_INVENTORY_CACHE_TS = 0.0
_QUALITY_JOB_ISSUES = {
    "latest_job_failed",
    "partial_crawl",
    "job_in_progress",
    "job_pending_stale",
    "proxy_unavailable",
    "proxy_auth_failed",
    "anti_bot_blocked",
    "empty_sitemap",
    "market_paused",
}
_QUALITY_SITE_ISSUES = {
    "no_products",
    "coverage_low",
    "sku_deviation_high",
    "pdp_price_required",
    "promotions_missing",
    "sales_history_insufficient",
    "never_crawled",
}


def _table_count(db: Session, model) -> int:
    return db.query(func.count(model.id)).scalar() or 0


def _count_by(db: Session, model, col, *, limit: int = 20) -> list[dict]:
    rows = (db.query(col, func.count(model.id))
            .group_by(col)
            .order_by(func.count(model.id).desc())
            .limit(limit)
            .all())
    return [{"key": key if key is not None else "null", "count": int(n or 0)}
            for key, n in rows]


def _weak_product_title_filter():
    title = func.trim(func.coalesce(Product.title, ""))
    return or_(
        func.length(title) == 0,
        func.length(title) < 4,
        func.lower(title).in_(
            ("product", "item", "sku", "untitled", "detail", "details",
             "view product", "shop now")
        ),
        func.lower(title) == func.lower(func.trim(func.coalesce(Product.sku, ""))),
    )


def _product_quality_issues(product: Product) -> list[str]:
    title = (product.title or "").strip()
    sku = (product.sku or "").strip()
    weak_titles = {
        "product", "item", "sku", "untitled", "detail", "details",
        "view product", "shop now",
    }
    issues: list[str] = []
    if (not title or len(title) < 4 or title.lower() in weak_titles or
            (sku and title.lower() == sku.lower())):
        issues.append("title_weak")
    if not ((product.sale_price or 0) > 0 or (product.original_price or 0) > 0):
        issues.append("price_missing")
    expected_currency = currency_for_site(product.site)
    currency = (product.currency or "").strip().upper()
    if expected_currency and not currency:
        issues.append("currency_missing")
    elif expected_currency and currency != expected_currency:
        issues.append("currency_mismatch")
    if not ((product.thirty_day_sales or 0) > 0):
        issues.append("sales_missing")
    if not ((product.thirty_day_revenue or 0) > 0):
        issues.append("revenue_missing")
    return issues


def _payload_sites(payload: dict, db: Session) -> list[str]:
    raw_sites = payload.get("sites")
    if raw_sites is None and payload.get("site"):
        raw_sites = [payload.get("site")]
    if not isinstance(raw_sites, list):
        raise HTTPException(422, {"error": "sites required"})
    sites = []
    for item in raw_sites:
        site = str(item or "").strip()
        if site and site not in sites:
            sites.append(site)
    if not sites:
        raise HTTPException(422, {"error": "sites required"})
    existing_sites = {
        site for (site,) in db.query(Site.site).filter(Site.site.in_(sites)).all()
    }
    missing = [site for site in sites if site not in existing_sites]
    if missing:
        raise HTTPException(404, {"error": "site_not_found", "sites": missing})
    return sites


def _parse_metric_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        raise HTTPException(422, {"error": "date required"})
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        raise HTTPException(422, {"error": "invalid_date", "date": text})


def _metric_number(value, *, percent: bool = False) -> float | int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if percent:
            text = text.rstrip("%").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            raise HTTPException(422, {"error": "invalid_metric", "value": value})
    if percent:
        return round(number, 4)
    return int(round(number))


def _metric_rows_from_payload(payload: dict) -> list[dict]:
    rows = payload.get("rows")
    if rows is None and payload.get("csv"):
        reader = csv.DictReader(io.StringIO(str(payload.get("csv") or "")))
        rows = list(reader)
    if rows is None and any(k in payload for k in ("site", "date", "traffic", "conversion_rate")):
        rows = [payload]
    if not isinstance(rows, list) or not rows:
        raise HTTPException(422, {"error": "rows_or_csv_required"})
    return [r for r in rows if isinstance(r, dict)]


def _metric_template_payload(
    db: Session,
    *,
    tenant: int | None = None,
    include_hidden: bool = False,
    day: date | None = None,
) -> dict:
    day = day or date.today()
    q = (db.query(WorkspaceSite.site, WorkspaceSite.target_sku_count)
         .join(Workspace, Workspace.id == WorkspaceSite.workspace_id)
         .filter(WorkspaceSite.enabled.is_(True), Workspace.status == "active"))
    if tenant is not None:
        q = q.filter(WorkspaceSite.workspace_id == tenant)
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))
    site_codes: list[str] = []
    target_sku_by_site: dict[str, int] = {}
    for site, target_sku_count in q.order_by(WorkspaceSite.site).all():
        if site not in site_codes:
            site_codes.append(site)
        if target_sku_count:
            target_sku_by_site[site] = max(
                int(target_sku_by_site.get(site, 0)),
                int(target_sku_count),
            )
    sites = db.query(Site).filter(Site.site.in_(site_codes)).all() if site_codes else []
    quality = _build_data_quality_payload(db, sites, target_sku_by_site)
    items = []
    for row in quality.get("items") or []:
        issues = set(row.get("issues") or [])
        missing_traffic = "traffic_missing" in issues
        missing_conversion = "conversion_missing" in issues
        if not (missing_traffic or missing_conversion):
            continue
        items.append({
            "site": row.get("site"),
            "date": day.isoformat(),
            "traffic": "",
            "conversion_rate": "",
            "missing_traffic": missing_traffic,
            "missing_conversion": missing_conversion,
            "sku_count": row.get("sku_count") or 0,
            "brand": row.get("brand"),
            "country": row.get("country"),
            "note": "fill traffic/conversion_rate; conversion_rate uses percentage, e.g. 2.5",
        })
    output = io.StringIO()
    fieldnames = ["site", "date", "traffic", "conversion_rate", "note"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for item in items:
        writer.writerow({key: item.get(key, "") for key in fieldnames})
    return {
        "date": day.isoformat(),
        "count": len(items),
        "items": items,
        "csv": output.getvalue(),
        "summary": {
            "missing_traffic": sum(1 for item in items if item["missing_traffic"]),
            "missing_conversion": sum(1 for item in items if item["missing_conversion"]),
        },
    }


def _validate_metric_rows(db: Session, rows: list[dict]) -> dict:
    site_codes = sorted({
        str(row.get("site") or "").strip()
        for row in rows
        if str(row.get("site") or "").strip()
    })
    existing_sites = {
        site for (site,) in db.query(Site.site)
        .filter(Site.site.in_(site_codes)).all()
    } if site_codes else set()
    seen: set[tuple[str, date]] = set()
    valid_rows = []
    errors = []
    created = updated = skipped = 0
    by_site: dict[str, dict] = {}
    for index, raw in enumerate(rows, start=1):
        site = str(raw.get("site") or "").strip()
        row_errors = []
        parsed_day: date | None = None
        traffic = None
        conversion = None
        if not site:
            row_errors.append("site required")
        elif site not in existing_sites:
            row_errors.append("site_not_found")
        try:
            parsed_day = _parse_metric_date(raw.get("date"))
        except HTTPException as exc:
            row_errors.append(str(exc.detail))
        try:
            traffic = _metric_number(raw.get("traffic"))
        except HTTPException as exc:
            row_errors.append(str(exc.detail))
        try:
            conversion = _metric_number(
                raw.get("conversion_rate") or raw.get("conversion") or raw.get("cv_rate"),
                percent=True,
            )
        except HTTPException as exc:
            row_errors.append(str(exc.detail))
        if traffic is None and conversion is None:
            row_errors.append("traffic_or_conversion_required")
        duplicate = False
        if site and parsed_day is not None:
            key = (site, parsed_day)
            duplicate = key in seen
            seen.add(key)
        if row_errors:
            skipped += 1
            errors.append({
                "row": index,
                "site": site or None,
                "date": parsed_day.isoformat() if parsed_day else raw.get("date"),
                "errors": row_errors,
            })
            continue
        assert parsed_day is not None
        exists = (db.query(Trend.id)
                  .filter(Trend.site == site, Trend.date == parsed_day)
                  .first()) is not None
        if exists:
            updated += 1
        else:
            created += 1
        by_site.setdefault(site, {"rows": 0, "created": 0, "updated": 0})
        by_site[site]["rows"] += 1
        by_site[site]["created"] += 0 if exists else 1
        by_site[site]["updated"] += 1 if exists else 0
        valid_rows.append({
            "row": index,
            "site": site,
            "date": parsed_day.isoformat(),
            "traffic": traffic,
            "conversion_rate": conversion,
            "will": "update" if exists else "create",
            "duplicate_in_payload": duplicate,
        })
    return {
        "valid": not errors and bool(valid_rows),
        "rows": len(rows),
        "valid_rows": len(valid_rows),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "sites": sorted(by_site),
        "by_site": by_site,
        "errors": errors,
        "items": valid_rows[:100],
    }


def _trend_seed_for_site_date(db: Session, site: str, day: date) -> dict:
    sku_count = db.query(func.count(Product.id)).filter(Product.site == site).scalar() or 0
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    new_count = (db.query(func.count(Product.id))
                 .filter(Product.site == site,
                         or_(and_(Product.created_time >= start,
                                  Product.created_time < end),
                             and_(Product.published_at >= start,
                                  Product.published_at < end)))
                 .scalar() or 0)
    sales, revenue = db.query(
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
    ).filter(Product.site == site).first()
    avg_rating, review_total = db.query(
        func.avg(Product.ratings),
        func.coalesce(func.sum(Product.review_count), 0),
    ).filter(Product.site == site).first()
    return {
        "sku_count": int(sku_count or 0),
        "new_product_count": int(new_count or 0),
        "estimated_sales": int(sales or 0),
        "estimated_revenue": round(float(revenue or 0), 2),
        "avg_rating": round(float(avg_rating), 2) if avg_rating is not None else None,
        "review_total": int(review_total or 0),
    }


def _group_rows(query, *, limit: int = 20) -> list[dict]:
    rows = query.order_by(func.count().desc()).limit(limit).all()
    return [{"key": key if key is not None else "null", "count": int(n or 0)}
            for key, n in rows]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


def _age_sec(created_at: datetime | None, *, now: datetime) -> int | None:
    return _seconds_between(created_at, now)


def _active_sec(started_at: datetime | None, finished_at: datetime | None,
                *, now: datetime) -> int | None:
    if not started_at:
        return None
    return _seconds_between(started_at, finished_at or now)


def _empty_queue_counts() -> dict:
    return {key: 0 for key in (
        "pending", "running", "success", "failed", "stuck",
        "blocked", "skipped", "partial", "total")}


def _norm_status(source: str, status: str | None, *, stuck: bool = False) -> str:
    if stuck:
        return "stuck"
    raw = (status or "unknown").lower()
    if source == "ondemand" and raw == "queued":
        return "pending"
    if raw in ("pending", "running", "success", "failed", "blocked", "skipped", "partial"):
        return raw
    return raw


def _add_count(bucket: dict, status: str, count: int) -> None:
    bucket[status] = int(bucket.get(status, 0)) + int(count or 0)
    bucket["total"] = int(bucket.get("total", 0)) + int(count or 0)


def _spine_stuck_filter(cutoff):
    return (SpineJob.status == "running",
            or_(SpineJob.heartbeat_at < cutoff, SpineJob.heartbeat_at.is_(None)))


def _crawl_stuck_filter(cutoff):
    return (CrawlJob.status == "running",
            CrawlJob.started_at.isnot(None),
            CrawlJob.started_at < cutoff,
            or_(CrawlJob.heartbeat_at.is_(None),
                CrawlJob.heartbeat_at < cutoff))


def _ondemand_stuck_filter(cutoff):
    return (OnDemandJob.status == "running",
            OnDemandJob.created_at.isnot(None),
            OnDemandJob.created_at < cutoff)


def _queue_stats(db: Session) -> dict:
    now = datetime.utcnow()
    spine_cutoff = now - timedelta(seconds=_STUCK_SEC)
    crawl_cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
    ondemand_cutoff = now - timedelta(seconds=_ONDEMAND_STUCK_SEC)
    by_queue = {
        "spine": _empty_queue_counts(),
        "crawl": _empty_queue_counts(),
        "ondemand": _empty_queue_counts(),
    }

    spine_stuck = (db.query(func.count(SpineJob.id))
                   .filter(*_spine_stuck_filter(spine_cutoff)).scalar() or 0)
    crawl_stuck = (db.query(func.count(CrawlJob.id))
                   .filter(*_crawl_stuck_filter(crawl_cutoff)).scalar() or 0)
    crawl_stale_pending = (
        db.query(func.count(CrawlJob.id))
        .filter(CrawlJob.status == "pending",
                CrawlJob.created_at.isnot(None),
                CrawlJob.created_at < crawl_cutoff)
        .scalar() or 0
    )
    ondemand_stuck = (db.query(func.count(OnDemandJob.id))
                      .filter(*_ondemand_stuck_filter(ondemand_cutoff)).scalar() or 0)

    for status, count in db.query(SpineJob.status, func.count(SpineJob.id)).group_by(SpineJob.status).all():
        status_key = _norm_status("spine", status)
        if status_key == "running":
            count = max(0, int(count or 0) - int(spine_stuck or 0))
        _add_count(by_queue["spine"], status_key, count)
    if spine_stuck:
        _add_count(by_queue["spine"], "stuck", spine_stuck)

    for status, count in db.query(CrawlJob.status, func.count(CrawlJob.id)).group_by(CrawlJob.status).all():
        status_key = _norm_status("crawl", status)
        if status_key == "running":
            count = max(0, int(count or 0) - int(crawl_stuck or 0))
        _add_count(by_queue["crawl"], status_key, count)
    if crawl_stuck:
        _add_count(by_queue["crawl"], "stuck", crawl_stuck)

    for status, count in db.query(OnDemandJob.status, func.count(OnDemandJob.id)).group_by(OnDemandJob.status).all():
        status_key = _norm_status("ondemand", status)
        if status_key == "running":
            count = max(0, int(count or 0) - int(ondemand_stuck or 0))
        _add_count(by_queue["ondemand"], status_key, count)
    if ondemand_stuck:
        _add_count(by_queue["ondemand"], "stuck", ondemand_stuck)

    total = _empty_queue_counts()
    for row in by_queue.values():
        for key, value in row.items():
            total[key] = int(total.get(key, 0)) + int(value or 0)
    by_queue["crawl"]["stale_pending"] = int(crawl_stale_pending or 0)
    total["stale_pending"] = int(crawl_stale_pending or 0)
    status_meta = {
        "spine": {
            "running_raw": int(by_queue["spine"].get("running", 0) or 0) + int(spine_stuck or 0),
            "running_active": int(by_queue["spine"].get("running", 0) or 0),
            "stuck": int(spine_stuck or 0),
            "stale_pending": 0,
        },
        "crawl": {
            "running_raw": int(by_queue["crawl"].get("running", 0) or 0) + int(crawl_stuck or 0),
            "running_active": int(by_queue["crawl"].get("running", 0) or 0),
            "stuck": int(crawl_stuck or 0),
            "pending_raw": int(by_queue["crawl"].get("pending", 0) or 0),
            "stale_pending": int(crawl_stale_pending or 0),
        },
        "ondemand": {
            "running_raw": int(by_queue["ondemand"].get("running", 0) or 0) + int(ondemand_stuck or 0),
            "running_active": int(by_queue["ondemand"].get("running", 0) or 0),
            "stuck": int(ondemand_stuck or 0),
            "stale_pending": 0,
        },
    }
    for queue_name, meta in status_meta.items():
        by_queue[queue_name]["status_meta"] = meta
    total["by_queue"] = by_queue
    total["status_meta"] = {
        "running_raw": sum(int(m.get("running_raw", 0) or 0) for m in status_meta.values()),
        "running_active": sum(int(m.get("running_active", 0) or 0) for m in status_meta.values()),
        "stuck": sum(int(m.get("stuck", 0) or 0) for m in status_meta.values()),
        "stale_pending": int(crawl_stale_pending or 0),
        "by_queue": status_meta,
    }
    total["status_count_note"] = (
        "运行中统计只包含仍在阈值内活跃的任务；超过阈值的 running 会单独归为卡住，"
        "久排是 pending 中排队超过阈值的子集。"
    )
    total["updated_at"] = now.isoformat()
    total["stuck_threshold_sec"] = {
        "spine": _STUCK_SEC,
        "crawl": _CRAWL_STUCK_SEC,
        "ondemand": _ONDEMAND_STUCK_SEC,
    }
    total["breakdowns"] = {
        "crawl_failed_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.status == "failed")
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_running_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.status == "running",
                    or_(CrawlJob.started_at.is_(None),
                        CrawlJob.started_at >= crawl_cutoff,
                        CrawlJob.heartbeat_at >= crawl_cutoff))
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_stuck_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(*_crawl_stuck_filter(crawl_cutoff))
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_stale_pending_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.status == "pending",
                    CrawlJob.created_at.isnot(None),
                    CrawlJob.created_at < crawl_cutoff)
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_blocked_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.status == "blocked")
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_skipped_by_site": _group_rows(
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.status == "skipped")
            .group_by(CrawlJob.site),
            limit=25,
        ),
        "crawl_failure_codes": _group_rows(
            db.query(CrawlJob.failure_code, func.count(CrawlJob.id))
            .filter(CrawlJob.status.in_(("failed", "blocked")))
            .group_by(CrawlJob.failure_code),
            limit=25,
        ),
        "spine_failed_by_dataset": _group_rows(
            db.query(SpineJob.dataset, func.count(SpineJob.id))
            .filter(SpineJob.status == "failed")
            .group_by(SpineJob.dataset),
            limit=25,
        ),
        "spine_running_by_dataset": _group_rows(
            db.query(SpineJob.dataset, func.count(SpineJob.id))
            .filter(SpineJob.status == "running",
                    SpineJob.heartbeat_at.isnot(None),
                    SpineJob.heartbeat_at >= spine_cutoff)
            .group_by(SpineJob.dataset),
            limit=25,
        ),
        "spine_stuck_by_dataset": _group_rows(
            db.query(SpineJob.dataset, func.count(SpineJob.id))
            .filter(*_spine_stuck_filter(spine_cutoff))
            .group_by(SpineJob.dataset),
            limit=25,
        ),
        "ondemand_running_by_platform": _group_rows(
            db.query(OnDemandJob.platform, func.count(OnDemandJob.id))
            .filter(OnDemandJob.status == "running",
                    or_(OnDemandJob.created_at.is_(None),
                        OnDemandJob.created_at >= ondemand_cutoff))
            .group_by(OnDemandJob.platform),
            limit=25,
        ),
        "ondemand_stuck_by_platform": _group_rows(
            db.query(OnDemandJob.platform, func.count(OnDemandJob.id))
            .filter(*_ondemand_stuck_filter(ondemand_cutoff))
            .group_by(OnDemandJob.platform),
            limit=25,
        ),
        "ondemand_failed_by_platform": _group_rows(
            db.query(OnDemandJob.platform, func.count(OnDemandJob.id))
            .filter(OnDemandJob.status == "failed")
            .group_by(OnDemandJob.platform),
            limit=25,
        ),
    }
    return total


def _job_ts(row) -> datetime:
    return row.created_at or row.started_at or row.finished_at or datetime.min


def _spine_job_dict(j: SpineJob, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=_STUCK_SEC)
    is_stuck = (j.status == "running"
                and (j.heartbeat_at is None or j.heartbeat_at < cutoff))
    duration_sec = _seconds_between(j.started_at, j.finished_at)
    return {
        **_job_dict(j),
        "source": "spine",
        "source_label": "通用抓取",
        "raw_status": j.status,
        "normalized_status": _norm_status("spine", j.status, stuck=is_stuck),
        "target": j.dataset or j.url,
        "duration_sec": duration_sec,
        "age_sec": _age_sec(j.created_at, now=now),
        "active_sec": _active_sec(j.started_at, j.finished_at, now=now),
        "retryable": bool(j.status == "failed"
                          and int(j.retries or 0) < int(j.max_retries or 0)),
        "stuck_reason": (
            "heartbeat_missing_or_expired"
            if is_stuck else None
        ),
        "suggested_action": (
            "worker 心跳过期，建议先确认 worker 是否存活；如已终止可重试"
            if is_stuck else None
        ),
    }


def _crawl_job_dict(j: CrawlJob, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
    pending_cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
    is_stuck = (j.status == "running" and j.started_at is not None
                and j.started_at < cutoff
                and (j.heartbeat_at is None or j.heartbeat_at < cutoff))
    is_stale_pending = (
        j.status == "pending" and j.created_at is not None
        and j.created_at < pending_cutoff
    )
    return {
        "id": j.id,
        "source": "crawl",
        "source_label": "站点采集",
        "site": j.site,
        "target": j.site,
        "trigger": j.trigger,
        "url": None,
        "dataset": None,
        "entity_type": "site",
        "status": _norm_status("crawl", j.status, stuck=is_stuck),
        "raw_status": j.status,
        "normalized_status": _norm_status("crawl", j.status, stuck=is_stuck),
        "retries": None,
        "max_retries": None,
        "error": j.failure_detail or j.error,
        "worker": j.worker,
        "result_record_id": None,
        "workspace_id": j.requested_by_workspace_id,
        "api_key_id": None,
        "created_at": _iso(j.created_at),
        "started_at": _iso(j.started_at),
        "finished_at": _iso(j.finished_at),
        "heartbeat_at": _iso(j.heartbeat_at),
        "products_count": j.products_count or 0,
        "new_count": j.new_count or 0,
        "promotion_count": j.promotion_count or 0,
        "duration_sec": j.duration_sec,
        "age_sec": _age_sec(j.created_at, now=now),
        "active_sec": _active_sec(j.started_at, j.finished_at, now=now),
        "attempts": None,
        "failure_code": j.failure_code,
        "failure_stage": j.failure_stage,
        "failure_detail": j.failure_detail,
        "retryable": j.retryable,
        "suggested_action": j.suggested_action,
        "is_stale_pending": is_stale_pending,
        "stuck_reason": "heartbeat_missing_or_expired" if is_stuck else (
            "pending_too_long" if is_stale_pending else None
        ),
    }


def _ondemand_job_dict(j: OnDemandJob, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=_ONDEMAND_STUCK_SEC)
    is_stuck = (j.status == "running" and j.created_at is not None
                and j.created_at < cutoff)
    status = _norm_status("ondemand", j.status, stuck=is_stuck)
    return {
        "id": j.id,
        "source": "ondemand",
        "source_label": "按需抓取",
        "platform": j.platform,
        "site": j.platform,
        "target": j.platform or j.kind or j.url,
        "url": j.url,
        "dataset": j.batch_id,
        "entity_type": j.kind,
        "status": status,
        "raw_status": j.status,
        "normalized_status": status,
        "retries": j.attempts or 0,
        "max_retries": None,
        "error": j.error,
        "worker": None,
        "result_record_id": None,
        "workspace_id": j.workspace_id,
        "api_key_id": None,
        "created_at": _iso(j.created_at),
        "started_at": None,
        "finished_at": _iso(j.finished_at),
        "heartbeat_at": None,
        "listing_count": j.listing_count or 0,
        "review_count": j.review_count or 0,
        "batch_id": j.batch_id,
        "attempts": j.attempts or 0,
        "notes": j.notes,
        "max_items": j.max_items,
        "review_limit": j.review_limit,
        "age_sec": _age_sec(j.created_at, now=now),
        "active_sec": _active_sec(j.created_at, j.finished_at, now=now) if status == "running" else None,
        "duration_sec": _seconds_between(j.created_at, j.finished_at),
        "stuck_reason": "running_timeout" if is_stuck else None,
        "retryable": status in ("failed", "partial"),
        "suggested_action": "可在详情页重试该按需任务" if status in ("failed", "partial") else None,
    }


def _queue_jobs_list(db: Session, *, status: str | None, dataset: str | None,
                     tenant: int | None, source: str, page: int, size: int,
                     failure_code: str | None = None) -> dict:
    source = (source or "all").lower()
    allowed_sources = {"all", "spine", "crawl", "ondemand"}
    if source not in allowed_sources:
        raise HTTPException(422, {"error": "unknown_job_source", "source": source})
    wanted = {s.strip() for s in (status or "").split(",") if s.strip()}
    target = (dataset or "").strip()
    code = (failure_code or "").strip()
    page = max(1, int(page or 1))
    size = max(1, min(200, int(size or 20)))
    now = datetime.utcnow()
    rows: list[dict] = []

    def matches_status(row: dict) -> bool:
        if not wanted:
            return True
        if row.get("normalized_status") in wanted:
            return True
        return "stale_pending" in wanted and bool(row.get("is_stale_pending"))

    if source in ("all", "spine"):
        q = db.query(SpineJob)
        if target:
            needle = f"%{target}%"
            q = q.filter(or_(SpineJob.dataset.ilike(needle),
                             SpineJob.url.ilike(needle),
                             SpineJob.worker.ilike(needle),
                             SpineJob.error.ilike(needle)))
        if tenant is not None:
            q = q.filter(SpineJob.workspace_id == tenant)
        if not code:
            for job in q.order_by(SpineJob.id.desc()).all():
                row = _spine_job_dict(job, now=now)
                if matches_status(row):
                    rows.append(row)

    if source in ("all", "crawl"):
        q = db.query(CrawlJob)
        if target:
            needle = f"%{target}%"
            q = q.filter(or_(CrawlJob.site.ilike(needle),
                             CrawlJob.trigger.ilike(needle),
                             CrawlJob.worker.ilike(needle),
                             CrawlJob.error.ilike(needle),
                             CrawlJob.failure_detail.ilike(needle)))
        if tenant is not None:
            q = q.filter(CrawlJob.requested_by_workspace_id == tenant)
        if code:
            q = q.filter(CrawlJob.failure_code == code)
        for job in q.order_by(CrawlJob.id.desc()).all():
            row = _crawl_job_dict(job, now=now)
            if matches_status(row):
                rows.append(row)

    if source in ("all", "ondemand"):
        q = db.query(OnDemandJob)
        if target:
            needle = f"%{target}%"
            q = q.filter(or_(OnDemandJob.batch_id.ilike(needle),
                             OnDemandJob.platform.ilike(needle),
                             OnDemandJob.url.ilike(needle),
                             OnDemandJob.error.ilike(needle)))
        if tenant is not None:
            q = q.filter(OnDemandJob.workspace_id == tenant)
        if not code:
            for job in q.order_by(OnDemandJob.id.desc()).all():
                row = _ondemand_job_dict(job, now=now)
                if matches_status(row):
                    rows.append(row)

    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    total = len(rows)
    start = max(0, (page - 1) * size)
    end = start + size
    return {"total": total, "items": rows[start:end]}


def _queue_maintenance(db: Session, *, apply: bool = False,
                       sample_limit: int = 20) -> dict:
    """Inspect and optionally repair stale queue rows across queue backends."""
    now = datetime.utcnow()
    spine_cutoff = now - timedelta(seconds=_STUCK_SEC)
    crawl_cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
    ondemand_cutoff = now - timedelta(seconds=_ONDEMAND_STUCK_SEC)
    sample_limit = max(1, min(100, int(sample_limit or 20)))

    spine_stuck = (db.query(SpineJob)
                   .filter(*_spine_stuck_filter(spine_cutoff))
                   .order_by(SpineJob.id)
                   .all())
    crawl_stuck = (db.query(CrawlJob)
                   .filter(*_crawl_stuck_filter(crawl_cutoff))
                   .order_by(CrawlJob.id)
                   .all())
    crawl_stale_pending = (
        db.query(CrawlJob)
        .filter(CrawlJob.status == "pending",
                CrawlJob.created_at.isnot(None),
                CrawlJob.created_at < crawl_cutoff)
        .order_by(CrawlJob.id)
        .all()
    )
    ondemand_stuck = (db.query(OnDemandJob)
                      .filter(*_ondemand_stuck_filter(ondemand_cutoff))
                      .order_by(OnDemandJob.id)
                      .all())

    def sample(rows: list, source: str) -> list[dict]:
        if source == "spine":
            return [_spine_job_dict(row, now=now) for row in rows[:sample_limit]]
        if source == "crawl":
            return [_crawl_job_dict(row, now=now) for row in rows[:sample_limit]]
        return [_ondemand_job_dict(row, now=now) for row in rows[:sample_limit]]

    samples = {
        "spine_stuck": sample(spine_stuck, "spine"),
        "crawl_stuck": sample(crawl_stuck, "crawl"),
        "crawl_stale_pending": sample(crawl_stale_pending, "crawl"),
        "ondemand_stuck": sample(ondemand_stuck, "ondemand"),
    }

    enqueue_ondemand_ids: list[int] = []
    if apply:
        for job in spine_stuck:
            job.status = "pending"
            job.worker = None
            job.next_attempt_at = now
            job.heartbeat_at = None

        for job in crawl_stuck:
            detail = f"admin-canceled: stuck running >{_CRAWL_STUCK_SEC}s"
            job.status = "failed"
            job.finished_at = now
            job.duration_sec = (
                (job.finished_at - job.started_at).total_seconds()
                if job.started_at else None
            )
            job.error = detail
            record_failure(
                db,
                site=job.site,
                job_id=job.id,
                info=job_timeout_failure(job.site, _CRAWL_STUCK_SEC, detail),
            )

        for job in ondemand_stuck:
            job.status = "queued"
            job.finished_at = None
            job.error = "admin requeued stale running job"
            enqueue_ondemand_ids.append(job.id)

    counts = {
        "spine_requeued": len(spine_stuck),
        "crawl_failed_timeout": len(crawl_stuck),
        "ondemand_requeued": len(ondemand_stuck),
        "crawl_stale_pending_observed": len(crawl_stale_pending),
    }
    return {
        "dry_run": not apply,
        "applied": bool(apply),
        "checked_at": now.isoformat(),
        "threshold_sec": {
            "spine": _STUCK_SEC,
            "crawl": _CRAWL_STUCK_SEC,
            "ondemand": _ONDEMAND_STUCK_SEC,
        },
        "counts": counts,
        "total_actionable": (
            counts["spine_requeued"]
            + counts["crawl_failed_timeout"]
            + counts["ondemand_requeued"]
        ),
        "samples": samples,
        "_ondemand_enqueue_ids": enqueue_ondemand_ids,
    }


def _quality_job_issue_filter(issue: str, stale_cutoff: datetime):
    if issue == "latest_job_failed":
        return CrawlJob.status.in_(("failed", "blocked"))
    if issue == "partial_crawl":
        return or_(
            CrawlJob.status == "partial",
            and_(CrawlJob.status == "success",
                 CrawlJob.failure_code.isnot(None),
                 CrawlJob.failure_code != ""),
        )
    if issue == "job_in_progress":
        return CrawlJob.status.in_(("pending", "running"))
    if issue == "job_pending_stale":
        return and_(
            CrawlJob.status == "pending",
            CrawlJob.created_at.isnot(None),
            CrawlJob.created_at < stale_cutoff,
        )
    if issue == "proxy_unavailable":
        return CrawlJob.failure_code == "proxy_unavailable"
    if issue == "proxy_auth_failed":
        return CrawlJob.failure_code == "proxy_auth_failed"
    if issue == "anti_bot_blocked":
        return CrawlJob.failure_code.in_(tuple(_ANTI_BOT_FAILURE_CODES))
    if issue in {"empty_sitemap", "market_paused"}:
        return CrawlJob.failure_code == issue
    return None


@router.get("/jobs/stats")
def jobs_stats(user: str = Depends(require_user),
               db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    return _queue_stats(db)


@router.get("/data-quality")
def admin_data_quality(
    tenant: int | None = None,
    include_hidden: bool = Query(default=False),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """全局站点数据质量明细，供后台定位哪些站点需要重跑。"""
    _require_super_admin(user, db)
    q = (db.query(WorkspaceSite.site, WorkspaceSite.workspace_id, Workspace.name,
                  WorkspaceSite.target_sku_count)
         .join(Workspace, Workspace.id == WorkspaceSite.workspace_id)
         .filter(WorkspaceSite.enabled.is_(True), Workspace.status == "active"))
    if tenant is not None:
        q = q.filter(WorkspaceSite.workspace_id == tenant)
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))

    site_workspace_rows = q.order_by(WorkspaceSite.site, WorkspaceSite.workspace_id).all()
    workspace_by_site: dict[str, list[dict]] = {}
    target_sku_by_site: dict[str, int] = {}
    for site, workspace_id, workspace_name, target_sku_count in site_workspace_rows:
        workspace_payload = {
            "id": workspace_id,
            "name": workspace_name,
        }
        if target_sku_count:
            workspace_payload["target_sku_count"] = target_sku_count
        workspace_by_site.setdefault(site, []).append(workspace_payload)
        if target_sku_count:
            target_sku_by_site[site] = max(
                int(target_sku_by_site.get(site, 0)),
                int(target_sku_count),
            )

    site_codes = sorted(workspace_by_site)
    sites = db.query(Site).filter(Site.site.in_(site_codes)).all() if site_codes else []
    payload = _build_data_quality_payload(db, sites, target_sku_by_site)
    for item in payload["items"]:
        item["workspaces"] = workspace_by_site.get(item["site"], [])
    payload["summary"]["workspace_count"] = len({
        ws["id"] for rows in workspace_by_site.values() for ws in rows
    })
    payload["summary"]["tenant_id"] = tenant
    return payload


@router.get("/data-quality/{site}/products")
def admin_data_quality_products(
    site: str,
    issue: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """某站点问题商品样例，避免验收/排障时只能人工查库。"""
    _require_super_admin(user, db)
    site = (site or "").strip()
    site_row = db.query(Site).filter(Site.site == site).first()
    if not site_row:
        raise HTTPException(404, "site 不存在")
    issue_value = issue if isinstance(issue, str) else None
    limit_value = limit if isinstance(limit, int) else 50
    limit_value = max(1, min(200, limit_value))
    page_value = page if isinstance(page, int) else 1
    page_value = max(1, int(page_value or 1))

    weak_title = _weak_product_title_filter()
    expected_currency = currency_for_site(site)
    currency_value = func.upper(func.trim(func.coalesce(Product.currency, "")))
    issue_filters = {
        "title_weak": weak_title,
        "price_missing": ~(
            (func.coalesce(Product.sale_price, 0) > 0) |
            (func.coalesce(Product.original_price, 0) > 0)
        ),
        "sales_missing": func.coalesce(Product.thirty_day_sales, 0) <= 0,
        "revenue_missing": func.coalesce(Product.thirty_day_revenue, 0) <= 0,
    }
    if expected_currency:
        issue_filters["currency_missing"] = currency_value == ""
        issue_filters["currency_mismatch"] = and_(
            currency_value != "",
            currency_value != expected_currency,
        )
    product_count = int(db.query(func.count(Product.id))
                        .filter(Product.site == site).scalar() or 0)
    trend_count = int(db.query(func.count(Trend.id))
                      .filter(Trend.site == site).scalar() or 0)
    trend_issue_counts = {
        "traffic_missing": int(
            db.query(func.count(Trend.id))
            .filter(Trend.site == site, Trend.traffic.is_(None))
            .scalar() or 0
        ),
        "conversion_missing": int(
            db.query(func.count(Trend.id))
            .filter(Trend.site == site, Trend.conversion_rate.is_(None))
            .scalar() or 0
        ),
    }
    if product_count > 0 and trend_count == 0:
        trend_issue_counts["traffic_missing"] = 1
        trend_issue_counts["conversion_missing"] = 1
    stale_cutoff = datetime.utcnow() - timedelta(seconds=_CRAWL_STUCK_SEC)
    job_issue_counts = {
        key: int(db.query(func.count(CrawlJob.id))
                 .filter(CrawlJob.site == site,
                         _quality_job_issue_filter(key, stale_cutoff))
                 .scalar() or 0)
        for key in _QUALITY_JOB_ISSUES
    }
    target_sku = (
        db.query(func.max(WorkspaceSite.target_sku_count))
        .filter(WorkspaceSite.site == site,
                WorkspaceSite.enabled.is_(True))
        .scalar()
    )
    quality_payload = _build_data_quality_payload(
        db, [site_row], {site: int(target_sku or 0)} if target_sku else None)
    quality_row = (quality_payload.get("items") or [{}])[0]
    site_issue_counts = {
        key: 1 if key in set(quality_row.get("issues") or []) else 0
        for key in _QUALITY_SITE_ISSUES
    }
    issue_counts = {
        key: int(db.query(func.count(Product.id))
                 .filter(Product.site == site, expr)
                 .scalar() or 0)
        for key, expr in issue_filters.items()
    }
    product_issue_total = int(db.query(func.count(Product.id))
                              .filter(Product.site == site,
                                      or_(*issue_filters.values()))
                              .scalar() or 0)
    issue_counts.update(trend_issue_counts)
    issue_counts.update(job_issue_counts)
    issue_counts.update(site_issue_counts)

    if issue_value in _QUALITY_SITE_ISSUES:
        issues = set(quality_row.get("issues") or [])
        item = {
            "id": f"site-{site}",
            "kind": "site",
            "site": site,
            "brand": quality_row.get("brand"),
            "country": quality_row.get("country"),
            "url": quality_row.get("url"),
            "sku_count": quality_row.get("sku_count"),
            "spu_count": quality_row.get("spu_count"),
            "fetched_count": quality_row.get("fetched_count"),
            "estimated_full": quality_row.get("estimated_full"),
            "target_sku_count": quality_row.get("target_sku_count"),
            "target_sku_source": quality_row.get("target_sku_source"),
            "sku_deviation_abs": quality_row.get("sku_deviation_abs"),
            "sku_deviation_pct": quality_row.get("sku_deviation_pct"),
            "coverage_pct": quality_row.get("coverage_pct"),
            "promotion_count": quality_row.get("promotion_count"),
            "price_source_configured": quality_row.get("price_source_configured"),
            "price_source_type": quality_row.get("price_source_type"),
            "price_source": quality_row.get("price_source"),
            "last_crawled": quality_row.get("last_crawled"),
            "last_product_updated": quality_row.get("last_product_updated"),
            "latest_job": quality_row.get("latest_job"),
            "suggested_action": quality_row.get("suggested_action"),
            "issues": quality_row.get("issues") or [],
            "rerun_recommended": quality_row.get("rerun_recommended"),
            "rerun_ready": quality_row.get("rerun_ready"),
            "rerun_after_setup": quality_row.get("rerun_after_setup"),
            "rerun_blocked": quality_row.get("rerun_blocked"),
            "rerun_preconditions": quality_row.get("rerun_preconditions") or [],
            "external_data_required": quality_row.get("external_data_required"),
            "latest_failure": quality_row.get("latest_failure"),
            "last_error": quality_row.get("last_error"),
            "last_error_code": quality_row.get("last_error_code"),
        }
        return {
            "site": site,
            "issue": issue_value,
            "kind": "site",
            "limit": limit_value,
            "page": page_value,
            "page_size": limit_value,
            "total": 1 if issue_value in issues else 0,
            "issue_counts": {
                "all": product_issue_total,
                **issue_counts,
            },
            "items": [item] if issue_value in issues and page_value == 1 else [],
        }

    if issue_value in _QUALITY_JOB_ISSUES:
        issue_filter = _quality_job_issue_filter(issue_value, stale_cutoff)
        if issue_filter is None:
            raise HTTPException(422, "未知任务问题类型")
        q = db.query(CrawlJob).filter(CrawlJob.site == site, issue_filter)
        total = q.count()
        rows = (q.order_by(CrawlJob.created_at.desc().nullslast(),
                           CrawlJob.id.desc())
                .offset((page_value - 1) * limit_value)
                .limit(limit_value)
                .all())
        return {
            "site": site,
            "issue": issue_value,
            "kind": "job",
            "limit": limit_value,
            "page": page_value,
            "page_size": limit_value,
            "total": total,
            "issue_counts": {
                "all": product_issue_total,
                **issue_counts,
            },
            "items": [_crawl_job_dict(row) for row in rows],
        }

    if issue_value in {"traffic_missing", "conversion_missing"}:
        col = Trend.traffic if issue_value == "traffic_missing" else Trend.conversion_rate
        trend_q = db.query(Trend).filter(Trend.site == site, col.is_(None))
        total = trend_q.count()
        rows = (trend_q.order_by(Trend.date.desc(), Trend.id.desc())
                .offset((page_value - 1) * limit_value)
                .limit(limit_value)
                .all())
        items = [{
            "id": f"trend-{row.id}",
            "kind": "trend",
            "site": row.site,
            "date": row.date.isoformat() if row.date else None,
            "sku_count": row.sku_count,
            "new_product_count": row.new_product_count,
            "estimated_sales": row.estimated_sales,
            "estimated_revenue": row.estimated_revenue,
            "traffic": row.traffic,
            "conversion_rate": row.conversion_rate,
            "issues": [
                key for key, missing in (
                    ("traffic_missing", row.traffic is None),
                    ("conversion_missing", row.conversion_rate is None),
                ) if missing
            ],
        } for row in rows]
        if product_count > 0 and trend_count == 0 and page_value == 1:
            total = 1
            items = [{
                "id": f"trend-missing-{site}",
                "kind": "trend",
                "site": site,
                "date": None,
                "sku_count": product_count,
                "new_product_count": None,
                "estimated_sales": None,
                "estimated_revenue": None,
                "traffic": None,
                "conversion_rate": None,
                "issues": ["traffic_missing", "conversion_missing"],
                "note": "该站点暂无趋势/第三方信号行",
            }]
        return {
            "site": site,
            "issue": issue_value,
            "kind": "trend",
            "limit": limit_value,
            "page": page_value,
            "page_size": limit_value,
            "total": total,
            "issue_counts": {
                "all": product_issue_total,
                **issue_counts,
            },
            "items": items,
        }

    q = db.query(Product).filter(Product.site == site)
    if issue_value:
        if issue_value not in issue_filters:
            raise HTTPException(
                422,
                "issue 必须是 title_weak/price_missing/currency_missing/"
                "currency_mismatch/sales_missing/revenue_missing/"
                "sales_history_insufficient/traffic_missing/conversion_missing/"
                "latest_job_failed/partial_crawl/"
                "job_in_progress/job_pending_stale/proxy_unavailable/"
                "proxy_auth_failed/anti_bot_blocked/empty_sitemap/market_paused/"
                "no_products/coverage_low/sku_deviation_high/"
                "promotions_missing/pdp_price_required/never_crawled",
            )
        q = q.filter(issue_filters[issue_value])
    else:
        q = q.filter(or_(*issue_filters.values()))

    total = q.count()
    rows = (q.order_by(Product.updated_time.desc(), Product.id.desc())
            .offset((page_value - 1) * limit_value)
            .limit(limit_value)
            .all())
    return {
        "site": site,
        "issue": issue_value or "all",
        "kind": "product",
        "limit": limit_value,
        "page": page_value,
        "page_size": limit_value,
        "total": total,
        "issue_counts": {
            "all": product_issue_total,
            **issue_counts,
        },
        "items": [{
            "id": row.id,
            "site": row.site,
            "brand": row.brand,
            "sku": row.sku,
            "spu": row.spu,
            "title": row.title,
            "product_url": row.product_url,
            "image": (row.image_urls or [None])[0],
            "category_path": row.category_path,
            "status": row.status,
            "sale_price": row.sale_price,
            "original_price": row.original_price,
            "currency": row.currency,
            "expected_currency": currency_for_site(row.site),
            "thirty_day_sales": row.thirty_day_sales,
            "thirty_day_revenue": row.thirty_day_revenue,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "created_time": row.created_time.isoformat() if row.created_time else None,
            "updated_time": row.updated_time.isoformat() if row.updated_time else None,
            "latest_job": quality_row.get("latest_job"),
            "suggested_action": quality_row.get("suggested_action"),
            "issues": _product_quality_issues(row),
        } for row in rows],
    }


@router.post("/crawl/enqueue")
def admin_crawl_enqueue(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """后台按站点触发采集；复用已有 pending/running，避免重复入队。"""
    actor = _require_super_admin(user, db)
    sites = _payload_sites(payload, db)

    from ..runner import HIGH_PRIORITY_TRIGGERS, enqueue as enqueue_crawl

    jobs: list[int] = []
    created: list[int] = []
    reused: list[int] = []
    promoted: list[int] = []
    by_site: dict[str, dict] = {}
    for site in sites:
        running = (db.query(CrawlJob)
                   .filter(CrawlJob.site == site, CrawlJob.status == "running")
                   .order_by(CrawlJob.id.desc())
                   .first())
        if running:
            jobs.append(running.id)
            reused.append(running.id)
            by_site[site] = {"job_id": running.id, "status": "already_running"}
            continue
        pending = (db.query(CrawlJob)
                   .filter(CrawlJob.site == site, CrawlJob.status == "pending")
                   .order_by(CrawlJob.id.desc())
                   .first())
        if pending:
            jobs.append(pending.id)
            if pending.trigger in HIGH_PRIORITY_TRIGGERS:
                reused.append(pending.id)
                by_site[site] = {"job_id": pending.id, "status": "already_queued"}
            else:
                pending.trigger = "admin_quality_rerun"
                pending.requested_by_user_id = actor.id
                pending.created_at = datetime.utcnow()
                promoted.append(pending.id)
                by_site[site] = {"job_id": pending.id, "status": "promoted"}
            continue
        job_id = enqueue_crawl(site, trigger="admin_quality_rerun",
                               requested_by_user_id=actor.id)
        jobs.append(job_id)
        created.append(job_id)
        created_job = db.get(CrawlJob, job_id)
        if created_job and created_job.status == "skipped":
            by_site[site] = {
                "job_id": job_id,
                "status": "skipped_precondition",
                "failure_code": created_job.failure_code,
                "suggested_action": created_job.suggested_action,
            }
        else:
            by_site[site] = {"job_id": job_id, "status": "queued"}

    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="crawl.enqueue", target_type="site",
                 target_id=",".join(sites),
                 detail={"created_jobs": created, "existing_jobs": reused,
                         "promoted_jobs": promoted},
                 ip=ip or None)
    db.commit()
    skipped_precondition = [
        row["job_id"] for row in by_site.values()
        if row.get("status") == "skipped_precondition"
    ]
    return {
        "status": "skipped_precondition" if skipped_precondition and len(skipped_precondition) == len(jobs) else (
            "queued" if created and not reused and not promoted and not skipped_precondition else (
            "already_running" if reused and not created and not promoted else "mixed"
        )),
        "jobs": jobs,
        "created_jobs": created,
        "existing_jobs": reused,
        "promoted_jobs": promoted,
        "skipped_precondition_jobs": skipped_precondition,
        "by_site": by_site,
        "count": len(jobs),
        "queued_at": datetime.utcnow().isoformat(),
    }


@router.post("/promotions/rebuild")
def admin_promotions_rebuild(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """按已有商品重新识别促销；用于修复历史数据，不必先全站重抓。"""
    actor = _require_super_admin(user, db)
    sites = _payload_sites(payload, db)
    from ..runner import _detect_promotions

    by_site: dict[str, dict] = {}
    total_created = 0
    for site in sites:
        before = db.query(func.count(Promotion.id)).filter(
            Promotion.site == site).scalar() or 0
        created = _detect_promotions(db, site)
        db.flush()
        after = db.query(func.count(Promotion.id)).filter(
            Promotion.site == site).scalar() or 0
        total_created += int(created or 0)
        by_site[site] = {
            "before": int(before),
            "after": int(after),
            "created": int(created or 0),
            "delta": int(after) - int(before),
        }

    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="promotions.rebuild", target_type="site",
                 target_id=",".join(sites),
                 detail={"sites": sites, "by_site": by_site},
                 ip=ip or None)
    db.commit()
    return {
        "status": "rebuilt",
        "sites": sites,
        "count": len(sites),
        "created": total_created,
        "by_site": by_site,
        "rebuilt_at": datetime.utcnow().isoformat(),
    }


@router.post("/analytics/recompute")
def admin_analytics_recompute(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """按已有快照重算销量/收入/趋势；不触发网页抓取。"""
    actor = _require_super_admin(user, db)
    sites = _payload_sites(payload, db)
    from ..analytics import recompute_site

    by_site: dict[str, dict] = {}
    totals = {
        "estimated_skus": 0,
        "estimated_sales": 0,
        "estimated_revenue": 0.0,
        "insufficient_history_skus": 0,
        "trend_days": 0,
    }
    for site in sites:
        result = recompute_site(db, site)
        by_site[site] = result
        for key in ("estimated_skus", "estimated_sales",
                    "insufficient_history_skus", "trend_days"):
            totals[key] += int(result.get(key) or 0)
        totals["estimated_revenue"] += float(result.get("estimated_revenue") or 0)

    totals["estimated_revenue"] = round(totals["estimated_revenue"], 2)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="analytics.recompute", target_type="site",
                 target_id=",".join(sites),
                 detail={"sites": sites, "by_site": by_site, "totals": totals},
                 ip=ip or None)
    db.commit()
    return {
        "status": "recomputed",
        "sites": sites,
        "count": len(sites),
        "totals": totals,
        "by_site": by_site,
        "recomputed_at": datetime.utcnow().isoformat(),
    }


@router.post("/third-party-metrics/import")
def admin_third_party_metrics_import(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """导入站点级第三方流量/转化率。

    支持 payload.rows=[{site,date,traffic,conversion_rate}] 或 csv 文本。
    date 为 YYYY-MM-DD；conversion_rate 按页面展示口径存储，如 2.5 表示 2.5%。
    """
    actor = _require_super_admin(user, db)
    rows = _metric_rows_from_payload(payload)
    validation = _validate_metric_rows(db, rows)
    if validation["errors"] or not validation["valid_rows"]:
        raise HTTPException(422, {"error": "invalid_metrics", **validation})
    site_codes = sorted({
        str(row.get("site") or payload.get("site") or "").strip()
        for row in rows
    })
    if not site_codes or any(not site for site in site_codes):
        raise HTTPException(422, {"error": "site required"})
    existing_sites = {
        site for (site,) in db.query(Site.site)
        .filter(Site.site.in_(site_codes)).all()
    }
    missing = [site for site in site_codes if site not in existing_sites]
    if missing:
        raise HTTPException(404, {"error": "site_not_found", "sites": missing})

    by_site: dict[str, dict] = {}
    trend_cache: dict[tuple[str, date], tuple[Trend, bool]] = {}
    imported = 0
    updated = 0
    created = 0
    for raw in rows:
        site = str(raw.get("site") or payload.get("site") or "").strip()
        day = _parse_metric_date(raw.get("date") or payload.get("date"))
        traffic = _metric_number(raw.get("traffic"))
        conversion = _metric_number(
            raw.get("conversion_rate") or raw.get("conversion") or raw.get("cv_rate"),
            percent=True,
        )
        if traffic is None and conversion is None:
            continue
        cache_key = (site, day)
        if cache_key in trend_cache:
            trend, is_new = trend_cache[cache_key]
            row_created = False
            row_updated = not is_new
        else:
            trend = (db.query(Trend)
                     .filter(Trend.site == site, Trend.date == day)
                     .first())
            is_new = trend is None
            row_created = is_new
            row_updated = not is_new
            if trend is None:
                trend = Trend(site=site, date=day,
                              **_trend_seed_for_site_date(db, site, day))
                db.add(trend)
                created += 1
            else:
                updated += 1
            trend_cache[cache_key] = (trend, is_new)
        if traffic is not None:
            trend.traffic = int(traffic)
        if conversion is not None:
            trend.conversion_rate = float(conversion)
        imported += 1
        by_site.setdefault(site, {"rows": 0, "created": 0, "updated": 0})
        by_site[site]["rows"] += 1
        by_site[site]["created"] += 1 if row_created else 0
        by_site[site]["updated"] += 1 if row_updated else 0

    if imported == 0:
        raise HTTPException(422, {"error": "no_metrics_to_import"})
    db.flush()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="third_party_metrics.import", target_type="site",
                 target_id=",".join(site_codes),
                 detail={"sites": site_codes, "rows": imported,
                         "created": created, "updated": updated},
                 ip=ip or None)
    db.commit()
    return {
        "status": "imported",
        "rows": imported,
        "created": created,
        "updated": updated,
        "sites": site_codes,
        "by_site": by_site,
        "imported_at": datetime.utcnow().isoformat(),
    }


@router.get("/third-party-metrics/template")
def admin_third_party_metrics_template(
    tenant: int | None = None,
    include_hidden: bool = Query(default=False),
    date_value: str | None = Query(default=None, alias="date"),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """按当前数据质量缺口生成第三方指标导入模板。"""
    _require_super_admin(user, db)
    date_text = date_value if isinstance(date_value, str) else None
    day = _parse_metric_date(date_text) if date_text else date.today()
    return _metric_template_payload(
        db, tenant=tenant, include_hidden=include_hidden, day=day)


@router.post("/third-party-metrics/validate")
def admin_third_party_metrics_validate(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """预校验第三方指标导入内容，不写库。"""
    _require_super_admin(user, db)
    rows = _metric_rows_from_payload(payload)
    return _validate_metric_rows(db, rows)


def _job_dict(j: SpineJob) -> dict:
    return {"id": j.id, "url": j.url, "dataset": j.dataset,
            "entity_type": j.entity_type, "status": j.status,
            "retries": j.retries, "max_retries": j.max_retries,
            "error": j.error, "worker": j.worker,
            "result_record_id": j.result_record_id,
            "workspace_id": j.workspace_id, "api_key_id": j.api_key_id,
            "created_at": _iso(j.created_at),
            "started_at": _iso(j.started_at),
            "finished_at": _iso(j.finished_at),
            "heartbeat_at": _iso(j.heartbeat_at)}


@router.get("/jobs")
def jobs_list(status: str | None = None, dataset: str | None = None,
              tenant: int | None = None, source: str = "all",
              page: int = 1, size: int = 20,
              failure_code: str | None = None,
              user: str = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    return _queue_jobs_list(db, status=status, dataset=dataset,
                            tenant=tenant, source=source,
                            page=page, size=size,
                            failure_code=failure_code)


@router.post("/jobs/maintenance")
def jobs_maintenance(payload: dict | None = None,
                     user: str = Depends(require_user),
                     db: Session = Depends(get_db),
                     ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    payload = payload or {}
    apply = bool(payload.get("apply") or payload.get("execute"))
    sample_limit = int(payload.get("sample_limit") or 20)
    out = _queue_maintenance(db, apply=apply, sample_limit=sample_limit)
    enqueue_ids = out.pop("_ondemand_enqueue_ids", [])
    if apply:
        record_audit(
            db,
            actor_user_id=actor.id,
            actor_name=actor.username,
            action="job.maintenance",
            target_type="queue",
            target_id="all",
            detail={"counts": out["counts"], "total_actionable": out["total_actionable"]},
            ip=ip or None,
        )
        db.commit()
        if enqueue_ids:
            from ..ondemand.queue import enqueue as enqueue_ondemand

            for job_id in enqueue_ids:
                enqueue_ondemand(job_id)
    return out


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, source: str = "spine",
               user: str = Depends(require_user),
               db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    source = (source or "spine").lower()
    if source == "crawl":
        j = db.get(CrawlJob, job_id)
        if j is None:
            raise HTTPException(404, {"error": "job_not_found", "job_id": job_id,
                                      "source": source})
        return _crawl_job_dict(j)
    if source == "ondemand":
        j = db.get(OnDemandJob, job_id)
        if j is None:
            raise HTTPException(404, {"error": "job_not_found", "job_id": job_id,
                                      "source": source})
        return _ondemand_job_dict(j)
    if source != "spine":
        raise HTTPException(422, {"error": "unknown_job_source", "source": source})
    j = db.get(SpineJob, job_id)
    if j is None:
        raise HTTPException(404, {"error": "job_not_found", "job_id": job_id})
    return _spine_job_dict(j)


@router.post("/jobs/{job_id}/retry")
def job_retry(job_id: int, source: str = "spine",
              user: str = Depends(require_user),
              db: Session = Depends(get_db),
              ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    source = (source or "spine").lower()
    if source == "crawl":
        from ..runner import enqueue as enqueue_crawl

        j = db.get(CrawlJob, job_id)
        if j is None:
            raise HTTPException(404, {"error": "job_not_found", "job_id": job_id,
                                      "source": source})
        now = datetime.utcnow()
        crawl_cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
        is_stuck = (j.status == "running" and j.started_at is not None
                    and j.started_at < crawl_cutoff
                    and (j.heartbeat_at is None or j.heartbeat_at < crawl_cutoff))
        is_stale_pending = (
            j.status == "pending" and j.created_at is not None
            and j.created_at < crawl_cutoff
        )
        if j.status in ("pending", "running") and not (is_stuck or is_stale_pending):
            raise HTTPException(409, {"error": "job_not_retryable",
                                      "status": j.status})
        new_id = enqueue_crawl(j.site, trigger="admin_retry",
                               requested_by_workspace_id=j.requested_by_workspace_id,
                               requested_by_user_id=actor.id)
        record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                     action="job.retry", target_type="crawl_job",
                     target_id=str(job_id), detail={"new_job_id": new_id},
                     ip=ip or None)
        db.commit()
        return {"job_id": new_id, "source": "crawl", "status": "pending",
                "retried_from": job_id}
    if source == "ondemand":
        from ..ondemand.queue import enqueue as enqueue_ondemand

        j = db.get(OnDemandJob, job_id)
        if j is None:
            raise HTTPException(404, {"error": "job_not_found", "job_id": job_id,
                                      "source": source})
        now = datetime.utcnow()
        is_stuck = (
            j.status == "running" and j.created_at is not None
            and j.created_at < now - timedelta(seconds=_ONDEMAND_STUCK_SEC)
        )
        if j.status not in ("success", "partial", "failed") and not is_stuck:
            raise HTTPException(409, {"error": "job_not_retryable",
                                      "status": j.status})
        prev = j.status
        j.status = "queued"
        j.error = None
        j.finished_at = None
        record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                     action="job.retry", target_type="ondemand_job",
                     target_id=str(job_id), detail={"from": prev, "to": "queued"},
                     ip=ip or None)
        db.commit()
        enqueue_ondemand(job_id)
        return {"job_id": job_id, "source": "ondemand", "status": "queued"}
    if source != "spine":
        raise HTTPException(422, {"error": "unknown_job_source", "source": source})
    j = db.get(SpineJob, job_id)
    if j is None:
        raise HTTPException(404, {"error": "job_not_found", "job_id": job_id})
    prev_error = j.error
    j.status = "pending"
    j.worker = None
    j.next_attempt_at = datetime.utcnow()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="job.retry", target_type="job", target_id=str(job_id),
                 detail={"prev_error": prev_error}, ip=ip or None)
    db.commit()
    return {"job_id": job_id, "status": "pending"}


@router.post("/jobs/enqueue")
def job_enqueue(payload: dict, user: str = Depends(require_user),
                db: Session = Depends(get_db),
                ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    url = payload.get("url")
    dataset = payload.get("dataset")
    if not url or not dataset:
        raise HTTPException(422, {"error": "url and dataset required"})
    job_id = spine_queue.enqueue(
        db, url, dataset, entity_type=payload.get("entity_type", "generic"),
        save_policy=payload.get("save_policy", "promote_if_valid"),
        workspace_id=None)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="job.enqueue", target_type="job", target_id=str(job_id),
                 detail={"url": url, "dataset": dataset}, ip=ip or None)
    db.commit()
    return {"job_id": job_id, "status": "pending"}


@router.get("/datasets")
def datasets_list(user: str = Depends(require_user),
                  db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    rows = db.query(Dataset).order_by(Dataset.id.desc()).all()
    items = []
    for d in rows:
        n = db.query(ExtractedRecord).filter(ExtractedRecord.dataset_id == d.id).count()
        items.append({"id": d.id, "name": d.name, "slug": d.slug,
                      "entity_type": d.entity_type, "record_count": n,
                      "workspace_id": d.workspace_id})
    return {"items": items, "total": len(items)}


@router.get("/datasets/{dataset_id}/records")
def dataset_records(dataset_id: int, quality_status: str | None = None,
                    page: int = 1, size: int = 20,
                    user: str = Depends(require_user),
                    db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = db.query(ExtractedRecord).filter(ExtractedRecord.dataset_id == dataset_id)
    if quality_status:
        q = q.filter(ExtractedRecord.quality_status == quality_status)
    total = q.count()
    rows = (q.order_by(ExtractedRecord.id.desc())
            .offset((page - 1) * size).limit(size).all())
    return {"total": total, "items": [
        {"id": r.id, "source_url": r.source_url, "entity_type": r.entity_type,
         "quality_status": r.quality_status, "confidence": r.confidence,
         "data": r.data,
         "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None}
        for r in rows]}


@router.get("/records/{record_id}")
def record_detail(record_id: int, user: str = Depends(require_user),
                  db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    r = db.get(ExtractedRecord, record_id)
    if r is None:
        raise HTTPException(404, {"error": "record_not_found", "record_id": record_id})
    snap = db.get(RawSnapshot, r.snapshot_id) if r.snapshot_id else None
    return {
        "id": r.id, "data": r.data, "entity_type": r.entity_type,
        "quality_status": r.quality_status, "confidence": r.confidence,
        "provenance": {"source_url": r.source_url, "canonical_url": r.canonical_url,
                       "content_hash": r.content_hash,
                       "extraction_method": r.extraction_method,
                       "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None},
        "snapshot": ({"id": snap.id, "url": snap.url,
                      "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None}
                     if snap else None),
    }


@router.post("/records/{record_id}/promote")
def record_promote(record_id: int, user: str = Depends(require_user),
                   db: Session = Depends(get_db),
                   ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    r = db.get(ExtractedRecord, record_id)
    if r is None:
        raise HTTPException(404, {"error": "record_not_found", "record_id": record_id})
    prev = r.quality_status
    r.quality_status = "main"
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="record.promote", target_type="record",
                 target_id=str(record_id), detail={"from": prev, "to": "main"},
                 ip=ip or None)
    db.commit()
    return {"record_id": record_id, "quality_status": "main"}


@router.delete("/records/{record_id}")
def record_delete(record_id: int, user: str = Depends(require_user),
                  db: Session = Depends(get_db),
                  ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    r = db.get(ExtractedRecord, record_id)
    if r is None:
        raise HTTPException(404, {"error": "record_not_found", "record_id": record_id})
    db.delete(r)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="record.delete", target_type="record",
                 target_id=str(record_id), detail={}, ip=ip or None)
    db.commit()
    return {"record_id": record_id, "deleted": True}


def _usage_filtered(db, start, end, endpoint):
    q = db.query(Usage)
    if endpoint:
        q = q.filter(Usage.endpoint == endpoint)
    if start:
        q = q.filter(Usage.occurred_at >= datetime.fromisoformat(start))
    if end:
        end_dt = datetime.fromisoformat(end)
        if len(end) == 10:
            end_dt = end_dt + timedelta(days=1)
            q = q.filter(Usage.occurred_at < end_dt)
        else:
            q = q.filter(Usage.occurred_at <= end_dt)
    return q


@router.get("/usage")
def usage_summary(start: str | None = None, end: str | None = None,
                  endpoint: str | None = None,
                  user: str = Depends(require_user),
                  db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = _usage_filtered(db, start, end, endpoint)
    total_credits = q.with_entities(func.coalesce(func.sum(Usage.credits_used), 0)).scalar()
    total_records = q.with_entities(func.coalesce(func.sum(Usage.record_count), 0)).scalar()
    total_api_calls = q.with_entities(func.coalesce(func.sum(Usage.api_calls), 0)).scalar()
    total_browser_opens = q.with_entities(func.coalesce(func.sum(Usage.browser_opens), 0)).scalar()
    total_pages_fetched = q.with_entities(func.coalesce(func.sum(Usage.pages_fetched), 0)).scalar()
    return {"total_credits": int(total_credits or 0),
            "total_records": int(total_records or 0),
            "rows": q.count(),
            "total_api_calls": int(total_api_calls or 0),
            "total_browser_opens": int(total_browser_opens or 0),
            "total_pages_fetched": int(total_pages_fetched or 0)}


@router.get("/usage/by-key")
def usage_by_key(start: str | None = None, end: str | None = None,
                 endpoint: str | None = None,
                 user: str = Depends(require_user),
                 db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = _usage_filtered(db, start, end, endpoint)
    rows = (q.with_entities(Usage.api_key_id,
                            func.sum(Usage.credits_used),
                            func.count(Usage.id),
                            func.coalesce(func.sum(Usage.record_count), 0),
                            func.coalesce(func.sum(Usage.api_calls), 0),
                            func.coalesce(func.sum(Usage.browser_opens), 0),
                            func.coalesce(func.sum(Usage.pages_fetched), 0))
            .group_by(Usage.api_key_id).all())
    return {"items": [{"api_key_id": k, "credits": int(c or 0), "calls": n,
                       "records": int(r or 0),
                       "api_calls": int(ac or 0),
                       "browser_opens": int(bo or 0),
                       "pages_fetched": int(pf or 0)}
                      for k, c, n, r, ac, bo, pf in rows]}


@router.get("/usage/by-tenant")
def usage_by_tenant(start: str | None = None, end: str | None = None,
                    endpoint: str | None = None,
                    user: str = Depends(require_user),
                    db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = _usage_filtered(db, start, end, endpoint)
    rows = (q.with_entities(Usage.workspace_id,
                            func.sum(Usage.credits_used),
                            func.count(Usage.id),
                            func.coalesce(func.sum(Usage.record_count), 0),
                            func.coalesce(func.sum(Usage.api_calls), 0),
                            func.coalesce(func.sum(Usage.browser_opens), 0),
                            func.coalesce(func.sum(Usage.pages_fetched), 0))
            .group_by(Usage.workspace_id).all())
    return {"items": [{"workspace_id": w, "credits": int(c or 0), "calls": n,
                       "records": int(r or 0),
                       "api_calls": int(ac or 0),
                       "browser_opens": int(bo or 0),
                       "pages_fetched": int(pf or 0)}
                      for w, c, n, r, ac, bo, pf in rows]}


@router.get("/health")
def health(user: str = Depends(require_user),
           db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    cutoff = datetime.utcnow() - timedelta(seconds=_STUCK_SEC)
    last_hb = db.query(func.max(SpineJob.heartbeat_at)).scalar()
    last_success = (db.query(func.max(SpineJob.finished_at))
                    .filter(SpineJob.status == "success").scalar())
    recent = None
    for t in (last_hb, last_success):
        if t and (recent is None or t > recent):
            recent = t
    stuck = (db.query(SpineJob)
             .filter(SpineJob.status == "running",
                     or_(SpineJob.heartbeat_at < cutoff,
                         SpineJob.heartbeat_at.is_(None)))
             .count())
    active_running = (db.query(SpineJob)
                      .filter(SpineJob.status == "running",
                              SpineJob.heartbeat_at >= cutoff)
                      .count())
    pending = db.query(SpineJob).filter(SpineJob.status == "pending").count()
    if active_running:
        status = "running"
    elif stuck:
        status = "stuck"
    elif pending:
        status = "pending"
    elif recent is None:
        status = "unknown"
    else:
        status = "idle"
    return {"worker_status": status,
            "last_activity_at": recent.isoformat() if recent else None,
            "reclaim_hint": {"stuck_running": stuck},
            "running": active_running,
            "pending": pending}


@router.get("/config")
def config(user: str = Depends(require_user),
           db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    return {"heartbeat_interval": HEARTBEAT_INTERVAL,
            "stuck_timeout_sec": _STUCK_SEC,
            "backoff": {str(i): int(_backoff(i).total_seconds()) for i in (1, 2, 3)}}


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _proxy_health_dict(row: ProxyHealth | None) -> dict:
    if row is None:
        return {
            "status": "unknown",
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_checked_at": None,
            "last_failure_code": None,
            "last_failure_detail": None,
            "blocked_until": None,
            "updated_at": None,
        }
    return {
        "status": row.status or "unknown",
        "success_count": row.success_count or 0,
        "failure_count": row.failure_count or 0,
        "consecutive_failures": row.consecutive_failures or 0,
        "last_success_at": _dt(row.last_success_at),
        "last_failure_at": _dt(row.last_failure_at),
        "last_checked_at": _dt(row.last_checked_at),
        "last_failure_code": row.last_failure_code,
        "last_failure_detail": row.last_failure_detail,
        "blocked_until": _dt(row.blocked_until),
        "updated_at": _dt(row.updated_at),
    }


_SENSITIVE_CONFIG_KEYS = {
    "api_token",
    "vidaxl_api_token",
    "token",
    "password",
    "secret",
    "feed_url",
    "price_feed_url",
    "price_feed",
    "price_api_url",
    "pdp_price_api_url",
    "vidaxl_feed_url",
}


def _mask_config_value(key: str, value):
    if value in (None, ""):
        return value
    low = key.lower()
    if low not in _SENSITIVE_CONFIG_KEYS:
        return value
    text = str(value)
    if len(text) <= 12:
        return "****"
    return f"{text[:6]}…{text[-4:]}"


def _is_masked_config_value(key: str, value) -> bool:
    if value in (None, ""):
        return False
    low = key.lower()
    if low not in _SENSITIVE_CONFIG_KEYS:
        return False
    text = str(value)
    return text == "****" or "…" in text


def _public_crawler_config(config: dict | None) -> dict:
    cfg = config or {}
    return {k: _mask_config_value(k, v) for k, v in cfg.items()}


def _merge_crawler_config(existing: dict | None, payload: dict) -> dict:
    cfg = dict(existing or {})
    for key, value in (payload or {}).items():
        if key in {"site", "crawler_config", "proxy_tier"}:
            continue
        if _is_masked_config_value(key, value):
            continue
        if value is None or value == "":
            cfg.pop(key, None)
        else:
            cfg[key] = value
    nested = (payload or {}).get("crawler_config")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if _is_masked_config_value(key, value):
                continue
            if value is None or value == "":
                cfg.pop(key, None)
            else:
                cfg[key] = value
    return cfg


def _product_price_source_sample(row: Product) -> dict:
    return {
        "sku": row.sku,
        "title": row.title,
        "sale_price": row.sale_price,
        "original_price": row.original_price,
        "currency": row.currency,
        "product_url": row.product_url,
    }


@router.get("/sites/{site_code}/crawler-config")
def site_crawler_config(site_code: str,
                        user: str = Depends(require_user),
                        db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    site = db.query(Site).filter(Site.site == site_code).first()
    if not site:
        raise HTTPException(404, {"error": "site_not_found", "site": site_code})
    cfg = site.crawler_config or {}
    return {
        "site": site.site,
        "platform": site.platform,
        "proxy_tier": site.proxy_tier,
        "crawler_config": _public_crawler_config(cfg),
        "configured_keys": sorted(cfg.keys()),
    }


@router.patch("/sites/{site_code}/crawler-config")
def site_crawler_config_update(site_code: str, payload: dict,
                               user: str = Depends(require_user),
                               db: Session = Depends(get_db),
                               ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    site = db.query(Site).filter(Site.site == site_code).first()
    if not site:
        raise HTTPException(404, {"error": "site_not_found", "site": site_code})
    before_keys = sorted((site.crawler_config or {}).keys())
    proxy_tier = (payload or {}).get("proxy_tier")
    if proxy_tier is not None:
        site.proxy_tier = str(proxy_tier or "none").strip() or "none"
    site.crawler_config = _merge_crawler_config(site.crawler_config, payload or {})
    site.updated_at = datetime.utcnow()
    after_keys = sorted((site.crawler_config or {}).keys())
    record_audit(
        db,
        actor_user_id=actor.id,
        actor_name=actor.username,
        action="site.crawler_config.update",
        target_type="site",
        target_id=site.site,
        detail={"before_keys": before_keys, "after_keys": after_keys},
        ip=ip or None,
    )
    db.commit()
    return {
        "site": site.site,
        "proxy_tier": site.proxy_tier,
        "crawler_config": _public_crawler_config(site.crawler_config),
        "configured_keys": after_keys,
    }


@router.post("/sites/{site_code}/crawler-config/test-price-source")
def site_crawler_config_test_price_source(
    site_code: str,
    payload: dict | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """Dry-run 当前/待保存价格源配置，避免后台盲配后再跑整站。"""
    _require_super_admin(user, db)
    site = db.query(Site).filter(Site.site == site_code).first()
    if not site:
        raise HTTPException(404, {"error": "site_not_found", "site": site_code})
    payload = payload or {}
    sample_limit = max(1, min(20, int(payload.get("sample_limit") or 5)))
    config = _merge_crawler_config(site.crawler_config, payload)
    proxy_tier = str(payload.get("proxy_tier") or site.proxy_tier or "none")
    sample_rows = (
        db.query(Product)
        .filter(Product.site == site_code)
        .order_by(
            Product.sale_price.isnot(None),
            Product.original_price.isnot(None),
            Product.updated_time.desc().nullslast(),
            Product.id.desc(),
        )
        .limit(sample_limit)
        .all()
    )
    samples = [_product_price_source_sample(row) for row in sample_rows]
    if not samples:
        return {
            "site": site.site,
            "status": "no_sample_products",
            "sample_count": 0,
            "stats": {
                "applied": False,
                "rows": 0,
                "matched": 0,
                "updated": 0,
                "error": "该站点暂无商品样例，需先跑出商品列表再测试价格源",
            },
            "samples": [],
        }
    test_site = SimpleNamespace(
        site=site.site,
        brand=site.brand,
        country=site.country,
        platform=site.platform,
        url=site.url,
        proxy_tier=proxy_tier,
        crawler_config=config,
    )
    before = [dict(item) for item in samples]
    enriched, stats = enrich_products_from_site_config(test_site, samples)
    sample_results = []
    for prev, item in zip(before, enriched):
        sample_results.append({
            "sku": item.get("sku"),
            "product_url": item.get("product_url"),
            "before": {
                "title": prev.get("title"),
                "sale_price": prev.get("sale_price"),
                "original_price": prev.get("original_price"),
                "currency": prev.get("currency"),
            },
            "after": {
                "title": item.get("title"),
                "sale_price": item.get("sale_price"),
                "original_price": item.get("original_price"),
                "currency": item.get("currency"),
            },
            "changed": any(
                prev.get(key) != item.get(key)
                for key in ("title", "sale_price", "original_price", "currency")
            ),
        })
    return {
        "site": site.site,
        "status": "ok" if stats.get("applied") and not stats.get("error") else "check_failed",
        "sample_count": len(sample_results),
        "proxy_tier": proxy_tier,
        "stats": stats,
        "samples": sample_results,
    }


def _proxy_rule_availability(
    row: ProxyRule,
    *,
    pool_available_count: dict[str, int],
    pool_member_count_by_slug: dict[str, int],
    pool_fallback_by_slug: dict[str, str | None],
) -> dict:
    mode = (row.proxy_mode or "pool").strip().lower()
    primary_pool_slug: str | None = None
    if mode == "pool":
        primary_pool_slug = (row.pool_slug or "").strip() or None
    elif mode in ("datacenter", "residential"):
        primary_pool_slug = mode

    fallback_pool_slug = (
        (row.fallback_pool_slug or "").strip()
        or (pool_fallback_by_slug.get(primary_pool_slug) if primary_pool_slug else None)
    )
    primary_available = (
        pool_available_count.get(primary_pool_slug, 0) if primary_pool_slug else 0
    )
    fallback_available = (
        pool_available_count.get(fallback_pool_slug, 0) if fallback_pool_slug else 0
    )
    if not row.enabled:
        effective_status = "disabled"
    elif mode == "none":
        effective_status = "direct"
    elif primary_pool_slug and primary_available > 0:
        effective_status = "primary_available"
    elif fallback_pool_slug and fallback_available > 0:
        effective_status = "fallback_available"
    elif primary_pool_slug or fallback_pool_slug:
        effective_status = "unavailable"
    else:
        effective_status = "misconfigured"

    return {
        "primary_pool_slug": primary_pool_slug,
        "fallback_pool_slug": fallback_pool_slug,
        "primary_member_count": (
            pool_member_count_by_slug.get(primary_pool_slug, 0)
            if primary_pool_slug else 0
        ),
        "fallback_member_count": (
            pool_member_count_by_slug.get(fallback_pool_slug, 0)
            if fallback_pool_slug else 0
        ),
        "primary_available_count": primary_available,
        "fallback_available_count": fallback_available,
        "effective_status": effective_status,
    }


def _proxy_pool_availability(
    row: ProxyPoolConfig,
    *,
    pool_available_count: dict[str, int],
    pool_member_count_by_slug: dict[str, int],
) -> dict:
    primary_available = int(pool_available_count.get(row.slug, 0) or 0)
    fallback_slug = (row.fallback_pool_slug or "").strip() or None
    fallback_available = (
        int(pool_available_count.get(fallback_slug, 0) or 0)
        if fallback_slug else 0
    )
    if not row.active:
        effective_status = "disabled"
    elif primary_available > 0:
        effective_status = "primary_available"
    elif fallback_slug and fallback_available > 0:
        effective_status = "fallback_available"
    elif pool_member_count_by_slug.get(row.slug, 0) > 0:
        effective_status = "unavailable"
    else:
        effective_status = "empty"

    return {
        "primary_pool_slug": row.slug,
        "primary_member_count": int(pool_member_count_by_slug.get(row.slug, 0) or 0),
        "primary_available_count": primary_available,
        "fallback_pool_slug": fallback_slug,
        "fallback_member_count": (
            int(pool_member_count_by_slug.get(fallback_slug, 0) or 0)
            if fallback_slug else 0
        ),
        "fallback_available_count": fallback_available,
        "effective_available_count": (
            primary_available if primary_available > 0 else fallback_available
        ),
        "effective_status": effective_status,
    }


def _proxy_rule_matches_site(row: ProxyRule, site: str) -> bool:
    pattern = (row.site_pattern or "").strip().lower()
    value = (site or "").strip().lower()
    if not pattern or not value:
        return False
    match_type = (row.match_type or "contains").strip().lower()
    if match_type == "exact":
        return value == pattern
    if match_type == "prefix":
        return value.startswith(pattern)
    return pattern in value


def _recommended_proxy_rule(site: str, issue: str) -> dict:
    return {
        "site_pattern": site,
        "match_type": "exact",
        "proxy_mode": "pool",
        "pool_slug": "residential",
        "fallback_pool_slug": "datacenter",
        "priority": 90 if issue == "anti_bot_blocked" else 85,
        "notes": f"数据质量自动建议：{issue}",
    }


def _anti_bot_quality_payload(db: Session, *, tenant: int | None,
                              include_hidden: bool) -> dict:
    q = (db.query(WorkspaceSite.site, WorkspaceSite.target_sku_count)
         .join(Workspace, Workspace.id == WorkspaceSite.workspace_id)
         .filter(WorkspaceSite.enabled.is_(True), Workspace.status == "active"))
    if tenant is not None:
        q = q.filter(WorkspaceSite.workspace_id == tenant)
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))
    site_codes: list[str] = []
    target_sku_by_site: dict[str, int] = {}
    for site, target_sku_count in q.order_by(WorkspaceSite.site).all():
        if site not in site_codes:
            site_codes.append(site)
        if target_sku_count:
            target_sku_by_site[site] = max(
                int(target_sku_by_site.get(site, 0)),
                int(target_sku_count),
            )
    sites = db.query(Site).filter(Site.site.in_(site_codes)).all() if site_codes else []
    return _build_data_quality_payload(db, sites, target_sku_by_site)


def _anti_bot_diagnostics_payload(
    db: Session,
    *,
    tenant: int | None = None,
    include_hidden: bool = False,
) -> dict:
    quality = _anti_bot_quality_payload(
        db, tenant=tenant, include_hidden=include_hidden)
    proxy_payload = _proxy_admin_payload(db)
    rule_rows = db.query(ProxyRule).order_by(
        ProxyRule.priority.asc(), ProxyRule.id.asc()).all()
    rule_payload_by_id = {row.get("id"): row for row in proxy_payload.get("rules", [])}
    items = []
    issue_keys = {"anti_bot_blocked", "proxy_unavailable", "proxy_auth_failed"}
    for row in quality.get("items") or []:
        issues = [issue for issue in row.get("issues") or [] if issue in issue_keys]
        if not issues:
            continue
        site = str(row.get("site") or "")
        matched_rule = next(
            (rule for rule in rule_rows if rule.enabled and _proxy_rule_matches_site(rule, site)),
            None,
        )
        matched_payload = rule_payload_by_id.get(matched_rule.id) if matched_rule else None
        issue = issues[0]
        items.append({
            "site": site,
            "brand": row.get("brand"),
            "country": row.get("country"),
            "url": row.get("url"),
            "issues": issues,
            "latest_job": row.get("latest_job"),
            "latest_failure": row.get("latest_failure"),
            "last_error_code": row.get("last_error_code"),
            "last_error": row.get("last_error"),
            "suggested_action": row.get("suggested_action"),
            "current_rule": matched_payload,
            "rule_status": (matched_payload or {}).get("effective_status"),
            "recommended_rule": _recommended_proxy_rule(site, issue),
            "probe": None,
        })
    return {
        "items": items,
        "count": len(items),
        "summary": {
            "anti_bot_blocked": sum(1 for item in items if "anti_bot_blocked" in item["issues"]),
            "proxy_unavailable": sum(1 for item in items if "proxy_unavailable" in item["issues"]),
            "proxy_auth_failed": sum(1 for item in items if "proxy_auth_failed" in item["issues"]),
            "with_available_rule": sum(
                1 for item in items
                if item.get("rule_status") in {"primary_available", "fallback_available", "direct"}
            ),
            "needs_rule": sum(1 for item in items if not item.get("current_rule")),
        },
        "proxy": {
            "available": sum(
                int(row.get("available_count") or 0)
                for row in proxy_payload.get("pools", [])
            ),
            "pools": proxy_payload.get("pools", []),
        },
    }


def _proxy_admin_payload(db: Session) -> dict:
    from ..proxy_pool import pool_status
    from ..proxy_health import proxy_health_summary
    from ..proxy_config import endpoint_dict, pool_dict, rule_dict

    pool = pool_status()
    pool_rows = pool.get("details") or []
    pool_by_hash = {row.get("hash"): row for row in pool_rows if row.get("hash")}
    pool_by_endpoint_id = {row.get("endpoint_id"): row for row in pool_rows
                           if row.get("endpoint_id")}
    health_rows = db.query(ProxyHealth).order_by(ProxyHealth.updated_at.desc()).all()
    health_by_hash = {row.proxy_hash: row for row in health_rows if row.proxy_hash}
    members = (db.query(ProxyPoolMember, ProxyPoolConfig)
               .join(ProxyPoolConfig, ProxyPoolConfig.id == ProxyPoolMember.pool_id)
               .all())
    pools_by_endpoint: dict[int, list[str]] = {}
    pool_member_count: dict[int, int] = {}
    for member, pool_cfg in members:
        if member.active:
            pools_by_endpoint.setdefault(member.endpoint_id, []).append(pool_cfg.slug)
            pool_member_count[pool_cfg.id] = pool_member_count.get(pool_cfg.id, 0) + 1
    endpoints = db.query(ProxyEndpoint).order_by(ProxyEndpoint.id.asc()).all()
    pool_available_count: dict[str, int] = {}
    for row in pool_rows:
        if row.get("available"):
            for slug in row.get("pools") or []:
                pool_available_count[slug] = pool_available_count.get(slug, 0) + 1
    pool_configs = db.query(ProxyPoolConfig).order_by(ProxyPoolConfig.slug.asc()).all()
    pool_member_count_by_slug = {
        row.slug: pool_member_count.get(row.id, 0)
        for row in pool_configs
    }
    pool_fallback_by_slug = {
        row.slug: row.fallback_pool_slug
        for row in pool_configs
    }
    rules = db.query(ProxyRule).order_by(ProxyRule.priority.asc(), ProxyRule.id.asc()).all()

    ordered_hashes: list[str] = []
    for row in pool_rows:
        h = row.get("hash")
        if h and h not in ordered_hashes:
            ordered_hashes.append(h)
    for row in health_rows:
        if row.proxy_hash and row.proxy_hash not in ordered_hashes:
            ordered_hashes.append(row.proxy_hash)

    now = datetime.utcnow()
    items = []
    for h in ordered_hashes:
        pool_row = pool_by_hash.get(h)
        health_row = health_by_hash.get(h)
        item = {
            "hash": h,
            "proxy": (health_row.proxy_redacted if health_row else None)
                     or (pool_row or {}).get("url"),
            "tier": (health_row.tier if health_row else None)
                    or (pool_row or {}).get("tier"),
            "configured": pool_row is not None,
            "pool_available": bool((pool_row or {}).get("available", False)),
            "pool_blocked_for_sec": int((pool_row or {}).get("blocked_for_sec") or 0),
            "endpoint_id": (pool_row or {}).get("endpoint_id"),
            "source": (pool_row or {}).get("source"),
            "pools": (pool_row or {}).get("pools") or [],
            "provider": (pool_row or {}).get("provider"),
            "country": (pool_row or {}).get("country"),
            "exclude": (pool_row or {}).get("exclude") or [],
            "pool_fail_count": int((pool_row or {}).get("fail_count") or 0),
            "pool_success_count": int((pool_row or {}).get("success_count") or 0),
            **_proxy_health_dict(health_row),
        }
        blocked_until = health_row.blocked_until if health_row else None
        item["persistently_blocking"] = (
            item["status"] in ("blocked", "down")
            and (blocked_until is None or blocked_until > now)
        )
        items.append(item)

    return {
        "pool": pool,
        "health": proxy_health_summary(db),
        "items": items,
        "endpoints": [
            {
                **endpoint_dict(row, pools=sorted(pools_by_endpoint.get(row.id, []))),
                "pool_available": bool((pool_by_endpoint_id.get(row.id) or {}).get("available")),
                "health": _proxy_health_dict(health_by_hash.get(row.proxy_hash)),
                "health_status": _proxy_health_dict(health_by_hash.get(row.proxy_hash))["status"],
            }
            for row in endpoints
        ],
        "pools": [
            {
                **pool_dict(row,
                            members=pool_member_count.get(row.id, 0),
                            available=pool_available_count.get(row.slug, 0)),
                **_proxy_pool_availability(
                    row,
                    pool_available_count=pool_available_count,
                    pool_member_count_by_slug=pool_member_count_by_slug,
                ),
            }
            for row in pool_configs
        ],
        "rules": [
            {
                **rule_dict(row),
                **_proxy_rule_availability(
                    row,
                    pool_available_count=pool_available_count,
                    pool_member_count_by_slug=pool_member_count_by_slug,
                    pool_fallback_by_slug=pool_fallback_by_slug,
                ),
            }
            for row in rules
        ],
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/proxies")
def proxies_status(user: str = Depends(require_user),
                   db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    return _proxy_admin_payload(db)


@router.post("/proxies/reload")
def proxies_reload(user: str = Depends(require_user),
                   db: Session = Depends(get_db),
                   ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    from ..proxy_pool import reload_pool

    reload_pool()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.reload", target_type="proxy_pool",
                 target_id="pool", detail={}, ip=ip or None)
    db.commit()
    return {"reloaded": True, **_proxy_admin_payload(db)}


def _split_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).replace(",", "\n").splitlines()
    return [str(x).strip() for x in raw if str(x).strip()]


def _reload_pool_safely() -> None:
    try:
        from ..proxy_pool import reload_pool

        reload_pool()
    except Exception:
        pass


def _update_endpoint_from_payload(row: ProxyEndpoint, payload: dict) -> None:
    from ..proxy_config import normalize_endpoint_type
    from ..proxy_health import proxy_hash, redact_proxy

    if "proxy_url" in payload and payload.get("proxy_url"):
        proxy_url = str(payload["proxy_url"]).strip()
        parsed = urlparse(proxy_url)
        row.proxy_url = proxy_url
        row.proxy_hash = proxy_hash(proxy_url)
        row.proxy_redacted = redact_proxy(proxy_url)
        row.scheme = parsed.scheme or None
        row.host = parsed.hostname
        row.port = parsed.port
    if "endpoint_type" in payload:
        row.endpoint_type = normalize_endpoint_type(payload.get("endpoint_type"))
    for field in ("name", "provider", "country", "source", "notes"):
        if field in payload:
            setattr(row, field, payload.get(field))
    if "active" in payload:
        row.active = bool(payload.get("active"))
    if "exclude_sites" in payload or "exclude" in payload:
        row.exclude_sites = [x.lower() for x in _split_list(
            payload.get("exclude_sites", payload.get("exclude")))]
    if "tags" in payload:
        row.tags = _split_list(payload.get("tags"))
    if "max_concurrency" in payload:
        row.max_concurrency = max(1, int(payload.get("max_concurrency") or 1))
    row.updated_at = datetime.utcnow()


@router.post("/proxies/import-file")
def proxies_import_file(user: str = Depends(require_user),
                        db: Session = Depends(get_db),
                        ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    from ..proxy_config import import_proxy_file

    result = import_proxy_file(db)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.import_file", target_type="proxy_pool",
                 target_id="file", detail=result, ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"imported": result, **_proxy_admin_payload(db)}


@router.post("/proxies/endpoints")
def proxy_endpoint_create(payload: dict,
                          user: str = Depends(require_user),
                          db: Session = Depends(get_db),
                          ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    from ..proxy_config import upsert_proxy_endpoint

    row = upsert_proxy_endpoint(
        db,
        proxy_url=payload.get("proxy_url") or payload.get("url"),
        endpoint_type=payload.get("endpoint_type") or payload.get("tier") or "datacenter",
        name=payload.get("name"),
        provider=payload.get("provider"),
        country=payload.get("country"),
        active=bool(payload.get("active", True)),
        exclude_sites=_split_list(payload.get("exclude_sites", payload.get("exclude"))),
        tags=_split_list(payload.get("tags")),
        max_concurrency=payload.get("max_concurrency"),
        source="admin",
        notes=payload.get("notes"),
    )
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.endpoint.upsert", target_type="proxy_endpoint",
                 target_id=str(row.id), detail={"hash": row.proxy_hash[:12]},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"endpoint_id": row.id, **_proxy_admin_payload(db)}


@router.patch("/proxies/endpoints/{endpoint_id}")
def proxy_endpoint_update(endpoint_id: int, payload: dict,
                          user: str = Depends(require_user),
                          db: Session = Depends(get_db),
                          ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    row = db.get(ProxyEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(404, {"error": "proxy_endpoint_not_found",
                                  "endpoint_id": endpoint_id})
    if payload.get("proxy_url"):
        from ..proxy_health import proxy_hash

        h = proxy_hash(str(payload["proxy_url"]).strip())
        other = (db.query(ProxyEndpoint)
                 .filter(ProxyEndpoint.proxy_hash == h,
                         ProxyEndpoint.id != endpoint_id)
                 .first())
        if other is not None:
            raise HTTPException(409, {"error": "proxy_url_already_exists",
                                      "endpoint_id": other.id})
    _update_endpoint_from_payload(row, payload)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.endpoint.update", target_type="proxy_endpoint",
                 target_id=str(row.id), detail={"fields": sorted(payload.keys())},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"endpoint_id": row.id, **_proxy_admin_payload(db)}


def _proxy_endpoint_probe_detail(
    row: ProxyEndpoint,
    *,
    url: str,
    timeout: int,
) -> dict:
    from ..proxy_probe import probe_proxy_url

    result = probe_proxy_url(
        proxy_url=row.proxy_url,
        tier=row.endpoint_type,
        url=url,
        timeout=timeout,
    )
    failure = result.failure
    return {
        "endpoint_id": row.id,
        "endpoint_type": row.endpoint_type,
        "proxy": row.proxy_redacted,
        "url": url,
        "ok": result.ok,
        "status_code": result.status_code,
        "failure_code": failure.code if failure else None,
        "failure_stage": failure.stage if failure else None,
        "failure_detail": failure.detail if failure else None,
    }


@router.post("/proxies/endpoints/check-batch")
def proxy_endpoint_check_batch(payload: dict | None = None,
                               user: str = Depends(require_user),
                               db: Session = Depends(get_db),
                               ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    """批量直接检测代理端点本身，不走站点规则或 fallback。"""
    actor = _require_super_admin(user, db)
    payload = payload or {}
    url = payload.get("url") or "https://www.google.com/generate_204"
    timeout = max(3, min(30, int(payload.get("timeout") or 8)))
    limit = max(1, min(100, int(payload.get("limit") or 50)))
    endpoint_type = (payload.get("endpoint_type") or payload.get("tier") or "").strip()
    health_status = (payload.get("health_status") or "").strip()
    active_only = bool(payload.get("active_only", True))

    q = db.query(ProxyEndpoint).filter(ProxyEndpoint.proxy_url.isnot(None))
    if active_only:
        q = q.filter(ProxyEndpoint.active.is_(True))
    if endpoint_type:
        q = q.filter(ProxyEndpoint.endpoint_type == endpoint_type)
    if health_status:
        q = q.join(ProxyHealth, ProxyHealth.proxy_hash == ProxyEndpoint.proxy_hash)
        q = q.filter(ProxyHealth.status == health_status)
    rows = q.order_by(ProxyEndpoint.endpoint_type.asc(),
                      ProxyEndpoint.id.asc()).limit(limit).all()

    results = [
        _proxy_endpoint_probe_detail(row, url=url, timeout=timeout)
        for row in rows
    ]
    ok = sum(1 for row in results if row.get("ok"))
    failed = len(results) - ok
    detail = {
        "url": url,
        "timeout": timeout,
        "limit": limit,
        "endpoint_type": endpoint_type or None,
        "health_status": health_status or None,
        "active_only": active_only,
        "checked": len(results),
        "ok": ok,
        "failed": failed,
        "results": results,
    }
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.endpoint.check_batch", target_type="proxy_endpoint",
                 target_id="batch", detail={k: v for k, v in detail.items()
                                            if k != "results"}, ip=ip or None)
    db.commit()
    _reload_pool_safely()
    db.expire_all()
    return {"batch": detail, **_proxy_admin_payload(db)}


@router.post("/proxies/endpoints/{endpoint_id}/check")
def proxy_endpoint_check(endpoint_id: int, payload: dict | None = None,
                         user: str = Depends(require_user),
                         db: Session = Depends(get_db),
                         ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    """直接检测某个代理端点，不走站点规则或 fallback。"""
    actor = _require_super_admin(user, db)
    row = db.get(ProxyEndpoint, endpoint_id)
    if row is None or not row.proxy_url:
        raise HTTPException(404, {"error": "proxy_endpoint_not_found",
                                  "endpoint_id": endpoint_id})
    payload = payload or {}
    url = payload.get("url") or "https://www.vidaxl.de/sitemap_index.xml"
    timeout = max(3, min(30, int(payload.get("timeout") or 8)))
    detail = _proxy_endpoint_probe_detail(row, url=url, timeout=timeout)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.endpoint.check", target_type="proxy_endpoint",
                 target_id=str(row.id), detail=detail, ip=ip or None)
    db.commit()
    _reload_pool_safely()
    db.expire_all()
    return {"probe": detail, **_proxy_admin_payload(db)}


@router.post("/proxies/pools")
def proxy_pool_create(payload: dict,
                      user: str = Depends(require_user),
                      db: Session = Depends(get_db),
                      ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    slug = (payload.get("slug") or "").strip().lower()
    if not slug:
        raise HTTPException(422, {"error": "slug required"})
    row = db.query(ProxyPoolConfig).filter(ProxyPoolConfig.slug == slug).first()
    if row is None:
        row = ProxyPoolConfig(slug=slug, created_at=datetime.utcnow())
        db.add(row)
    row.name = payload.get("name") or row.name or slug
    row.pool_type = payload.get("pool_type") or row.pool_type or "mixed"
    row.active = bool(payload.get("active", True))
    row.fallback_pool_slug = payload.get("fallback_pool_slug")
    row.description = payload.get("description")
    row.updated_at = datetime.utcnow()
    db.flush()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.pool.upsert", target_type="proxy_pool",
                 target_id=slug, detail={}, ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"pool_id": row.id, **_proxy_admin_payload(db)}


@router.patch("/proxies/pools/{pool_id}")
def proxy_pool_update(pool_id: int, payload: dict,
                      user: str = Depends(require_user),
                      db: Session = Depends(get_db),
                      ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    row = db.get(ProxyPoolConfig, pool_id)
    if row is None:
        raise HTTPException(404, {"error": "proxy_pool_not_found", "pool_id": pool_id})
    for field in ("name", "pool_type", "fallback_pool_slug", "description"):
        if field in payload:
            setattr(row, field, payload.get(field))
    if "active" in payload:
        row.active = bool(payload.get("active"))
    row.updated_at = datetime.utcnow()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.pool.update", target_type="proxy_pool",
                 target_id=row.slug, detail={"fields": sorted(payload.keys())},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"pool_id": row.id, **_proxy_admin_payload(db)}


@router.post("/proxies/pools/{pool_id}/members")
def proxy_pool_member_upsert(pool_id: int, payload: dict,
                             user: str = Depends(require_user),
                             db: Session = Depends(get_db),
                             ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    pool = db.get(ProxyPoolConfig, pool_id)
    endpoint_id = int(payload.get("endpoint_id") or 0)
    endpoint = db.get(ProxyEndpoint, endpoint_id) if endpoint_id else None
    if pool is None or endpoint is None:
        raise HTTPException(404, {"error": "proxy_pool_or_endpoint_not_found"})
    row = (db.query(ProxyPoolMember)
           .filter(ProxyPoolMember.pool_id == pool.id,
                   ProxyPoolMember.endpoint_id == endpoint.id)
           .first())
    if row is None:
        row = ProxyPoolMember(pool_id=pool.id, endpoint_id=endpoint.id,
                              created_at=datetime.utcnow())
        db.add(row)
    row.active = bool(payload.get("active", True))
    row.weight = max(1, int(payload.get("weight") or 1))
    row.priority = int(payload.get("priority") or 100)
    row.updated_at = datetime.utcnow()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.pool.member.upsert", target_type="proxy_pool",
                 target_id=pool.slug,
                 detail={"endpoint_id": endpoint.id, "active": row.active},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"member_id": row.id, **_proxy_admin_payload(db)}


@router.post("/proxies/rules")
def proxy_rule_create(payload: dict,
                      user: str = Depends(require_user),
                      db: Session = Depends(get_db),
                      ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    pattern = (payload.get("site_pattern") or payload.get("site") or "").strip()
    if not pattern:
        raise HTTPException(422, {"error": "site_pattern required"})
    match_type = (payload.get("match_type") or "contains").strip() or "contains"
    row = (db.query(ProxyRule)
           .filter(ProxyRule.site_pattern == pattern,
                   ProxyRule.match_type == match_type)
           .first())
    if row is None:
        row = ProxyRule(site_pattern=pattern, match_type=match_type,
                        created_at=datetime.utcnow())
        db.add(row)
    _update_rule_from_payload(row, payload)
    db.flush()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.rule.upsert", target_type="proxy_rule",
                 target_id=str(row.id), detail={"site_pattern": row.site_pattern},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"rule_id": row.id, **_proxy_admin_payload(db)}


@router.patch("/proxies/rules/{rule_id}")
def proxy_rule_update(rule_id: int, payload: dict,
                      user: str = Depends(require_user),
                      db: Session = Depends(get_db),
                      ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    row = db.get(ProxyRule, rule_id)
    if row is None:
        raise HTTPException(404, {"error": "proxy_rule_not_found", "rule_id": rule_id})
    _update_rule_from_payload(row, payload)
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.rule.update", target_type="proxy_rule",
                 target_id=str(row.id), detail={"fields": sorted(payload.keys())},
                 ip=ip or None)
    db.commit()
    _reload_pool_safely()
    return {"rule_id": row.id, **_proxy_admin_payload(db)}


def _update_rule_from_payload(row: ProxyRule, payload: dict) -> None:
    for field in ("site_pattern", "match_type", "proxy_mode", "pool_slug",
                  "fallback_pool_slug", "notes"):
        if field in payload:
            setattr(row, field, payload.get(field))
    if "site" in payload and "site_pattern" not in payload:
        row.site_pattern = payload.get("site")
    if "priority" in payload:
        row.priority = int(payload.get("priority") or 100)
    elif row.priority is None:
        row.priority = 100
    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))
    elif row.enabled is None:
        row.enabled = True
    if not row.match_type:
        row.match_type = "contains"
    if not row.proxy_mode:
        row.proxy_mode = "pool"
    row.updated_at = datetime.utcnow()


@router.post("/proxies/{proxy_hash_value}/clear")
def proxy_clear(proxy_hash_value: str, user: str = Depends(require_user),
                db: Session = Depends(get_db),
                ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    row = (db.query(ProxyHealth)
           .filter(ProxyHealth.proxy_hash == proxy_hash_value)
           .first())
    if row is None:
        raise HTTPException(404, {"error": "proxy_health_not_found",
                                  "proxy_hash": proxy_hash_value})
    prev = {
        "status": row.status,
        "blocked_until": _dt(row.blocked_until),
        "consecutive_failures": row.consecutive_failures or 0,
        "last_failure_code": row.last_failure_code,
    }
    row.status = "unknown"
    row.consecutive_failures = 0
    row.blocked_until = None
    row.updated_at = datetime.utcnow()
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.clear", target_type="proxy",
                 target_id=proxy_hash_value[:12], detail={"prev": prev},
                 ip=ip or None)
    db.commit()
    return {"cleared": True, **_proxy_admin_payload(db)}


@router.post("/proxies/check")
def proxies_check(payload: dict | None = None,
                  user: str = Depends(require_user),
                  db: Session = Depends(get_db),
                  ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
    payload = payload or {}
    tier = payload.get("tier") or "residential"
    site = payload.get("site") or "admin_proxy_check"
    url = payload.get("url") or "https://www.vidaxl.de/sitemap_index.xml"
    timeout = int(payload.get("timeout") or 8)
    from ..proxy_probe import probe_proxy_for_url

    result = probe_proxy_for_url(tier=tier, site=site, url=url, timeout=timeout)
    failure = result.failure
    detail = {
        "tier": tier,
        "site": site,
        "url": url,
        "ok": result.ok,
        "status_code": result.status_code,
        "failure_code": failure.code if failure else None,
        "failure_stage": failure.stage if failure else None,
        "failure_detail": failure.detail if failure else None,
    }
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.check", target_type="proxy_pool",
                 target_id=tier, detail=detail, ip=ip or None)
    db.commit()
    return {"probe": detail, **_proxy_admin_payload(db)}


@router.get("/proxies/anti-bot")
def proxies_anti_bot_diagnostics(
    tenant: int | None = None,
    include_hidden: bool = Query(default=False),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """从数据质量结果生成反爬/代理问题站点清单。"""
    _require_super_admin(user, db)
    return _anti_bot_diagnostics_payload(
        db, tenant=tenant, include_hidden=include_hidden)


@router.post("/proxies/anti-bot/check")
def proxies_anti_bot_check(
    payload: dict | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """批量预检当前反爬站点的推荐代理路径。"""
    actor = _require_super_admin(user, db)
    payload = payload or {}
    tenant = payload.get("tenant")
    tenant_id = int(tenant) if tenant not in (None, "") else None
    limit = max(1, min(20, int(payload.get("limit") or 10)))
    timeout = max(3, min(30, int(payload.get("timeout") or 8)))
    requested_sites = {
        str(site).strip() for site in (payload.get("sites") or [])
        if str(site).strip()
    }
    diagnostics = _anti_bot_diagnostics_payload(
        db,
        tenant=tenant_id,
        include_hidden=bool(payload.get("include_hidden")),
    )
    targets = diagnostics.get("items") or []
    if requested_sites:
        targets = [row for row in targets if row.get("site") in requested_sites]
    targets = targets[:limit]
    from ..proxy_probe import probe_proxy_for_url

    results = []
    for row in targets:
        site = row.get("site")
        rule = row.get("current_rule") or row.get("recommended_rule") or {}
        tier = (
            f"pool:{rule.get('pool_slug')}"
            if rule.get("proxy_mode") == "pool" and rule.get("pool_slug")
            else rule.get("proxy_mode") or "residential"
        )
        url = row.get("url") or "https://example.com"
        result = probe_proxy_for_url(
            tier=tier,
            site=site,
            url=url,
            timeout=timeout,
        )
        failure = result.failure
        probe = {
            "tier": tier,
            "site": site,
            "url": url,
            "ok": result.ok,
            "status_code": result.status_code,
            "failure_code": failure.code if failure else None,
            "failure_stage": failure.stage if failure else None,
            "failure_detail": failure.detail if failure else None,
        }
        row = {**row, "probe": probe}
        results.append(row)
    audit_detail = {
        "count": len(results),
        "ok": sum(1 for row in results if (row.get("probe") or {}).get("ok")),
        "sites": [row.get("site") for row in results],
    }
    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="proxy.anti_bot_check", target_type="proxy_pool",
                 target_id="anti_bot", detail=audit_detail, ip=ip or None)
    db.commit()
    return {
        **diagnostics,
        "items": results,
        "checked": len(results),
        "ok": audit_detail["ok"],
        "failed": len(results) - audit_detail["ok"],
        "checked_at": datetime.utcnow().isoformat(),
    }


@router.post("/proxies/anti-bot/apply-rules")
def proxies_anti_bot_apply_rules(
    payload: dict | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """把反爬诊断推荐的住宅代理规则批量落库。"""
    actor = _require_super_admin(user, db)
    payload = payload or {}
    tenant = payload.get("tenant")
    tenant_id = int(tenant) if tenant not in (None, "") else None
    requested_sites = {
        str(site).strip() for site in (payload.get("sites") or [])
        if str(site).strip()
    }
    diagnostics = _anti_bot_diagnostics_payload(
        db,
        tenant=tenant_id,
        include_hidden=bool(payload.get("include_hidden")),
    )
    targets = diagnostics.get("items") or []
    if requested_sites:
        targets = [row for row in targets if row.get("site") in requested_sites]
    applied = []
    for item in targets:
        rule_payload = dict(item.get("recommended_rule") or {})
        pattern = (rule_payload.get("site_pattern") or item.get("site") or "").strip()
        if not pattern:
            continue
        match_type = (rule_payload.get("match_type") or "exact").strip() or "exact"
        row = (db.query(ProxyRule)
               .filter(ProxyRule.site_pattern == pattern,
                       ProxyRule.match_type == match_type)
               .first())
        created = row is None
        if row is None:
            row = ProxyRule(site_pattern=pattern, match_type=match_type,
                            created_at=datetime.utcnow())
            db.add(row)
        _update_rule_from_payload(row, {**rule_payload, "enabled": True})
        db.flush()
        applied.append({
            "site": pattern,
            "rule_id": row.id,
            "created": created,
            "pool_slug": row.pool_slug,
            "fallback_pool_slug": row.fallback_pool_slug,
        })
    record_audit(
        db,
        actor_user_id=actor.id,
        actor_name=actor.username,
        action="proxy.anti_bot_apply_rules",
        target_type="proxy_rule",
        target_id="anti_bot",
        detail={"count": len(applied), "sites": [row["site"] for row in applied]},
        ip=ip or None,
    )
    db.commit()
    _reload_pool_safely()
    refreshed = _anti_bot_diagnostics_payload(
        db,
        tenant=tenant_id,
        include_hidden=bool(payload.get("include_hidden")),
    )
    return {
        **refreshed,
        "applied": applied,
        "applied_count": len(applied),
        "applied_at": datetime.utcnow().isoformat(),
    }


@router.get("/inventory")
def inventory(cached: bool = False,
              user: str = Depends(require_user),
              db: Session = Depends(get_db)) -> dict:
    """全库库存概览。

    admin-app 的 spine 数据集只覆盖 normalized view 层；这个端点把 legacy
    商品/VOC/按需任务和 spine 新管线放在同一张库存图上，避免误判为空库。
    """
    _require_super_admin(user, db)
    global _INVENTORY_CACHE, _INVENTORY_CACHE_TS
    now_ts = time.time()
    if (cached and _INVENTORY_CACHE is not None
            and now_ts - _INVENTORY_CACHE_TS <= _INVENTORY_CACHE_TTL):
        return _INVENTORY_CACHE
    legacy_counts = {
        "sites": _table_count(db, Site),
        "products": _table_count(db, Product),
        "reviews": _table_count(db, Review),
        "categories": _table_count(db, Category),
        "promotions": _table_count(db, Promotion),
        "price_history": _table_count(db, PriceHistory),
        "crawl_jobs": _table_count(db, CrawlJob),
        "ondemand_jobs": _table_count(db, OnDemandJob),
    }
    spine_counts = {
        "datasets": _table_count(db, Dataset),
        "extracted_records": _table_count(db, ExtractedRecord),
        "raw_snapshots": _table_count(db, RawSnapshot),
        "spine_jobs": _table_count(db, SpineJob),
    }
    admin_counts = {
        "workspaces": _table_count(db, Workspace),
        "users": _table_count(db, User),
        "api_keys": _table_count(db, ApiKey),
        "usage_records": _table_count(db, Usage),
        "audit_logs": _table_count(db, AdminAuditLog),
    }
    out = {
        "legacy": legacy_counts,
        "spine": spine_counts,
        "admin": admin_counts,
        "breakdowns": {
            "products_by_site": _count_by(db, Product, Product.site, limit=12),
            "reviews_by_platform": _count_by(db, Review, Review.platform, limit=12),
            "crawl_jobs_by_status": _count_by(db, CrawlJob, CrawlJob.status, limit=12),
            "ondemand_jobs_by_status": _count_by(db, OnDemandJob, OnDemandJob.status, limit=12),
            "spine_jobs_by_status": _count_by(db, SpineJob, SpineJob.status, limit=12),
            "records_by_quality": _count_by(db, ExtractedRecord,
                                             ExtractedRecord.quality_status, limit=12),
            "usage_by_endpoint": [
                {"key": endpoint if endpoint is not None else "null",
                 "calls": int(calls or 0),
                 "records": int(records or 0),
                 "credits": int(credits or 0)}
                for endpoint, calls, records, credits in (
                    db.query(Usage.endpoint,
                             func.count(Usage.id),
                             func.coalesce(func.sum(Usage.record_count), 0),
                             func.coalesce(func.sum(Usage.credits_used), 0))
                    .group_by(Usage.endpoint)
                    .order_by(func.count(Usage.id).desc())
                    .limit(12)
                    .all()
                )
            ],
        },
        "updated_at": datetime.utcnow().isoformat(),
        "cache_ttl_sec": _INVENTORY_CACHE_TTL if cached else 0,
    }
    if cached:
        _INVENTORY_CACHE = out
        _INVENTORY_CACHE_TS = now_ts
    return out


@router.get("/tenants")
def tenants(user: str = Depends(require_user),
            db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    rows = db.query(Workspace).order_by(Workspace.id.desc()).all()
    items = []
    for ws in rows:
        site_codes = [site for (site,) in (
            db.query(WorkspaceSite.site)
            .filter(WorkspaceSite.workspace_id == ws.id,
                    WorkspaceSite.enabled.is_(True),
                    WorkspaceSite.hidden.is_(False))
            .all()
        )]
        product_count = 0
        review_count = 0
        if site_codes:
            product_count = (db.query(func.count(Product.id))
                             .filter(Product.site.in_(site_codes)).scalar() or 0)
            review_count = (db.query(func.count(Review.id))
                            .filter(Review.site.in_(site_codes)).scalar() or 0)
        items.append({
            "id": ws.id,
            "name": ws.name,
            "slug": ws.slug,
            "type": ws.type,
            "status": ws.status,
            "created_at": ws.created_at.isoformat() if ws.created_at else None,
            "member_count": (db.query(func.count(WorkspaceMember.id))
                             .filter(WorkspaceMember.workspace_id == ws.id).scalar() or 0),
            "site_count": len(site_codes),
            "product_count": int(product_count),
            "review_count": int(review_count),
            "api_key_count": (db.query(func.count(ApiKey.id))
                              .filter(ApiKey.workspace_id == ws.id).scalar() or 0),
            "usage_credits": int((db.query(func.coalesce(func.sum(Usage.credits_used), 0))
                                  .filter(Usage.workspace_id == ws.id).scalar()) or 0),
            "spine_job_count": (db.query(func.count(SpineJob.id))
                                .filter(SpineJob.workspace_id == ws.id).scalar() or 0),
            "dataset_count": (db.query(func.count(Dataset.id))
                              .filter(Dataset.workspace_id == ws.id).scalar() or 0),
            "ondemand_job_count": (db.query(func.count(OnDemandJob.id))
                                   .filter(OnDemandJob.workspace_id == ws.id).scalar() or 0),
        })
    return {"items": items, "total": len(items)}


@router.get("/audit")
def audit_list(actor: str | None = None, action: str | None = None,
               start: str | None = None, end: str | None = None,
               page: int = 1, size: int = 20,
               user: str = Depends(require_user),
               db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = db.query(AdminAuditLog)
    if actor:
        q = q.filter(AdminAuditLog.actor_name == actor)
    if action:
        q = q.filter(AdminAuditLog.action == action)
    if start:
        q = q.filter(AdminAuditLog.created_at >= datetime.fromisoformat(start))
    if end:
        end_dt = datetime.fromisoformat(end)
        if len(end) == 10:
            end_dt = end_dt + timedelta(days=1)
            q = q.filter(AdminAuditLog.created_at < end_dt)
        else:
            q = q.filter(AdminAuditLog.created_at <= end_dt)
    total = q.count()
    rows = (q.order_by(AdminAuditLog.id.desc())
            .offset((page - 1) * size).limit(size).all())
    return {"total": total, "items": [
        {"id": r.id, "actor_name": r.actor_name, "action": r.action,
         "target_type": r.target_type, "target_id": r.target_id,
         "detail": r.detail, "ip": r.ip,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}
