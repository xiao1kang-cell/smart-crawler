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
import time
from datetime import datetime

from .analytics import recompute
from .db import session_scope
from .models import CrawlJob
from .runner import claim_job, execute_job
from . import memory_gate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [worker] %(message)s")
logger = logging.getLogger("smart-crawler.worker")

WORKER_ID = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL = int(os.environ.get("WORKER_POLL", "10"))
JOB_TIMEOUT = int(os.environ.get("WORKER_JOB_TIMEOUT", "1800"))  # 30 min 默认
# 内存自适应并发闸 —— 主机已用内存超阈值则暂停领新 job。设 0/100 关闸。
MEM_THRESHOLD = float(os.environ.get("MEM_GATE_THRESHOLD", "80"))
MEM_CHECK_INTERVAL = float(os.environ.get("MEM_GATE_CHECK_INTERVAL", "2"))
MEM_MAX_WAIT = float(os.environ.get("MEM_GATE_MAX_WAIT", "300"))
_running = True


class JobTimeout(Exception):
    """单条 job 超时（worker hang 在死代理上的兜底）。"""


def _alarm_handler(signum, frame):
    raise JobTimeout(f"job exceeded {JOB_TIMEOUT}s")


def _set_alarm(seconds: int) -> None:
    """signal.alarm 只能在主线程调用；in-process worker 跑在守护线程时跳过。"""
    try:
        signal.alarm(seconds)
    except (ValueError, AttributeError):
        pass


def _mark_job_timeout(job_id: int) -> None:
    """标 job 为 failed（worker 自我兜底，不依赖 clean_stale daemon）。"""
    try:
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            if job and job.status == "running":
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                job.error = f"worker timeout {JOB_TIMEOUT}s（死代理 hang 兜底）"
    except Exception as exc:
        logger.error("mark_job_timeout 失败 job=%s: %s", job_id, exc)


def run_loop(should_continue=None) -> None:
    """领取并执行队列任务，直到 should_continue() 为假。"""
    should_continue = should_continue or (lambda: _running)
    # 注册超时 alarm handler（main thread only —— worker.py 主线程跑没问题）
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
    except ValueError:
        # 非主线程（in-process mode）—— alarm 不可用，靠 clean_stale daemon 兜底
        pass
    logger.info("worker %s 启动，轮询间隔 %ds，单 job 超时 %ds",
                WORKER_ID, POLL_INTERVAL, JOB_TIMEOUT)
    while should_continue():
        # 内存安全闸:已用内存超阈值则暂停领新 job(不起新浏览器),
        # 内存回落自动恢复。超时回循环重判,绝不在内存高位硬领。
        if not memory_gate.wait_until_ok(
                MEM_THRESHOLD, check_interval=MEM_CHECK_INTERVAL,
                max_wait=MEM_MAX_WAIT, should_continue=should_continue):
            continue
        try:
            job_id = claim_job(WORKER_ID)
        except Exception as exc:
            logger.error("领取任务失败: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue
        if job_id is None:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            _set_alarm(JOB_TIMEOUT)
            try:
                result = execute_job(job_id)
            finally:
                _set_alarm(0)
            if result["status"] == "success":
                recompute(result["site"])
            logger.info("job %s %s -> %s", job_id, result["site"],
                        result["status"])
        except JobTimeout as exc:
            _set_alarm(0)
            _mark_job_timeout(job_id)
            logger.warning("job %s 超时: %s", job_id, exc)
        except Exception as exc:
            _set_alarm(0)
            logger.error("job %s 执行异常: %s", job_id, exc)
    logger.info("worker %s 退出", WORKER_ID)


def _stop(*_):
    global _running
    _running = False


def main() -> None:
    from .db import init_db
    init_db()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_loop()


if __name__ == "__main__":
    main()
