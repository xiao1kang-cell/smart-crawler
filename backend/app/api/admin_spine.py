"""超管后台 · spine 管理端点(队列/数据集/计费/健康/审计)。

全部经 _require_super_admin。写操作经 audit.record_audit 埋点。
与现有 routes.py 的 /api/admin/* 并列,不碰它们。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import spine_queue
from ..audit import record_audit
from ..db import get_db
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
    Usage,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceSite,
)
from ..spine_queue import HEARTBEAT_INTERVAL, _backoff
from .routes import require_user, _build_data_quality_payload, _require_super_admin

router = APIRouter(prefix="/api/admin/spine", tags=["admin · spine"])

_STUCK_SEC = 600
_CRAWL_STUCK_SEC = 1800
_ONDEMAND_STUCK_SEC = 1800
_INVENTORY_CACHE_TTL = 30
_INVENTORY_CACHE: dict | None = None
_INVENTORY_CACHE_TS = 0.0


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


def _group_rows(query, *, limit: int = 20) -> list[dict]:
    rows = query.order_by(func.count().desc()).limit(limit).all()
    return [{"key": key if key is not None else "null", "count": int(n or 0)}
            for key, n in rows]


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
            CrawlJob.started_at < cutoff)


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
    total["by_queue"] = by_queue
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
            .filter(CrawlJob.status == "running")
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
    return {
        **_job_dict(j),
        "source": "spine",
        "source_label": "通用抓取",
        "raw_status": j.status,
        "normalized_status": _norm_status("spine", j.status, stuck=is_stuck),
        "target": j.dataset or j.url,
    }


def _crawl_job_dict(j: CrawlJob, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=_CRAWL_STUCK_SEC)
    is_stuck = (j.status == "running" and j.started_at is not None
                and j.started_at < cutoff)
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
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "heartbeat_at": None,
        "products_count": j.products_count or 0,
        "new_count": j.new_count or 0,
        "promotion_count": j.promotion_count or 0,
        "duration_sec": j.duration_sec,
        "failure_code": j.failure_code,
        "failure_stage": j.failure_stage,
        "failure_detail": j.failure_detail,
        "retryable": j.retryable,
        "suggested_action": j.suggested_action,
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
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "started_at": None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "heartbeat_at": None,
        "listing_count": j.listing_count or 0,
        "review_count": j.review_count or 0,
        "batch_id": j.batch_id,
        "attempts": j.attempts or 0,
        "notes": j.notes,
        "max_items": j.max_items,
        "review_limit": j.review_limit,
    }


def _queue_jobs_list(db: Session, *, status: str | None, dataset: str | None,
                     tenant: int | None, source: str, page: int, size: int,
                     failure_code: str | None = None) -> dict:
    source = (source or "all").lower()
    allowed_sources = {"all", "spine", "crawl", "ondemand"}
    if source not in allowed_sources:
        raise HTTPException(422, {"error": "unknown_job_source", "source": source})
    wanted = {s.strip() for s in (status or "").split(",") if s.strip()}
    now = datetime.utcnow()
    rows: list[dict] = []

    if source in ("all", "spine"):
        q = db.query(SpineJob)
        if dataset:
            needle = f"%{dataset}%"
            q = q.filter(or_(SpineJob.dataset == dataset, SpineJob.url.ilike(needle)))
        if tenant is not None:
            q = q.filter(SpineJob.workspace_id == tenant)
        if not failure_code:
            for job in q.order_by(SpineJob.id.desc()).all():
                row = _spine_job_dict(job, now=now)
                if not wanted or row["normalized_status"] in wanted:
                    rows.append(row)

    if source in ("all", "crawl"):
        q = db.query(CrawlJob)
        if dataset:
            q = q.filter(CrawlJob.site == dataset)
        if tenant is not None:
            q = q.filter(CrawlJob.requested_by_workspace_id == tenant)
        if failure_code:
            q = q.filter(CrawlJob.failure_code == failure_code)
        for job in q.order_by(CrawlJob.id.desc()).all():
            row = _crawl_job_dict(job, now=now)
            if not wanted or row["normalized_status"] in wanted:
                rows.append(row)

    if source in ("all", "ondemand"):
        q = db.query(OnDemandJob)
        if dataset:
            needle = f"%{dataset}%"
            q = q.filter((OnDemandJob.batch_id == dataset) |
                         (OnDemandJob.platform == dataset) |
                         (OnDemandJob.url.ilike(needle)))
        if tenant is not None:
            q = q.filter(OnDemandJob.workspace_id == tenant)
        if not failure_code:
            for job in q.order_by(OnDemandJob.id.desc()).all():
                row = _ondemand_job_dict(job, now=now)
                if not wanted or row["normalized_status"] in wanted:
                    rows.append(row)

    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    total = len(rows)
    start = max(0, (page - 1) * size)
    end = start + size
    return {"total": total, "items": rows[start:end]}


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
    q = (db.query(WorkspaceSite.site, WorkspaceSite.workspace_id, Workspace.name)
         .join(Workspace, Workspace.id == WorkspaceSite.workspace_id)
         .filter(WorkspaceSite.enabled.is_(True), Workspace.status == "active"))
    if tenant is not None:
        q = q.filter(WorkspaceSite.workspace_id == tenant)
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))

    site_workspace_rows = q.order_by(WorkspaceSite.site, WorkspaceSite.workspace_id).all()
    workspace_by_site: dict[str, list[dict]] = {}
    for site, workspace_id, workspace_name in site_workspace_rows:
        workspace_by_site.setdefault(site, []).append({
            "id": workspace_id,
            "name": workspace_name,
        })

    site_codes = sorted(workspace_by_site)
    sites = db.query(Site).filter(Site.site.in_(site_codes)).all() if site_codes else []
    payload = _build_data_quality_payload(db, sites)
    for item in payload["items"]:
        item["workspaces"] = workspace_by_site.get(item["site"], [])
    payload["summary"]["workspace_count"] = len({
        ws["id"] for rows in workspace_by_site.values() for ws in rows
    })
    payload["summary"]["tenant_id"] = tenant
    return payload


@router.post("/crawl/enqueue")
def admin_crawl_enqueue(
    payload: dict,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
    ip: str = Header(default="", alias="X-Forwarded-For"),
) -> dict:
    """后台按站点触发采集；复用已有 pending/running，避免重复入队。"""
    actor = _require_super_admin(user, db)
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
        by_site[site] = {"job_id": job_id, "status": "queued"}

    record_audit(db, actor_user_id=actor.id, actor_name=actor.username,
                 action="crawl.enqueue", target_type="site",
                 target_id=",".join(sites),
                 detail={"created_jobs": created, "existing_jobs": reused,
                         "promoted_jobs": promoted},
                 ip=ip or None)
    db.commit()
    return {
        "status": "queued" if created and not reused and not promoted else (
            "already_running" if reused and not created and not promoted else "mixed"
        ),
        "jobs": jobs,
        "created_jobs": created,
        "existing_jobs": reused,
        "promoted_jobs": promoted,
        "by_site": by_site,
        "count": len(jobs),
        "queued_at": datetime.utcnow().isoformat(),
    }


def _job_dict(j: SpineJob) -> dict:
    return {"id": j.id, "url": j.url, "dataset": j.dataset,
            "entity_type": j.entity_type, "status": j.status,
            "retries": j.retries, "max_retries": j.max_retries,
            "error": j.error, "worker": j.worker,
            "result_record_id": j.result_record_id,
            "workspace_id": j.workspace_id, "api_key_id": j.api_key_id,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "heartbeat_at": j.heartbeat_at.isoformat() if j.heartbeat_at else None}


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
        if j.status in ("pending", "running"):
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
        if j.status not in ("success", "partial", "failed"):
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
            }
            for row in endpoints
        ],
        "pools": [
            pool_dict(row,
                      members=pool_member_count.get(row.id, 0),
                      available=pool_available_count.get(row.slug, 0))
            for row in pool_configs
        ],
        "rules": [rule_dict(row) for row in rules],
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
