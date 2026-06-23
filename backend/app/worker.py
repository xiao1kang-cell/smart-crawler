"""采集 worker —— 轮询任务队列，执行采集任务。

两种用法：
  · 独立容器： python -m app.worker     （服务化部署，可起多副本）
  · 进程内线程：main.py 在单机模式下起一个 run_loop 守护线程
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import or_

from .analytics import recompute
from .crawl_diagnostics import classify_exception, job_timeout_failure, record_failure
from .db import session_scope
from .models import CrawlJob
from .runner import claim_job, execute_job
from . import memory_gate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [worker] %(message)s")
logger = logging.getLogger("smart-crawler.worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


WORKER_ID = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL = _env_int("WORKER_POLL", 10)
DEFAULT_JOB_TIMEOUT = _env_int("WORKER_JOB_TIMEOUT", 43200)  # 12h 默认
JOB_TIMEOUT_MIN = _env_int("WORKER_JOB_TIMEOUT_MIN", 300)
JOB_TIMEOUT_MAX = _env_int("WORKER_JOB_TIMEOUT_MAX", 86400)
STALE_HEARTBEAT_TIMEOUT = max(
    43200,
    _env_int("WORKER_STALE_HEARTBEAT_TIMEOUT", 43200),
)
JOB_HEARTBEAT_INTERVAL = float(os.environ.get("WORKER_JOB_HEARTBEAT_INTERVAL", "30"))
TRIGGER_ALLOWLIST = tuple(
    trigger.strip() for trigger in os.environ.get("TRIGGER_ALLOWLIST", "").split(",")
    if trigger.strip()
) or None


def _env_int_tuple(name: str) -> tuple[int, ...] | None:
    raw = os.environ.get(name, "")
    ids = tuple(int(x.strip()) for x in raw.split(",") if x.strip().isdigit())
    return ids or None


WORKSPACE_ALLOWLIST = _env_int_tuple("WORKSPACE_ALLOWLIST")
WORKSPACE_BLOCKLIST = _env_int_tuple("WORKSPACE_BLOCKLIST")
# 内存自适应并发闸 —— 主机已用内存超阈值则暂停领新 job。设 0/100 关闸。
MEM_THRESHOLD = float(os.environ.get("MEM_GATE_THRESHOLD", "80"))
MEM_CHECK_INTERVAL = float(os.environ.get("MEM_GATE_CHECK_INTERVAL", "2"))
MEM_MAX_WAIT = float(os.environ.get("MEM_GATE_MAX_WAIT", "300"))
_running = True


class JobTimeout(Exception):
    """单条 job 超时（worker hang 在死代理上的兜底）。"""


def _alarm_handler(signum, frame):
    raise JobTimeout("job exceeded runtime budget")


def _set_alarm(seconds: int) -> None:
    """signal.alarm 只能在主线程调用；in-process worker 跑在守护线程时跳过。"""
    try:
        signal.alarm(seconds)
    except (ValueError, AttributeError):
        pass


def _bounded_timeout(value: int | None) -> int:
    try:
        seconds = int(value or DEFAULT_JOB_TIMEOUT)
    except (TypeError, ValueError):
        seconds = DEFAULT_JOB_TIMEOUT
    return max(JOB_TIMEOUT_MIN, min(JOB_TIMEOUT_MAX, seconds))


def _trigger_timeout(trigger: str | None) -> int:
    key = (trigger or "").strip().upper()
    if key:
        env = os.environ.get(f"WORKER_JOB_TIMEOUT_{key}")
        if env:
            return _bounded_timeout(env)
    if trigger in {"manual", "admin_retry", "admin_quality_rerun", "tracking_add"}:
        env = os.environ.get("WORKER_JOB_TIMEOUT_INTERACTIVE")
        if env:
            return _bounded_timeout(env)
    if trigger in {"scheduled", "daily_refresh"}:
        env = os.environ.get("WORKER_JOB_TIMEOUT_SCHEDULED")
        if env:
            return _bounded_timeout(env)
    return _bounded_timeout(DEFAULT_JOB_TIMEOUT)


def _job_runtime_budget(job_id: int) -> int:
    """Return the max runtime for a job.

    Large sites can set any of these in Site.crawler_config:
    job_timeout_sec / worker_timeout_sec / max_runtime_sec.
    """
    try:
        from .models import Site

        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            if job is None:
                return _bounded_timeout(DEFAULT_JOB_TIMEOUT)
            site = s.query(Site).filter(Site.site == job.site).first()
            cfg = site.crawler_config if site and isinstance(site.crawler_config, dict) else {}
            for key in ("job_timeout_sec", "worker_timeout_sec", "max_runtime_sec"):
                if cfg.get(key) is not None:
                    return _bounded_timeout(int(cfg[key]))
            return _trigger_timeout(job.trigger)
    except Exception as exc:
        logger.warning("读取 job runtime budget 失败 job=%s: %s", job_id, exc)
        return _bounded_timeout(DEFAULT_JOB_TIMEOUT)


def _mark_job_timeout(job_id: int, timeout_sec: int) -> None:
    """标 job 为 failed（worker 自我兜底，不依赖 clean_stale daemon）。"""
    try:
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            if job and job.status == "running":
                detail = f"worker runtime budget exceeded {timeout_sec}s"
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                if job.started_at:
                    job.duration_sec = (
                        job.finished_at - job.started_at).total_seconds()
                job.error = detail
                record_failure(
                    s,
                    site=job.site,
                    job_id=job_id,
                    info=job_timeout_failure(job.site, timeout_sec, detail),
                )
    except Exception as exc:
        logger.error("mark_job_timeout 失败 job=%s: %s", job_id, exc)


def _mark_job_failed(job_id: int, exc: Exception) -> None:
    """worker 兜底失败落库，避免未知平台等早期异常留下 running。"""
    try:
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            if job and job.status in {"pending", "running"}:
                info = classify_exception(exc)
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                if job.started_at:
                    job.duration_sec = (
                        job.finished_at - job.started_at).total_seconds()
                job.error = f"worker exception: {type(exc).__name__}: {exc}"
                record_failure(s, site=job.site, job_id=job_id, info=info)
    except Exception as mark_exc:
        logger.error("mark_job_failed 失败 job=%s: %s", job_id, mark_exc)


def _start_crawl_job_heartbeat(job_id: int, interval: float | None = None):
    """续约 crawl_jobs.heartbeat_at，避免活着的长任务被误判为卡死。"""
    interval = interval or JOB_HEARTBEAT_INTERVAL
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                with session_scope() as s:
                    job = s.get(CrawlJob, job_id)
                    if job and job.status == "running":
                        job.heartbeat_at = datetime.utcnow()
            except Exception:
                pass

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return stop, thread


def _reclaim_stale_crawl_jobs(timeout_sec: int = STALE_HEARTBEAT_TIMEOUT) -> int:
    """Fail running crawl jobs whose worker heartbeat stopped.

    This covers in-process worker threads where signal.alarm is unavailable and
    stale rows left behind by crashed worker containers.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=timeout_sec)
    try:
        with session_scope() as s:
            rows = (s.query(CrawlJob)
                    .filter(CrawlJob.status == "running",
                            CrawlJob.started_at.isnot(None),
                            CrawlJob.started_at < cutoff,
                            or_(CrawlJob.heartbeat_at.is_(None),
                                CrawlJob.heartbeat_at < cutoff))
                    .all())
            for job in rows:
                detail = f"auto-canceled: worker heartbeat stale >{timeout_sec}s"
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                job.duration_sec = (
                    job.finished_at - job.started_at).total_seconds()
                job.error = detail
                record_failure(
                    s,
                    site=job.site,
                    job_id=job.id,
                    info=job_timeout_failure(job.site, timeout_sec, detail),
                )
            return len(rows)
    except Exception as exc:
        logger.error("reclaim stale crawl jobs 失败: %s", exc)
        return 0


