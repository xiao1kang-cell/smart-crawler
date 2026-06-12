"""Spine 异步抓取队列 —— 任意 URL 入队,worker 消费走 spine.resolve 落库。

队列即 spine_jobs 表(镜像 runner.py 的乐观锁模式):
  enqueue()     —— 入队一条 pending(REST / MCP / 内部调用)
  claim_job()   —— worker 原子领取最旧的、到期的 pending 任务
  execute_job() —— 执行已领取的任务:spine.resolve 落库 → 成功/重试/失败
与电商 crawl_jobs 完全独立。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
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
