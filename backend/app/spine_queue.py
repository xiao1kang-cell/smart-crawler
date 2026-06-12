"""Spine 异步抓取队列 —— 任意 URL 入队,worker 消费走 spine.resolve 落库。

队列即 spine_jobs 表(镜像 runner.py 的乐观锁模式):
  enqueue()     —— 入队一条 pending(REST / MCP / 内部调用)
  claim_job()   —— worker 原子领取最旧的、到期的 pending 任务
  execute_job() —— 执行已领取的任务:spine.resolve 落库 → 成功/重试/失败
与电商 crawl_jobs 完全独立。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .db import session_scope
from .models import SpineJob


def enqueue(db: Session, url: str, dataset: str, *,
            entity_type: str = "generic",
            save_policy: str = "promote_if_valid",
            force_live: bool = False, max_retries: int = 3,
            workspace_id: int | None = None) -> int:
    """入队一条 spine 抓取任务,返回 job_id。调用方负责 commit。"""
    job = SpineJob(url=url, dataset=dataset, entity_type=entity_type,
                   save_policy=save_policy, force_live=force_live,
                   status="pending", retries=0, max_retries=max_retries,
                   next_attempt_at=datetime.utcnow(), workspace_id=workspace_id,
                   created_at=datetime.utcnow())
    db.add(job)
    db.flush()
    return job.id


def claim_job(worker_id: str) -> int | None:
    """worker 原子领取最旧的、next_attempt_at<=now 的 pending 任务。

    乐观锁:仅当仍为 pending 时领取,防多 worker 抢同一任务。返回 job_id 或 None。
    """
    with session_scope() as s:
        now = datetime.utcnow()
        job = (s.query(SpineJob)
               .filter(SpineJob.status == "pending",
                       SpineJob.next_attempt_at <= now)
               .order_by(SpineJob.id).first())
        if job is None:
            return None
        res = s.execute(
            update(SpineJob)
            .where(SpineJob.id == job.id, SpineJob.status == "pending")
            .values(status="running", worker=worker_id,
                    started_at=now))
        return job.id if res.rowcount == 1 else None


def _backoff(retries: int) -> timedelta:
    """指数退避:1→30s, 2→2m, 3→10m, 之后封顶 10m。"""
    table = {1: 30, 2: 120, 3: 600}
    return timedelta(seconds=table.get(retries, 600))


def execute_job(job_id: int) -> dict:
    """执行一条已领取(running)的任务:spine.resolve 落库 → 成功/重试/失败。

    注意:spine.resolve 内部自行提交落库(dataset/snapshot/record);job 状态
    另由本函数的 session_scope 提交,二者非原子。极窄崩溃窗口下可能留下卡在
    running 的悬挂 job —— 由 spine_worker 的 running 超时回收兜底(Task 4)。
    """
    from . import spine
    with session_scope() as s:
        job = s.get(SpineJob, job_id)
        if job is None:
            raise ValueError(f"任务不存在: {job_id}")
        url = job.url
        dataset_name = job.dataset
        entity_type = job.entity_type or "generic"
        save_policy = job.save_policy or "promote_if_valid"
        force_live = bool(job.force_live)
        workspace_id = job.workspace_id
        try:
            ds = spine.get_or_create_dataset(
                s, dataset_name, workspace_id=workspace_id,
                entity_type=entity_type)
            out = spine.resolve(s, url, ds, workspace_id=workspace_id,
                                force_live=force_live, save_policy=save_policy)
            job.status = "success"
            job.result_record_id = out.get("record_id")
            job.finished_at = datetime.utcnow()
            job.error = None
            return {"job_id": job_id, "status": "success",
                    "record_id": out.get("record_id")}
        except Exception as exc:
            return _handle_failure(s, job, exc)


def _handle_failure(s: Session, job: SpineJob, exc: Exception) -> dict:
    """失败处理:未超限 → 回 pending + 退避;超限 → failed。"""
    job.retries = (job.retries or 0) + 1
    job.error = str(exc)
    if job.retries < (job.max_retries or 3):
        job.status = "pending"
        job.worker = None
        job.next_attempt_at = datetime.utcnow() + _backoff(job.retries)
        return {"job_id": job.id, "status": "pending", "retries": job.retries}
    job.status = "failed"
    job.finished_at = datetime.utcnow()
    return {"job_id": job.id, "status": "failed", "retries": job.retries}


def reclaim_stale_jobs(running_timeout_sec: int = 600) -> int:
    """把卡在 running 且超时的 job 重置为 pending,返回回收条数。

    兜底 execute_job 非原子提交的崩溃窗口:进程若在 resolve 落库后、写 job
    状态前崩溃,会留下永久 running 的悬挂 job(claim 只领 pending,不会重领)。
    worker loop 每轮先调本函数。
    """
    cutoff = datetime.utcnow() - timedelta(seconds=running_timeout_sec)
    with session_scope() as s:
        stale = (s.query(SpineJob)
                 .filter(SpineJob.status == "running",
                         or_(SpineJob.started_at < cutoff,
                             SpineJob.started_at.is_(None)))
                 .all())
        for job in stale:
            job.status = "pending"
            job.worker = None
            job.next_attempt_at = datetime.utcnow()
        return len(stale)
