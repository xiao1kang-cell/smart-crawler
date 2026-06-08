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

from ..db import session_scope
from ..models import OnDemandJob

logger = logging.getLogger(__name__)

_q: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

# 终态(可被重试) / 未完成态(占用并发闸)
TERMINAL = ("success", "partial", "failed")
PENDING = ("queued", "running")


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
        job.listing_count = len(listings)
        job.review_count = len(reviews)
        job.notes = notes
        job.item_skus = skus
        job.error = error


def _run_loop() -> None:
    while True:
        job_id = _q.get()
        try:
            process_one(job_id)
        except Exception:                          # 兜底,绝不让循环退出
            logger.exception("worker loop error on job %s", job_id)
        finally:
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
            ids.append(r.id)
    for jid in ids:
        enqueue(jid)
    return len(ids)