def _repair_missing_failure_diagnostics(limit: int = 200) -> int:
    """Backfill structured diagnostics for failed/blocked jobs created before
    failure_code existed or by old cleanup scripts.
    """
    try:
        with session_scope() as s:
            rows = (s.query(CrawlJob)
                    .filter(CrawlJob.status.in_(("failed", "blocked")),
                            or_(CrawlJob.failure_code.is_(None),
                                CrawlJob.failure_code == "unknown"))
                    .order_by(CrawlJob.id.desc())
                    .limit(limit)
                    .all())
            repaired = 0
            for job in rows:
                detail = job.error or f"{job.status} without structured diagnostic"
                info = classify_exception(RuntimeError(detail))
                if job.failure_code == info.code:
                    continue
                record_failure(s, site=job.site, job_id=job.id, info=info)
                repaired += 1
            return repaired
    except Exception as exc:
        logger.error("repair missing failure diagnostics 失败: %s", exc)
        return 0


def run_loop(should_continue=None) -> None:
    """领取并执行队列任务，直到 should_continue() 为假。"""
    should_continue = should_continue or (lambda: _running)
    # 注册超时 alarm handler（main thread only —— worker.py 主线程跑没问题）
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
    except ValueError:
        # 非主线程（in-process mode）—— alarm 不可用，靠 clean_stale daemon 兜底
        pass
    trigger_scope = ",".join(TRIGGER_ALLOWLIST) if TRIGGER_ALLOWLIST else "all"
    logger.info(
        "worker %s 启动，轮询间隔 %ds，默认运行预算 %ds，心跳卡死阈值 %ds，trigger=%s",
        WORKER_ID, POLL_INTERVAL, DEFAULT_JOB_TIMEOUT,
        STALE_HEARTBEAT_TIMEOUT, trigger_scope)
    while should_continue():
        reclaimed = _reclaim_stale_crawl_jobs(STALE_HEARTBEAT_TIMEOUT)
        if reclaimed:
            logger.warning("回收 %d 个超时 crawl job", reclaimed)
        # 内存安全闸:已用内存超阈值则暂停领新 job(不起新浏览器),
        # 内存回落自动恢复。超时回循环重判,绝不在内存高位硬领。
        if not memory_gate.wait_until_ok(
                MEM_THRESHOLD, check_interval=MEM_CHECK_INTERVAL,
                max_wait=MEM_MAX_WAIT, should_continue=should_continue):
            # 闸返回 False 有两种:内存仍高 / 正在停机。仅"内存仍高"时记一条
            # 并 sleep 一拍——让"无 job 活动"可区分于"被内存闸暂停",同时防
            # MEM_MAX_WAIT=0 时空转。停机(内存已回落)则直接 continue,快速退出。
            if memory_gate.used_percent() >= MEM_THRESHOLD:
                logger.warning("内存闸暂停领新 job:已用 %.0f%% ≥ 阈值 %.0f%%",
                               memory_gate.used_percent(), MEM_THRESHOLD)
                time.sleep(POLL_INTERVAL)
            continue
        try:
            job_id = claim_job(WORKER_ID, TRIGGER_ALLOWLIST,
                               workspace_allowlist=WORKSPACE_ALLOWLIST,
                               workspace_blocklist=WORKSPACE_BLOCKLIST)
        except Exception as exc:
            logger.error("领取任务失败: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue
        if job_id is None:
            time.sleep(POLL_INTERVAL)
            continue
        runtime_budget = _job_runtime_budget(job_id)
        try:
            _set_alarm(runtime_budget)
            stop_heartbeat, heartbeat_thread = _start_crawl_job_heartbeat(job_id)
            try:
                result = execute_job(job_id)
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2)
                _set_alarm(0)
            if result["status"] == "success":
                recompute(result["site"])
            logger.info("job %s %s -> %s", job_id, result["site"],
                        result["status"])
        except JobTimeout as exc:
            _set_alarm(0)
            _mark_job_timeout(job_id, runtime_budget)
            logger.warning("job %s 超时: %s", job_id, exc)
        except Exception as exc:
            _set_alarm(0)
            _mark_job_failed(job_id, exc)
            logger.error("job %s 执行异常: %s", job_id, exc)
    logger.info("worker %s 退出", WORKER_ID)


def _stop(*_):
    global _running
    _running = False


def main() -> None:
    from .db import IS_SQLITE, init_db
    if IS_SQLITE or os.environ.get("WORKER_RUN_INIT_DB") == "1":
        init_db()
    repaired = _repair_missing_failure_diagnostics()
    if repaired:
        logger.info("补齐 %d 个历史失败任务的结构化诊断", repaired)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_loop()


if __name__ == "__main__":
    main()
