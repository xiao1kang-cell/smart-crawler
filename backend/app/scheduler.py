"""采集调度 —— 规格 §8.1（C-001 ~ C-007）。

差异化频率（按数据源更新节奏分档，配置见 sites.yaml settings）：
  · 商品/价格/促销   每日（freq_products）   —— 入队，worker 执行
  · 口碑评论         每周（freq_reviews）    —— 评论增长慢 + 反爬敏感
  · Google Shopping  每周（freq_shopping）
单站点可在 sites.yaml 加 crawl_freq 覆盖（如促销敏感站每日 2 次）。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings, get_sites

logger = logging.getLogger("smart-crawler.scheduler")
_scheduler: BackgroundScheduler | None = None
_AUTO_RETRY_FAILURE_CODES = {
    "unknown",
    "job_timeout",
    "network_timeout",
    "proxy_unavailable",
    "http_5xx",
}


def _cron(expr: str, fallback: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(expr)
    except ValueError:
        return CronTrigger.from_crontab(fallback)


# ---------- 三类定时任务 ----------
def _product_job(site_name: str, trigger: str = "scheduled") -> bool:
    """商品站定时采集 —— 入队，由 worker 执行。paused 站跳过。"""
    try:
        from .db import session_scope
        from .models import Site
        with session_scope() as s:
            site = s.query(Site).filter(Site.site == site_name).first()
            if site and site.track_status == "paused":
                logger.info("站点 %s 已暂停追踪,跳过定时采集", site_name)
                return True
        from .runner import enqueue
        job_id = enqueue(site_name, trigger=trigger)
        if job_id is None:
            logger.info("站点 %s 已暂停追踪,未创建定时采集任务", site_name)
            return True
        logger.info("已入队商品采集: %s (job %s, trigger=%s)",
                    site_name, job_id, trigger)
        return True
    except Exception as exc:
        logger.error("入队失败 %s: %s", site_name, exc)
        return False


def _product_batch_job(site_names: list[str], trigger: str = "scheduled") -> None:
    """Serially enqueue scheduled product sites.

    APScheduler runs jobs with a thread pool. Registering one cron job per site
    makes every site open DB connections at the same second, which can exhaust
    Postgres before all jobs are inserted. Keep scheduling as one small serial
    critical section; workers still crawl in parallel after jobs are created.
    """
    retries = 3
    delay_sec = 0.25
    failed: list[str] = []
    logger.info("批量商品采集入队开始：%d 个站点，trigger=%s",
                len(site_names), trigger)
    for site_name in site_names:
        ok = False
        for attempt in range(1, retries + 1):
            ok = bool(_product_job(site_name, trigger=trigger))
            if ok:
                break
            sleep_for = min(5.0, delay_sec * (2 ** (attempt - 1)))
            logger.warning(
                "站点 %s 定时入队失败，第 %d/%d 次，%.1fs 后重试",
                site_name, attempt, retries, sleep_for,
            )
            time.sleep(sleep_for)
        if not ok:
            failed.append(site_name)
        time.sleep(delay_sec)
    if failed:
        logger.error("批量商品采集入队完成，但仍失败 %d 个站点：%s",
                     len(failed), ",".join(failed))
    else:
        logger.info("批量商品采集入队完成：%d 个站点全部处理", len(site_names))


def _beijing_day_window_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return today's Beijing business day as naive UTC datetimes."""
    now_utc = now or datetime.utcnow()
    beijing_now = now_utc + timedelta(hours=8)
    beijing_start = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = beijing_start - timedelta(hours=8)
    return utc_start, utc_start + timedelta(days=1)


def _reconcile_today_product_jobs() -> None:
    """Backfill missed scheduled jobs and one safe retry for transient failures."""
    try:
        from .db import session_scope
        from .models import CrawlJob, Site, WorkspaceSite
        start, end = _beijing_day_window_utc()
        expected_sites = [cfg["site"] for cfg in _scheduled_product_sites()]
        if not expected_sites:
            logger.info("定时补偿检查：没有可调度站点")
            return
        expected = set(expected_sites)
        retry_sites: list[str] = []
        with session_scope() as s:
            scheduled_rows = (
                s.query(CrawlJob.site)
                .filter(CrawlJob.site.in_(expected_sites),
                        CrawlJob.trigger == "scheduled",
                        CrawlJob.created_at >= start,
                        CrawlJob.created_at < end)
                .distinct()
                .all()
            )
            scheduled = {site for (site,) in scheduled_rows}
            missing = sorted(expected - scheduled)

            failed_jobs = (
                s.query(CrawlJob)
                .join(Site, Site.site == CrawlJob.site)
                .filter(CrawlJob.site.in_(expected_sites),
                        CrawlJob.trigger == "scheduled",
                        CrawlJob.status == "failed",
                        CrawlJob.created_at >= start,
                        CrawlJob.created_at < end,
                        CrawlJob.failure_code.in_(tuple(_AUTO_RETRY_FAILURE_CODES)),
                        ((Site.track_status.is_(None)) |
                         (Site.track_status == "tracking")),
                        s.query(WorkspaceSite.id)
                        .filter(WorkspaceSite.site == Site.site,
                                WorkspaceSite.enabled.is_(True),
                                WorkspaceSite.hidden.is_(False))
                        .exists())
                .order_by(CrawlJob.id.desc())
                .all()
            )
            for job in failed_jobs:
                later = (
                    s.query(CrawlJob.id)
                    .filter(CrawlJob.site == job.site,
                            CrawlJob.created_at > job.created_at,
                            CrawlJob.created_at < end,
                            CrawlJob.trigger.in_(("scheduled", "admin_retry")),
                            CrawlJob.status.in_(("pending", "running", "success",
                                                 "partial")))
                    .first()
                )
                if later is None and job.site not in retry_sites:
                    retry_sites.append(job.site)

        if missing:
            logger.warning("定时补偿检查：今天漏入队 %d 个站点：%s",
                           len(missing), ",".join(missing))
            _product_batch_job(missing, trigger="scheduled")
        else:
            logger.info("定时补偿检查：今天没有漏入队站点")
        if retry_sites:
            logger.warning("定时补偿检查：可重试失败 %d 个站点：%s",
                           len(retry_sites), ",".join(retry_sites))
            _product_batch_job(sorted(retry_sites), trigger="admin_retry")
    except Exception as exc:
        logger.error("定时补偿检查失败: %s", exc)


