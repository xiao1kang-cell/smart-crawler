"""Spine 队列 worker —— 轮询 spine_jobs,执行抓取任务。

用法:
  · 独立进程: python -m app.spine_worker(服务化部署,可起多副本)
镜像 app/worker.py 模式,但消费 spine 队列、走 spine.resolve。
每轮先回收超时的悬挂 running job(兜 execute_job 非原子崩溃窗口)。
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time

from .spine_queue import claim_job, execute_job, reclaim_stale_jobs

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [spine-worker] %(message)s")
logger = logging.getLogger("smart-crawler.spine-worker")

WORKER_ID = os.environ.get("SPINE_WORKER_ID") or \
    f"spine-{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL = int(os.environ.get("SPINE_WORKER_POLL", "5"))
RUNNING_TIMEOUT = int(os.environ.get("SPINE_WORKER_RUNNING_TIMEOUT", "600"))
_running = True


def run_loop(poll_interval: int | None = None, should_continue=None) -> None:
    """领取并执行队列任务,直到 should_continue() 为假。"""
    interval = POLL_INTERVAL if poll_interval is None else poll_interval
    should_continue = should_continue or (lambda: _running)
    logger.info("spine-worker %s 启动,轮询间隔 %ds", WORKER_ID, interval)
    while should_continue():
        try:
            reclaimed = reclaim_stale_jobs(running_timeout_sec=RUNNING_TIMEOUT)
            if reclaimed:
                logger.warning("回收 %d 个超时悬挂 running job", reclaimed)
        except Exception as exc:
            logger.error("回收悬挂 job 失败: %s", exc)
        try:
            job_id = claim_job(WORKER_ID)
        except Exception as exc:
            logger.error("领取任务失败: %s", exc)
            time.sleep(interval)
            continue
        if job_id is None:
            time.sleep(interval)
            continue
        try:
            result = execute_job(job_id)
            logger.info("job %s -> %s", job_id, result.get("status"))
        except Exception as exc:
            # execute_job 内部已兜失败;这里只防御未预期异常,worker 永不挂
            logger.error("执行 job %s 异常: %s", job_id, exc)
    logger.info("spine-worker %s 退出", WORKER_ID)


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
