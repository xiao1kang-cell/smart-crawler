"""超管后台 · spine 管理端点(队列/数据集/计费/健康/审计)。

全部经 _require_super_admin。写操作经 audit.record_audit 埋点。
与现有 routes.py 的 /api/admin/* 并列,不碰它们。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import spine_queue
from ..audit import record_audit
from ..db import get_db
from ..models import SpineJob
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