def _scheduled_product_sites() -> list[dict]:
    """Return product sites that should be auto-scheduled.

    The tracking UI is driven by workspace_sites, so the scheduler must use the
    same visible-site scope.  Fall back to sites.yaml only when the database is
    unavailable during startup.
    """
    yaml_by_site = {cfg.get("site"): cfg for cfg in get_sites() if cfg.get("site")}
    try:
        from .db import session_scope
        from .models import Site, WorkspaceSite
        with session_scope() as s:
            rows = (
                s.query(Site.site, Site.crawler_config)
                .filter(
                    (Site.track_status.is_(None)) | (Site.track_status == "tracking"),
                    s.query(WorkspaceSite.id)
                    .filter(
                        WorkspaceSite.site == Site.site,
                        WorkspaceSite.enabled.is_(True),
                        WorkspaceSite.hidden.is_(False),
                    )
                    .exists(),
                )
                .order_by(Site.site)
                .all()
            )
    except Exception as exc:
        logger.warning("读取工作区可见站点失败，回退 sites.yaml 调度: %s", exc)
        return list(yaml_by_site.values())

    out: list[dict] = []
    for site_name, crawler_config in rows:
        cfg = dict(yaml_by_site.get(site_name) or {"site": site_name})
        if isinstance(crawler_config, dict) and crawler_config.get("crawl_freq"):
            cfg["crawl_freq"] = crawler_config["crawl_freq"]
        out.append(cfg)
    return out


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
    # 按 cron 分组后批量串行入队，避免同一秒几十个 APScheduler 线程
    # 同时打开 DB 连接导致 Postgres "too many clients already"。
    product_sites = _scheduled_product_sites()
    product_groups: dict[str, list[str]] = defaultdict(list)
    for cfg in product_sites:
        cron = cfg.get("crawl_freq", freq_products)
        product_groups[cron].append(cfg["site"])
    for idx, (cron, site_names) in enumerate(sorted(product_groups.items()), start=1):
        sched.add_job(_product_batch_job, trigger=_cron(cron, default),
                      args=[site_names], id=f"crawl_products_{idx}",
                      replace_existing=True, max_instances=1,
                      misfire_grace_time=3600)
    sched.add_job(_reconcile_today_product_jobs,
                  trigger=CronTrigger(minute="15", timezone="UTC"),
                  id="crawl_products_reconcile", replace_existing=True,
                  max_instances=1, misfire_grace_time=900)

    # 2) 口碑评论 —— 每周
    sched.add_job(_review_job, trigger=_cron(freq_reviews, "0 4 * * 1"),
                  id="crawl_reviews", replace_existing=True,
                  max_instances=1, misfire_grace_time=7200)

    # 3) Google Shopping —— 每周
    sched.add_job(_shopping_job, trigger=_cron(freq_shopping, "0 5 * * 1"),
                  id="crawl_shopping", replace_existing=True,
                  max_instances=1, misfire_grace_time=7200)

    # 4) Daily Delta —— 每天凌晨 2:20 UTC（北京时间 10:20 AM，错开商品入队尖峰）
    #    5 个 delta job：sitemap / top SKU / promo / review / aggregate
    try:
        from .daily_delta import run_all_daily_delta
        freq_daily_delta = s.get("freq_daily_delta", "20 2 * * *")
        sched.add_job(run_all_daily_delta,
                      trigger=_cron(freq_daily_delta, "20 2 * * *"),
                      id="daily_delta", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
        logger.info("Daily Delta 已注册：cron=%s", freq_daily_delta)
    except Exception as exc:
        logger.error("Daily Delta 注册失败: %s", exc)

    sched.start()
    _scheduler = sched
    logger.info("调度器已启动：%d 商品站(每日) + 评论(每周) + Shopping(每周) + Delta(凌晨)",
                len(product_sites))
    return sched


def list_scheduled_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    return [{
        "id": j.id,
        "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
    } for j in _scheduler.get_jobs()]
