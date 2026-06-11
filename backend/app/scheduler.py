"""采集调度 —— 规格 §8.1（C-001 ~ C-007）。

差异化频率（按数据源更新节奏分档，配置见 sites.yaml settings）：
  · 商品/价格/促销   每日（freq_products）   —— 入队，worker 执行
  · 口碑评论         每周（freq_reviews）    —— 评论增长慢 + 反爬敏感
  · Google Shopping  每周（freq_shopping）
单站点可在 sites.yaml 加 crawl_freq 覆盖（如促销敏感站每日 2 次）。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings, get_sites

logger = logging.getLogger("smart-crawler.scheduler")
_scheduler: BackgroundScheduler | None = None


def _cron(expr: str, fallback: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(expr)
    except ValueError:
        return CronTrigger.from_crontab(fallback)


# ---------- 三类定时任务 ----------
def _product_job(site_name: str) -> None:
    """商品站定时采集 —— 入队，由 worker 执行。paused 站跳过。"""
    try:
        from .db import session_scope
        from .models import Site
        with session_scope() as s:
            site = s.query(Site).filter(Site.site == site_name).first()
            if site and site.track_status == "paused":
                logger.info("站点 %s 已暂停追踪,跳过定时采集", site_name)
                return
        from .runner import enqueue
        job_id = enqueue(site_name, trigger="scheduled")
        logger.info("已入队商品采集: %s (job %s)", site_name, job_id)
    except Exception as exc:
        logger.error("入队失败 %s: %s", site_name, exc)


def _review_job() -> None:
    """口碑评论定时采集 —— 周级，覆盖所有已实现采集器的平台。"""
    try:
        from .review_runner import load_channels, run_review_platform
        channels, _ = load_channels()
        platforms = {c["platform"] for c in channels
                     if c.get("platform") in ("trustpilot", "reviews_io",
                                               "google_map")}
        for p in platforms:
            logger.info("定时评论采集开始: %s", p)
            run_review_platform(p)
    except Exception as exc:
        logger.error("定时评论采集失败: %s", exc)


def _shopping_job() -> None:
    """Google Shopping 定时采集 —— 周级，全部关键词。"""
    try:
        from .shopping_runner import crawl_all_keywords
        logger.info("定时 Google Shopping 采集开始")
        crawl_all_keywords()
    except Exception as exc:
        logger.error("定时 Shopping 采集失败: %s", exc)


def start_scheduler() -> BackgroundScheduler:
    """启动调度器，注册差异化定时任务。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    s = get_settings()
    default = s.get("default_freq", "0 2 * * *")
    freq_products = s.get("freq_products", default)
    freq_reviews = s.get("freq_reviews", "0 4 * * 1")
    freq_shopping = s.get("freq_shopping", "0 5 * * 1")

    sched = BackgroundScheduler(timezone="UTC")

    # 1) 商品站 —— 每日（站点可 crawl_freq 覆盖）
    for cfg in get_sites():
        cron = cfg.get("crawl_freq", freq_products)
        sched.add_job(_product_job, trigger=_cron(cron, default),
                      args=[cfg["site"]], id=f"crawl_{cfg['site']}",
                      replace_existing=True, max_instances=1,
                      misfire_grace_time=3600)

    # 2) 口碑评论 —— 每周
    sched.add_job(_review_job, trigger=_cron(freq_reviews, "0 4 * * 1"),
                  id="crawl_reviews", replace_existing=True,
                  max_instances=1, misfire_grace_time=7200)

    # 3) Google Shopping —— 每周
    sched.add_job(_shopping_job, trigger=_cron(freq_shopping, "0 5 * * 1"),
                  id="crawl_shopping", replace_existing=True,
                  max_instances=1, misfire_grace_time=7200)

    # 4) Daily Delta —— 每天凌晨 2:00 UTC（北京时间 10:00 AM，遨森 SOP）
    #    5 个 delta job：sitemap / top SKU / promo / review / aggregate
    try:
        from .daily_delta import run_all_daily_delta
        freq_daily_delta = s.get("freq_daily_delta", "0 2 * * *")
        sched.add_job(run_all_daily_delta,
                      trigger=_cron(freq_daily_delta, "0 2 * * *"),
                      id="daily_delta", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
        logger.info("Daily Delta 已注册：cron=%s", freq_daily_delta)
    except Exception as exc:
        logger.error("Daily Delta 注册失败: %s", exc)

    sched.start()
    _scheduler = sched
    logger.info("调度器已启动：%d 商品站(每日) + 评论(每周) + Shopping(每周) + Delta(凌晨)",
                len(get_sites()))
    return sched


def list_scheduled_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    return [{
        "id": j.id,
        "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
    } for j in _scheduler.get_jobs()]
