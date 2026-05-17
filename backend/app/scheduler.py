"""采集调度 —— 规格 §8.1（C-001 ~ C-007）。

用 APScheduler 给每个站点注册定时采集任务（默认每日 02:00 当地低峰错峰）。
失败重试由 runner 内的异常处理 + 任务表状态体现。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .analytics import recompute
from .config import get_settings, get_sites
from .runner import run_site

logger = logging.getLogger("smart-crawler.scheduler")
_scheduler: BackgroundScheduler | None = None


def _job(site_name: str) -> None:
    """一次定时采集 + 重算分析。"""
    logger.info("定时采集开始: %s", site_name)
    result = run_site(site_name)
    if result["status"] == "success":
        recompute(site_name)
    logger.info("定时采集结束: %s -> %s", site_name, result["status"])


def start_scheduler() -> BackgroundScheduler:
    """启动调度器，按 sites.yaml 注册每站点的采集任务。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    default_freq = settings.get("default_freq", "0 2 * * *")
    sched = BackgroundScheduler(timezone="UTC")

    for cfg in get_sites():
        cron = cfg.get("crawl_freq", default_freq)
        try:
            trigger = CronTrigger.from_crontab(cron)
        except ValueError:
            trigger = CronTrigger.from_crontab(default_freq)
        sched.add_job(
            _job, trigger=trigger, args=[cfg["site"]],
            id=f"crawl_{cfg['site']}", replace_existing=True,
            max_instances=1, misfire_grace_time=3600,
        )

    sched.start()
    _scheduler = sched
    logger.info("调度器已启动，注册 %d 个站点任务", len(sched.get_jobs()))
    return sched


def list_scheduled_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    return [{
        "id": j.id,
        "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
    } for j in _scheduler.get_jobs()]
