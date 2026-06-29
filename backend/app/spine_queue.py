"""Spine 异步抓取队列 —— 任意 URL 入队,worker 消费走 spine.resolve 落库。

队列即 spine_jobs 表(镜像 runner.py 的乐观锁模式):
  enqueue()     —— 入队一条 pending(REST / MCP / 内部调用)
  claim_job()   —— worker 原子领取最旧的、到期的 pending 任务
  execute_job() —— 执行已领取的任务:spine.resolve 落库 → 成功/重试/失败
与电商 crawl_jobs 完全独立。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .db import session_scope
from .models import SpineJob


def _enqueue_spine_webhook(s: Session, job: SpineJob | None, *,
                           event_type: str,
                           result: dict | None = None,
                           error: str | None = None) -> None:
    if job is None:
        return
    try:
        from .webhooks import enqueue_delivery

        enqueue_delivery(
            s,
            workspace_id=job.workspace_id,
            event_type=event_type,
            job_kind="spine",
            job_id=job.id,
            status=job.status or "pending",
            created_at=job.created_at,
            finished_at=job.finished_at,
            error=error if error is not None else job.error,
            result={
                "url": job.url,
                "dataset": job.dataset,
                "entity_type": job.entity_type,
                "save_policy": job.save_policy,
                "result_record_id": job.result_record_id,
                "retries": job.retries or 0,
                **(result or {}),
            },
        )
    except Exception:
        pass


def enqueue(db: Session, url: str, dataset: str, *,
            entity_type: str = "generic",
            save_policy: str = "promote_if_valid",
            force_live: bool = False, max_retries: int = 3,
            api_key_id: int | None = None,
            workspace_id: int | None = None) -> int:
    """入队一条 spine 抓取任务,返回 job_id。调用方负责 commit。"""
    job = SpineJob(url=url, dataset=dataset, entity_type=entity_type,
                   save_policy=save_policy, force_live=force_live,
                   status="pending", retries=0, max_retries=max_retries,
                   next_attempt_at=datetime.utcnow(), api_key_id=api_key_id,
                   workspace_id=workspace_id, created_at=datetime.utcnow())
    db.add(job)
    db.flush()
    _enqueue_spine_webhook(db, job, event_type="job.triggered")
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
                    started_at=now, heartbeat_at=now))
        return job.id if res.rowcount == 1 else None


def _backoff(retries: int) -> timedelta:
    """指数退避:1→30s, 2→2m, 3→10m, 之后封顶 10m。"""
    table = {1: 30, 2: 120, 3: 600}
    return timedelta(seconds=table.get(retries, 600))


HEARTBEAT_INTERVAL = 30.0


def _start_heartbeat(job_id: int, interval: float | None = None):
    """起一个后台线程,每 interval 秒把 job.heartbeat_at 续约为 now。

    返回 (stop_event, thread)。execute 结束时 stop.set() + join 停掉。
    让 reclaim 能区分"活着的长抓(心跳在续)"和"真崩溃(心跳停)"。

    interval 默认 None 时在运行时读模块级 HEARTBEAT_INTERVAL(而非 def 时
    绑定),便于测试 monkeypatch 模块级值压短心跳间隔。
    """
    if interval is None:
        interval = HEARTBEAT_INTERVAL
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            try:
                with session_scope() as s:
                    j = s.get(SpineJob, job_id)
                    if j is not None:
                        j.heartbeat_at = datetime.utcnow()
            except Exception:
                pass  # 续约失败不影响主执行

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return stop, t


def execute_job(job_id: int) -> dict:
    """执行一条已领取(running)的任务:spine.resolve 落库 → 成功/重试/失败。

    注意:spine.resolve 内部自行提交落库(dataset/snapshot/record);job 状态
    另由本函数的 session_scope 提交,二者非原子。极窄崩溃窗口下可能留下卡在
    running 的悬挂 job —— 由 spine_worker 的 running 超时回收兜底,期间靠心跳
    续约区分活着的长抓 vs 真崩溃。
    """
    from . import spine
    stop, t = _start_heartbeat(job_id)
    try:
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
            api_key_id = job.api_key_id
            try:
                ds = spine.get_or_create_dataset(
                    s, dataset_name, workspace_id=workspace_id,
                    entity_type=entity_type)
                out = spine.resolve(s, url, ds, workspace_id=workspace_id,
                                    force_live=force_live, save_policy=save_policy)
                _record_execute_usage(api_key_id, workspace_id, out)
                job.status = "success"
                job.result_record_id = out.get("record_id")
                job.finished_at = datetime.utcnow()
                job.error = None
                _enqueue_spine_webhook(
                    s,
                    job,
                    event_type="job.completed",
                    result={"record_id": out.get("record_id")},
                )
                return {"job_id": job_id, "status": "success",
                        "record_id": out.get("record_id")}
            except Exception as exc:
                return _handle_failure(s, job, exc)
    finally:
        stop.set()
        t.join(timeout=2)


def _record_execute_usage(api_key_id, workspace_id, out) -> None:
    """成功落库后按 resolve 的 credits_used 记账(精确到 key)。失败路径不调本函数。"""
    from .billing import record_usage
    try:
        record_usage(api_key_id=api_key_id, endpoint="/spine/worker/execute",
                     record_count=1, bytes_returned=0, duration_ms=0,
                     credits_used=int(out.get("credits_used") or 0),
                     workspace_id=workspace_id,
                     api_calls=int(out.get("api_calls") or 0),
                     browser_opens=int(out.get("browser_opens") or 0),
                     pages_fetched=int(out.get("api_calls") or 0)
                     + int(out.get("browser_opens") or 0))
    except Exception:
        # 计费绝不阻断 worker 落库(与同步 _meter 容错一致)
        pass


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
    _enqueue_spine_webhook(s, job, event_type="job.completed",
                           error=job.error)
    return {"job_id": job.id, "status": "failed", "retries": job.retries}


def reclaim_stale_jobs(running_timeout_sec: int = 600) -> int:
    """把心跳停超 running_timeout_sec 的 running job 重置为 pending,返回回收条数。

    判据用 heartbeat_at(worker execute 期间每 HEARTBEAT_INTERVAL 续约):
    只有真崩溃/卡死(心跳停了)才被回收;活着的长抓持续续约,不会被误回收。
    heartbeat_at IS NULL(刚领还没续约的脏行)一并回收。worker loop 每轮先调。
    """
    cutoff = datetime.utcnow() - timedelta(seconds=running_timeout_sec)
    with session_scope() as s:
        stale = (s.query(SpineJob)
                 .filter(SpineJob.status == "running",
                         or_(SpineJob.heartbeat_at < cutoff,
                             SpineJob.heartbeat_at.is_(None)))
                 .all())
        for job in stale:
            job.status = "pending"
            job.worker = None
            job.next_attempt_at = datetime.utcnow()
        return len(stale)
