"""超管后台 · spine 管理端点(队列/数据集/计费/健康/审计)。

全部经 _require_super_admin。写操作经 audit.record_audit 埋点。
与现有 routes.py 的 /api/admin/* 并列,不碰它们。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import spine_queue
from ..audit import record_audit
from ..db import get_db
from ..models import SpineJob, Dataset, ExtractedRecord, RawSnapshot, Usage, AdminAuditLog
from ..spine_queue import HEARTBEAT_INTERVAL, _backoff
from .routes import require_user, _require_super_admin

router = APIRouter(prefix="/api/admin/spine", tags=["admin · spine"])

_STUCK_SEC = 600


@router.get("/jobs/stats")
def jobs_stats(user: str = Depends(require_user),
               db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    counts = {st: db.query(SpineJob).filter(SpineJob.status == st).count()
              for st in ("pending", "running", "success", "failed")}
    cutoff = datetime.utcnow() - timedelta(seconds=_STUCK_SEC)
    counts["stuck"] = (db.query(SpineJob)
                       .filter(SpineJob.status == "running",
                               SpineJob.heartbeat_at < cutoff).count())
    return counts


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
              tenant: int | None = None, page: int = 1, size: int = 20,
              user: str = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    q = db.query(SpineJob)
    if status:
        q = q.filter(SpineJob.status == status)
    if dataset:
        q = q.filter(SpineJob.dataset == dataset)
    if tenant is not None:
        q = q.filter(SpineJob.workspace_id == tenant)
    total = q.count()
    rows = (q.order_by(SpineJob.id.desc())
            .offset((page - 1) * size).limit(size).all())
    return {"total": total, "items": [_job_dict(j) for j in rows]}


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, user: str = Depends(require_user),
               db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    j = db.get(SpineJob, job_id)
    if j is None:
        raise HTTPException(404, {"error": "job_not_found", "job_id": job_id})
    return _job_dict(j)


@router.post("/jobs/{job_id}/retry")
def job_retry(job_id: int, user: str = Depends(require_user),
              db: Session = Depends(get_db),
              ip: str = Header(default="", alias="X-Forwarded-For")) -> dict:
    actor = _require_super_admin(user, db)
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
        q = q.filter(Usage.occurred_at <= datetime.fromisoformat(end))
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
    return {"total_credits": int(total_credits or 0),
            "total_records": int(total_records or 0),
            "rows": q.count()}


@router.get("/usage/by-key")
def usage_by_key(user: str = Depends(require_user),
                 db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    rows = (db.query(Usage.api_key_id,
                     func.sum(Usage.credits_used),
                     func.count(Usage.id))
            .group_by(Usage.api_key_id).all())
    return {"items": [{"api_key_id": k, "credits": int(c or 0), "calls": n}
                      for k, c, n in rows]}


@router.get("/usage/by-tenant")
def usage_by_tenant(user: str = Depends(require_user),
                    db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    rows = (db.query(Usage.workspace_id,
                     func.sum(Usage.credits_used),
                     func.count(Usage.id))
            .group_by(Usage.workspace_id).all())
    return {"items": [{"workspace_id": w, "credits": int(c or 0), "calls": n}
                      for w, c, n in rows]}


@router.get("/health")
def health(user: str = Depends(require_user),
           db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    last_hb = db.query(func.max(SpineJob.heartbeat_at)).scalar()
    last_success = (db.query(func.max(SpineJob.finished_at))
                    .filter(SpineJob.status == "success").scalar())
    recent = None
    for t in (last_hb, last_success):
        if t and (recent is None or t > recent):
            recent = t
    if recent is None:
        status = "unknown"
    elif (datetime.utcnow() - recent).total_seconds() <= _STUCK_SEC:
        status = "running"
    else:
        status = "idle"
    stuck = (db.query(SpineJob)
             .filter(SpineJob.status == "running",
                     SpineJob.heartbeat_at < datetime.utcnow() - timedelta(seconds=_STUCK_SEC))
             .count())
    return {"worker_status": status,
            "last_activity_at": recent.isoformat() if recent else None,
            "reclaim_hint": {"stuck_running": stuck},
            "pending": db.query(SpineJob).filter(SpineJob.status == "pending").count()}


@router.get("/config")
def config(user: str = Depends(require_user),
           db: Session = Depends(get_db)) -> dict:
    _require_super_admin(user, db)
    return {"heartbeat_interval": HEARTBEAT_INTERVAL,
            "stuck_timeout_sec": _STUCK_SEC,
            "backoff": {str(i): int(_backoff(i).total_seconds()) for i in (1, 2, 3)}}


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
        q = q.filter(AdminAuditLog.created_at <= datetime.fromisoformat(end))
    total = q.count()
    rows = (q.order_by(AdminAuditLog.id.desc())
            .offset((page - 1) * size).limit(size).all())
    return {"total": total, "items": [
        {"id": r.id, "actor_name": r.actor_name, "action": r.action,
         "target_type": r.target_type, "target_id": r.target_id,
         "detail": r.detail, "ip": r.ip,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}
