"""按需抓取的进程内队列 + 单 worker 线程(串行执行)。

为什么自建而非复用 scheduler/runner.enqueue:那套是"按站点定时采集"语义
(Job 模型 + site-name),与 URL 级按需抓取错配;且服务化部署 RUN_SCHEDULER=0
时整套不启动,本地批量会直接失效。

设计:
  · 模块级 queue.Queue[int](存 OnDemandJob.id)。
  · 懒启动的常驻 daemon 线程:首次 enqueue 时拉起,循环取一条 → 跑 → 取下一条。
    单线程天然串行,契合反爬限速/代理配额。
  · process_one(job_id):置 running、attempts+1 → 调 runner.fetch → 原地更新结果。
  · requeue_pending():进程重启后把残留 queued/running 的 job 重新入队
    (内存队列在重启时丢失)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from ..db import session_scope
from ..models import OnDemandJob

logger = logging.getLogger(__name__)

# worker 读不到 job 时的短重试(防御未提交可见性,正常路径用不到)
_READ_RETRY = 5
_READ_RETRY_DELAY = 0.3

_q: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

# 终态(可被重试) / 未完成态(占用并发闸)
TERMINAL = ("success", "partial", "failed")
PENDING = ("queued", "running")


def _enqueue_ondemand_webhook(s, job: OnDemandJob | None, *,
                              event_type: str,
                              result: dict | None = None,
                              error: str | None = None) -> None:
    if job is None:
        return
    try:
        from ..webhooks import enqueue_delivery

        enqueue_delivery(
            s,
            workspace_id=job.workspace_id,
            event_type=event_type,
            job_kind="ondemand",
            job_id=job.id,
            status=job.status or "queued",
            created_at=job.created_at,
            finished_at=job.finished_at,
            error=error if error is not None else job.error,
            result={
                "url": job.url,
                "platform": job.platform,
                "kind": job.kind,
                "batch_id": job.batch_id,
                "listing_count": job.listing_count or 0,
                "review_count": job.review_count or 0,
                "attempts": job.attempts or 0,
                "notes": job.notes or [],
                "item_skus": job.item_skus or [],
                **(result or {}),
            },
        )
    except Exception as exc:
        logger.warning("enqueue ondemand webhook failed job=%s: %s",
                       getattr(job, "id", None), exc)


def _dispatch_webhooks() -> None:
    try:
        from ..webhooks import dispatch_pending

        with session_scope() as s:
            dispatch_pending(s, limit=10)
    except Exception as exc:
        logger.warning("dispatch ondemand webhook failed: %s", exc)


def _status_of(listing_count: int, review_count: int, notes: list) -> str:
    """与 ondemand_jobs.record_job 同口径:无数据=failed、有数据带 notes=partial。"""
    if listing_count == 0 and review_count == 0:
        return "failed"
    if notes:
        return "partial"
    return "success"


def process_one(job_id: int) -> None:
    """执行一条 job:渲染抓取 → 原地更新该行。异常被隔离为 failed。"""
    from . import runner

    # Defense-in-depth:正常路径下入队已在 commit 之后,worker 必能读到。
    # 但万一仍遇上未提交可见性(如 PG 复制延迟),短重试几次再放弃,
    # 避免把"暂时读不到"误判为"任务不存在"而静默卡死。
    job = None
    for attempt in range(_READ_RETRY):
        with session_scope() as s:
            job = s.get(OnDemandJob, job_id)
            if job is not None:
                break
        time.sleep(_READ_RETRY_DELAY)
    if job is None:
        logger.error("ondemand job %s 始终读不到,放弃执行(疑似入队早于提交)",
                     job_id)
        return

    with session_scope() as s:
        job = s.get(OnDemandJob, job_id)
        if job is None:
            return
        job.status = "running"
        job.attempts = (job.attempts or 0) + 1
        url = job.url
        max_items = job.max_items or 100
        review_limit = job.review_limit or 100
        # 提前提交 running 状态,让前端轮询可见"执行中"
        s.flush()

    try:
        res = runner.fetch(url, max_items=max_items, review_limit=review_limit,
                           do_persist=True)
        listings = list(res.listings)
        reviews = list(res.reviews)
        notes = list(res.notes or [])
        skus = [l.get("sku") for l in listings if l.get("sku")]
        status = _status_of(len(listings), len(reviews), notes)
        error = None if status != "failed" else "; ".join(notes) or "抓取失败"
    except Exception as exc:                       # 失败隔离,不让 worker 线程崩
        logger.exception("ondemand job %s failed", job_id)
        listings, reviews, notes, skus = [], [], [], []
        status, error = "failed", f"{type(exc).__name__}: {exc}"

    with session_scope() as s:
        job = s.get(OnDemandJob, job_id)
        if job is None:
            return
        job.status = status
        job.finished_at = datetime.utcnow()
        job.listing_count = len(listings)
        job.review_count = len(reviews)
        job.notes = notes
        job.item_skus = skus
        job.error = error
        _enqueue_ondemand_webhook(s, job, event_type="job.completed",
                                  error=error)


def _run_loop() -> None:
    while True:
        _dispatch_webhooks()
        job_id = _q.get()
        try:
            process_one(job_id)
        except Exception:                          # 兜底,绝不让循环退出
            logger.exception("worker loop error on job %s", job_id)
        finally:
            _dispatch_webhooks()
            _q.task_done()


def ensure_worker() -> None:
    """保证 worker 线程已启动(懒启动,幂等)。"""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_loop, daemon=True,
                                   name="ondemand-worker")
        _worker.start()


def enqueue(job_id: int) -> None:
    """把一条 job 加入队列,并确保 worker 在跑。"""
    with session_scope() as s:
        _enqueue_ondemand_webhook(s, s.get(OnDemandJob, job_id),
                                  event_type="job.triggered")
    ensure_worker()
    _q.put(job_id)


def requeue_pending() -> int:
    """进程启动时把残留 queued/running 的 job 重新入队。返回条数。

    running 表示上次进程在抓取中途被杀,job 卡住 → 重置回 queued 再入队。
    """
    ids: list[int] = []
    with session_scope() as s:
        rows = (s.query(OnDemandJob)
                .filter(OnDemandJob.status.in_(PENDING)).all())
        for r in rows:
            r.status = "queued"
            r.finished_at = None
            ids.append(r.id)
    for jid in ids:
        enqueue(jid)
    return len(ids)
