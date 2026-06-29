"""采集编排 + 任务队列。

队列即 crawl_jobs 表：
  enqueue()     —— 入队一条 pending 任务（scheduler / API 调用）
  claim_job()   —— worker 原子领取最旧 pending 任务
  execute_job() —— 执行已领取的任务：采集 → 清洗入库 → 促销识别 → 收尾
  run_site()    —— 入队 + 立即执行（CLI 同步路径，保持向后兼容）
"""
from __future__ import annotations

import re
import traceback
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, case, exists, false, func, or_, text, update

from .antiban import BlockedError, in_cooldown, set_cooldown
from .billing import record_usage
from .crawl_diagnostics import (
    FailureInfo,
    PROXY_UNAVAILABLE,
    STAGE_FETCH,
    STAGE_JOB,
    TRACKING_PAUSED,
    WORKSPACE_HIDDEN,
    classify_exception,
    record_failure,
    zero_products_failure,
)
from .crawlers.registry import get_crawler
from .db import IS_SQLITE, session_scope
from .models import Category, CrawlJob, CrawlUrl, Product, Promotion, Site
from .pipeline import parse_dt, to_price
from .price_sources import enrich_products_from_site_config
from .site_metrics import refresh_site_metrics

logger = logging.getLogger(__name__)

_PROMO_KEYWORDS = re.compile(
    r"\b("
    r"sale|deal|discount|promo|promotion|coupon|clearance|save|off|"
    r"black\s*friday|cyber\s*monday|flash|limited|"
    r"bundle|multibuy|multi[-\s]?buy|buy\s+\d|gift|free\s+shipping|"
    r"free\s+delivery|delivery\s+included|shipping\s+included|"
    r"rabatt|aktion|angebot|gutschein|"
    r"versandkostenfrei|kostenloser\s+versand|"
    r"remise|soldes|réduction|reduction|livraison\s+gratuite|"
    r"sconto|offerta|descuento|oferta|cupon|cupón|"
    r"spedizione\s+gratuita|env[ií]o\s+gratis|"
    r"korting|aanbieding|gratis\s+verzending|promoção|promocao|desconto|"
    r"rabat|zniżka|znizka|wyprzedaż|wyprzedaz|"
    r"darmowa\s+dostawa|特价|促销|优惠|折扣|券|包邮|免运费"
    r")\b",
    re.IGNORECASE,
)
_PROMO_ATTR_KEY = re.compile(
    r"(promo|promotion|coupon|discount|deal|sale|offer|badge|label|"
    r"savings|saving|bundle|shipping|delivery|campaign|couponcode|"
    r"coupon_code|voucher|markdown|was_price|rrp|msrp)",
    re.IGNORECASE,
)
_PERCENT_DISCOUNT_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:off|discount|save)?", re.I)
_PROMO_NAME_KEYS = (
    "promotion_name", "promo_name", "campaign_name", "offer_name",
    "coupon_name", "deal_name", "sale_name", "badge", "label",
    "shipping_label", "delivery_label", "free_shipping_label",
)
_PROMO_TYPE_KEYS = (
    "promotion_type", "promo_type", "offer_type", "coupon_type",
    "deal_type", "discount_type",
)
_PROMO_THRESHOLD_KEYS = (
    "threshold", "promotion_threshold", "promo_threshold",
    "minimum_order", "minimum_order_value", "min_order", "min_spend",
    "condition", "conditions", "coupon_condition", "offer_condition",
)
_PROMO_START_KEYS = (
    "start_time", "starts_at", "start_at", "start_date",
    "promotion_start", "promo_start", "valid_from", "validfrom", "from_date",
)
_PROMO_END_KEYS = (
    "end_time", "ends_at", "end_at", "end_date",
    "promotion_end", "promo_end", "valid_until", "valid_to",
    "valid_through", "validthrough", "to_date",
)

FAILED_PRODUCT_RETRY_TRIGGER = "failed_product_retry"
HIGH_PRIORITY_TRIGGERS = ("manual", "admin_quality_rerun", "admin_retry",
                          FAILED_PRODUCT_RETRY_TRIGGER)
AUTO_DEDUP_TRIGGERS = ("scheduled", "daily_refresh", "daily_delta")
DEFAULT_PLATFORM_RUNNING_LIMITS = {"vidaxl": 3}
NON_FATAL_PARTIAL_FAILURE_CODES = {
    "anti_bot_challenge",
    "incomplete_detail_parse",
    "network_timeout",
    "http_429",
    "parse_no_jsonld",
}
NON_FATAL_PARTIAL_MIN_PRODUCTS = 50
NON_FATAL_PARTIAL_SITE_MIN_PRODUCTS = {
    "cdiscount_fr": 40,
}
NON_FATAL_PARTIAL_MIN_SUCCESS_RATE = 95.0
AUTO_FAILED_PRODUCT_RETRY_LIMIT = int(os.environ.get(
    "AUTO_FAILED_PRODUCT_RETRY_LIMIT", "500"))
AUTO_FAILED_PRODUCT_RETRY_MAX_GAP = int(os.environ.get(
    "AUTO_FAILED_PRODUCT_RETRY_MAX_GAP", "1000"))
AUTO_FAILED_PRODUCT_RETRY_CHAIN_MAX = int(os.environ.get(
    "AUTO_FAILED_PRODUCT_RETRY_CHAIN_MAX", "20"))
AUTO_JOB_RETRY_TRIGGER = os.environ.get("AUTO_JOB_RETRY_TRIGGER", "admin_retry")
AUTO_JOB_RETRY_MAX_PER_SITE_DAY = int(os.environ.get(
    "AUTO_JOB_RETRY_MAX_PER_SITE_DAY", "2"))
AUTO_JOB_RETRY_CODES = tuple(
    code.strip() for code in os.environ.get(
        "AUTO_JOB_RETRY_CODES",
        "job_timeout,worker_interrupted,queue_stalled,resource_exhausted,"
        "proxy_unavailable,network_timeout,http_429,http_5xx,anti_bot_challenge",
    ).split(",")
    if code.strip()
)
FAILED_PRODUCT_RETRY_NODES = tuple(
    node.strip() for node in os.environ.get(
        "FAILED_PRODUCT_RETRY_NODES", "nas").split(",")
    if node.strip()
)


class FailedProductRetryError(RuntimeError):
    def __init__(self, info: FailureInfo, *, status: str = "failed"):
        super().__init__(info.detail)
        self.info = info
        self.status = status


def _auto_enqueue_job_retry(s, job: CrawlJob, *, reason_code: str | None) -> int | None:
    """Queue a bounded whole-site retry for transient infra failures."""
    if not job or not job.site:
        return None
    code = (reason_code or job.failure_code or "").strip()
    if code not in AUTO_JOB_RETRY_CODES:
        return None
    retry_trigger = (
        FAILED_PRODUCT_RETRY_TRIGGER
        if job.trigger == FAILED_PRODUCT_RETRY_TRIGGER
        else AUTO_JOB_RETRY_TRIGGER
    )
    now = datetime.utcnow()
    day_start = (job.created_at or now).replace(
        hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    source_marker = f"[auto_job_retry] source_job={job.id} code={code}"

    if getattr(getattr(s, "bind", None), "dialect", None) is not None:
        if s.bind.dialect.name == "postgresql":
            s.execute(
                text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"crawl-auto-retry:{job.id}:{code}"},
            )

    existing_source_retry = (
        s.query(CrawlJob)
        .filter(
            CrawlJob.site == job.site,
            CrawlJob.id != job.id,
            CrawlJob.created_at >= day_start,
            CrawlJob.created_at < day_end,
            CrawlJob.suggested_action == source_marker,
        )
        .order_by(CrawlJob.id.desc())
        .first()
    )
    if existing_source_retry is not None:
        job.suggested_action = (
            f"系统已为源任务 #{job.id} 创建过自动重跑任务 "
            f"#{existing_source_retry.id}，未重复创建。"
        )
        return existing_source_retry.id

    newer_success_q = (
        s.query(CrawlJob)
        .filter(
            CrawlJob.site == job.site,
            CrawlJob.id != job.id,
            CrawlJob.status == "success",
            CrawlJob.created_at >= day_start,
            CrawlJob.created_at < day_end,
        )
    )
    if job.created_at is not None:
        newer_success_q = newer_success_q.filter(
            CrawlJob.created_at >= job.created_at)
    newer_success = newer_success_q.order_by(CrawlJob.id.desc()).first()
    if newer_success is not None:
        job.suggested_action = (
            f"系统检测到同站点当天已有更新成功任务 #{newer_success.id}，"
            "未再创建自动整站重跑。"
        )
        return newer_success.id

    existing = (
        s.query(CrawlJob)
        .filter(
            CrawlJob.site == job.site,
            CrawlJob.id != job.id,
            CrawlJob.status.in_(("pending", "running")),
        )
        .order_by(CrawlJob.id.desc())
        .first()
    )
    if existing is not None:
        job.suggested_action = (
            f"系统已检测到同站点任务 #{existing.id}"
            f"（{existing.status}），未重复创建自动重跑。"
        )
        return existing.id

    retries_today = (
        s.query(CrawlJob.id)
        .filter(
            CrawlJob.site == job.site,
            CrawlJob.created_at >= day_start,
            CrawlJob.created_at < day_end,
            CrawlJob.suggested_action.like("[auto_job_retry]%"),
        )
        .count()
    )
    if retries_today >= max(0, AUTO_JOB_RETRY_MAX_PER_SITE_DAY):
        job.suggested_action = (
            f"系统检测到 {code}，但 {job.site} 当日自动整站重跑已达到上限 "
            f"{AUTO_JOB_RETRY_MAX_PER_SITE_DAY} 次；请人工确认后重跑。"
        )
        return None

    retry_job = CrawlJob(
        site=job.site,
        status="pending",
        trigger=retry_trigger,
        created_at=now,
        requested_by_workspace_id=job.requested_by_workspace_id,
        requested_by_user_id=job.requested_by_user_id,
        suggested_action=source_marker,
    )
    s.add(retry_job)
    s.flush()
    _enqueue_crawl_webhook(s, retry_job, event_type="job.triggered")
    job.suggested_action = (
        f"系统检测到 {code}，已自动创建整站重跑任务 #{retry_job.id}。"
    )
    return retry_job.id


def _utc_day_window(value: datetime | None) -> tuple[datetime, datetime]:
    """Return the UTC day window for a UTC-naive DB timestamp."""
    base = value or datetime.utcnow()
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _today_crawl_url_predicate(start: datetime, end: datetime):
    return or_(
        and_(CrawlUrl.last_fetched_at >= start, CrawlUrl.last_fetched_at < end),
        and_(CrawlUrl.last_fetched_at.is_(None),
             CrawlUrl.first_seen_at >= start,
             CrawlUrl.first_seen_at < end),
    )


def _platform_running_limits() -> dict[str, int]:
    raw = os.environ.get("CRAWL_PLATFORM_RUNNING_LIMITS", "")
    limits = dict(DEFAULT_PLATFORM_RUNNING_LIMITS)
    if not raw.strip():
        return limits
    for part in raw.split(","):
        if ":" not in part:
            continue
        platform, value = part.split(":", 1)
        platform = platform.strip().lower()
        if not platform:
            continue
        try:
            limit = int(value.strip())
        except (TypeError, ValueError):
            continue
        if limit > 0:
            limits[platform] = limit
        else:
            limits.pop(platform, None)
    return limits


def _platform_limit_reached(s, site: Site | None) -> bool:
    platform = (getattr(site, "platform", None) or "").strip().lower()
    if not platform:
        return False
    limit = _platform_running_limits().get(platform)
    if not limit:
        return False
    if getattr(getattr(s, "bind", None), "dialect", None) is not None:
        if s.bind.dialect.name == "postgresql":
            s.execute(
                text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"crawl-platform:{platform}"},
            )
    running = (
        s.query(CrawlJob.id)
        .join(Site, Site.site == CrawlJob.site)
        .filter(CrawlJob.status == "running", Site.platform == platform)
        .count()
    )
    return running >= limit


def _platforms_at_running_limit(s) -> set[str]:
    limits = _platform_running_limits()
    if not limits:
        return set()
    if getattr(getattr(s, "bind", None), "dialect", None) is not None:
        if s.bind.dialect.name == "postgresql":
            for platform in sorted(limits):
                s.execute(
                    text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": f"crawl-platform:{platform}"},
                )
    rows = (
        s.query(func.lower(Site.platform), func.count(CrawlJob.id))
        .join(Site, Site.site == CrawlJob.site)
        .filter(CrawlJob.status == "running",
                func.lower(Site.platform).in_(tuple(limits)))
        .group_by(func.lower(Site.platform))
        .all()
    )
    return {
        platform for platform, running in rows
        if platform and int(running or 0) >= limits.get(platform, 0)
    }


def _apply_platform_limit_filter(query, s):
    over_limit = _platforms_at_running_limit(s)
    if not over_limit:
        return query
    return (
        query.join(Site, Site.site == CrawlJob.site)
        .filter(or_(Site.platform.is_(None),
                    ~func.lower(Site.platform).in_(tuple(over_limit))))
    )


def enqueue(site_name: str, trigger: str = "manual",
            requested_by_workspace_id: int | None = None,
            requested_by_user_id: int | None = None) -> int | None:
    """入队一条采集任务，返回 job_id。"""
    with session_scope() as s:
        site = s.query(Site).filter(Site.site == site_name).first()
        if not site:
            raise ValueError(f"站点不存在: {site_name}")
        if trigger in AUTO_DEDUP_TRIGGERS and site.track_status == "paused":
            return None
        if trigger in AUTO_DEDUP_TRIGGERS:
            existing = (s.query(CrawlJob)
                        .filter(CrawlJob.site == site_name,
                                CrawlJob.status.in_(("pending", "running")),
                                CrawlJob.trigger.in_(AUTO_DEDUP_TRIGGERS))
                        .order_by(CrawlJob.id.desc())
                        .first())
            if existing is not None:
                return existing.id
        job = CrawlJob(site=site_name, status="pending", trigger=trigger,
                       created_at=datetime.utcnow(),
                       requested_by_workspace_id=requested_by_workspace_id,
                       requested_by_user_id=requested_by_user_id)
        s.add(job)
        s.flush()
        preflight = crawl_preflight_issue(site, trigger=trigger, session=s)
        if preflight is not None:
            _skip_job(s, job, preflight)
        elif (
            site.track_status == "error"
            and trigger != FAILED_PRODUCT_RETRY_TRIGGER
        ):
            site.track_status = "tracking"
        _enqueue_crawl_webhook(s, job, event_type="job.triggered")
        if preflight is not None:
            _enqueue_crawl_webhook(
                s,
                job,
                event_type="job.completed",
                error=preflight.detail,
                result={"failure_code": preflight.code},
            )
        return job.id


def tracking_paused_issue(site: Site | None) -> FailureInfo | None:
    """Return a failure when a site is intentionally paused in benchmark tracking."""
    if site is None or site.track_status != "paused":
        return None
    return FailureInfo(
        TRACKING_PAUSED,
        STAGE_JOB,
        f"{site.site} 已在标杆维护中暂停追踪，任务已跳过",
        False,
        "如需重新采集，请先在标杆维护中恢复追踪",
    )


def proxy_preflight_issue(site: Site | None) -> FailureInfo | None:
    """Return a failure when site-level proxy prerequisites are not met."""
    if site is None:
        return None
    tier = (site.proxy_tier or "").strip().lower()
    if not tier or tier == "none":
        return None
    from .proxy_pool import has_available_proxy

    if has_available_proxy(tier, site=site.site):
        return None
    return FailureInfo(
        PROXY_UNAVAILABLE,
        STAGE_FETCH,
        f"无可用 {tier} 代理，任务未入执行队列",
        True,
        "先在代理池补充/修复可用代理，再重跑该站点",
    )


def workspace_hidden_issue(site: Site | None, *, trigger: str | None = None,
                           session=None) -> FailureInfo | None:
    """Skip auto crawls for sites hidden from every enabled workspace."""
    if site is None or trigger not in AUTO_DEDUP_TRIGGERS or session is None:
        return None
    from .models import WorkspaceSite

    links = (session.query(WorkspaceSite)
             .filter(WorkspaceSite.site == site.site,
                     WorkspaceSite.enabled == True)  # noqa: E712
             .all())
    if not links or any(not bool(link.hidden) for link in links):
        return None
    return FailureInfo(
        WORKSPACE_HIDDEN,
        STAGE_JOB,
        f"{site.site} 已在所有启用工作区隐藏，自动调度任务已跳过",
        False,
        "如需恢复自动采集，请先在后台将该站点设为可见，或使用后台手动重跑",
    )


def crawl_preflight_issue(site: Site | None, *, trigger: str | None = None,
                          session=None) -> FailureInfo | None:
    """Return the first site-level reason a crawl should not run."""
    return (
        tracking_paused_issue(site)
        or workspace_hidden_issue(site, trigger=trigger, session=session)
        or proxy_preflight_issue(site)
    )


def _skip_job(s, job: CrawlJob, info: FailureInfo) -> None:
    job.status = "skipped"
    job.finished_at = datetime.utcnow()
    job.error = info.detail
    record_failure(s, site=job.site, job_id=job.id, info=info)


def _workspace_belongs_predicate(s, ws_ids):
    """Return a NULL-safe job → workspace ownership predicate."""
    from .models import WorkspaceSite  # noqa: PLC0415  避免循环 import

    site_subq = (s.query(WorkspaceSite.site)
                 .filter(WorkspaceSite.workspace_id.in_(ws_ids)))
    return or_(
        and_(CrawlJob.requested_by_workspace_id.isnot(None),
             CrawlJob.requested_by_workspace_id.in_(ws_ids)),
        and_(CrawlJob.requested_by_workspace_id.is_(None),
             CrawlJob.site.in_(site_subq)),
    )


def _apply_claim_filters(query, s, trigger_allowlist=None,
                         workspace_allowlist=None,
                         workspace_blocklist=None,
                         assigned_node: str | None = None,
                         assigned_only: bool = False):
    running_alias = CrawlJob.__table__.alias("running_jobs")
    query = query.filter(~exists().where(
        running_alias.c.status == "running"
    ).where(running_alias.c.site == CrawlJob.site))
    if assigned_only:
        if not assigned_node:
            return query.filter(false())
        query = query.filter(CrawlJob.assigned_node == assigned_node)
    elif assigned_node:
        query = query.filter(or_(CrawlJob.assigned_node.is_(None),
                                 CrawlJob.assigned_node == assigned_node))
    if trigger_allowlist:
        query = query.filter(CrawlJob.trigger.in_(trigger_allowlist))
    if workspace_allowlist:
        query = query.filter(_workspace_belongs_predicate(s, workspace_allowlist))
    if workspace_blocklist:
        query = query.filter(~_workspace_belongs_predicate(s, workspace_blocklist))
    return query


def _parse_distribution_nodes(nodes: tuple[str, ...]) -> tuple[
    tuple[str, ...], dict[str, int], dict[str, int]
]:
    weights: dict[str, int] = {}
    caps: dict[str, int] = {}
    for raw_node in nodes:
        item = raw_node.strip()
        if not item:
            continue
        parts = item.split(":")
        node = parts[0].strip()
        if not node:
            continue
        weight = 1
        if len(parts) >= 2 and parts[1].strip():
            try:
                weight = max(1, int(parts[1]))
            except ValueError:
                weight = 1
        weights[node] = weights.get(node, 0) + weight
        if len(parts) >= 3 and parts[2].strip():
            try:
                caps[node] = max(1, int(parts[2]))
            except ValueError:
                pass
    return tuple(weights), weights, caps


def assign_pending_jobs(distributor_id: str,
                        nodes: tuple[str, ...],
                        batch_size: int = 100,
                        stale_after_sec: int = 300,
                        trigger_allowlist: tuple[str, ...] | None = None,
                        workspace_allowlist: tuple[int, ...] | None = None,
                        workspace_blocklist: tuple[int, ...] | None = None) -> int:
    """NAS 将未分配的 pending job 预分配到节点，返回本轮分配数。"""
    clean_nodes, node_weights, node_caps = _parse_distribution_nodes(nodes)
    if not clean_nodes or batch_size <= 0:
        return 0

    with session_scope() as s:
        if not IS_SQLITE:
            locked = s.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": 740731552},
            ).scalar()
            if not locked:
                return 0

        now = datetime.utcnow()
        if stale_after_sec > 0:
            cutoff = now - timedelta(seconds=stale_after_sec)
            s.execute(
                update(CrawlJob)
                .where(CrawlJob.status == "pending",
                       CrawlJob.assigned_node.isnot(None),
                       or_(CrawlJob.assigned_at.is_(None),
                           CrawlJob.assigned_at < cutoff))
                .values(assigned_node=None, assigned_at=None, assigned_by=None)
            )

        priority = case(
            (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), 0),
            (CrawlJob.trigger == "tracking_add", 1),
            else_=2,
        )
        high_priority_touched_at = case(
            (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), CrawlJob.created_at),
            else_=datetime(1970, 1, 1),
        )
        query = s.query(CrawlJob).filter(CrawlJob.status == "pending",
                                         CrawlJob.assigned_node.is_(None))
        query = _apply_claim_filters(
            query, s,
            trigger_allowlist=trigger_allowlist,
            workspace_allowlist=workspace_allowlist,
            workspace_blocklist=workspace_blocklist,
        )
        candidates = (
            query.order_by(priority, high_priority_touched_at.desc(), CrawlJob.id)
            .limit(batch_size)
            .all()
        )
        if not candidates:
            return 0

        assigned_counts = {
            node: s.query(CrawlJob)
            .filter(CrawlJob.status.in_(("pending", "running")),
                    CrawlJob.assigned_node == node)
            .count()
            for node in clean_nodes
        }
        assigned = 0
        for job in candidates:
            eligible_nodes = tuple(
                node for node in clean_nodes
                if node_caps.get(node) is None or assigned_counts[node] < node_caps[node]
            )
            if job.trigger == FAILED_PRODUCT_RETRY_TRIGGER and FAILED_PRODUCT_RETRY_NODES:
                eligible_nodes = tuple(
                    node for node in eligible_nodes
                    if node in FAILED_PRODUCT_RETRY_NODES
                )
            if not eligible_nodes:
                continue
            node = min(
                eligible_nodes,
                key=lambda item: (
                    assigned_counts[item] / node_weights[item],
                    assigned_counts[item],
                    clean_nodes.index(item),
                ),
            )
            job.assigned_node = node
            job.assigned_at = now
            job.assigned_by = distributor_id
            assigned_counts[node] += 1
            assigned += 1
        return assigned


def claim_job(worker_id: str,
              trigger_allowlist: tuple[str, ...] | None = None,
              workspace_allowlist: tuple[int, ...] | None = None,
              workspace_blocklist: tuple[int, ...] | None = None,
              assigned_node: str | None = None,
              assigned_only: bool = False) -> int | None:
    """worker 原子领取最旧的 pending 任务，返回 job_id 或 None。

    workspace_allowlist: 只领这些 workspace_id 的 job（mini 专用）。
    workspace_blocklist: 不领这些 workspace_id 的 job（NAS 兜底）。
    scheduled / daily_refresh 等系统触发的 job requested_by_workspace_id 为 NULL，
    此时靠 workspace_sites 表映射 site → workspace 判定归属。
    """
    with session_scope() as s:
        skipped = 0

        while True:
            priority = case(
                (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), 0),
                (CrawlJob.trigger == "tracking_add", 1),
                else_=2,
            )
            query = s.query(CrawlJob).filter(CrawlJob.status == "pending")
            query = _apply_claim_filters(
                query, s,
                trigger_allowlist=trigger_allowlist,
                workspace_allowlist=workspace_allowlist,
                workspace_blocklist=workspace_blocklist,
                assigned_node=assigned_node,
                assigned_only=assigned_only,
            )
            query = _apply_platform_limit_filter(query, s)
            high_priority_touched_at = case(
                (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), CrawlJob.created_at),
                else_=datetime(1970, 1, 1),
            )
            candidates = (
                query.order_by(priority, high_priority_touched_at.desc(),
                               CrawlJob.id)
                .limit(50)
                .all()
            )
            if not candidates:
                return None
            for job in candidates:
                site = s.query(Site).filter(Site.site == job.site).first()
                preflight = crawl_preflight_issue(site, trigger=job.trigger,
                                                  session=s)
                if preflight is not None:
                    _skip_job(s, job, preflight)
                    s.flush()
                    skipped += 1
                    if skipped >= 50:
                        return None
                    continue
                # 乐观锁：仅当仍为 pending 时领取，防多 worker 抢同一任务
                now = datetime.utcnow()
                res = s.execute(
                    update(CrawlJob)
                    .where(CrawlJob.id == job.id, CrawlJob.status == "pending")
                    .values(status="running", worker=worker_id,
                            started_at=now, heartbeat_at=now))
                return job.id if res.rowcount == 1 else None
            return None


def _failed_product_retry_limit(crawler) -> int:
    config = getattr(getattr(crawler, "site", None), "crawler_config", None) or {}
    if isinstance(config, dict):
        try:
            return max(1, min(int(config.get("failed_product_retry_limit") or 500), 2000))
        except (TypeError, ValueError):
            return 500
    return 500


def _failed_product_retry_max_attempts(crawler) -> int:
    config = getattr(getattr(crawler, "site", None), "crawler_config", None) or {}
    if isinstance(config, dict):
        try:
            return max(1, min(int(config.get("failed_product_retry_max_attempts") or 5), 20))
        except (TypeError, ValueError):
            return 5
    return 5


def _crawl_failed_product_retry(crawler, job_id: int, site_name: str):
    """Run a URL-level retry without rediscovering the whole site."""
    if not hasattr(crawler, "crawl_failed_products"):
        info = FailureInfo(
            "unsupported_failed_product_retry",
            STAGE_JOB,
            f"{site_name} 暂不支持失败商品级重抓",
            False,
            "先使用整站重跑，或为该站点适配 crawl_failed_products",
        )
        raise FailedProductRetryError(info, status="failed")

    with session_scope() as s:
        job = s.get(CrawlJob, job_id)
        now = datetime.utcnow()
        retry_start, retry_end = _utc_day_window(job.created_at if job else None)
        limit = _failed_product_retry_limit(crawler)
        max_attempts = _failed_product_retry_max_attempts(crawler)
        rows = (s.query(CrawlUrl)
                .filter(CrawlUrl.site == site_name,
                        CrawlUrl.kind == "product",
                        CrawlUrl.url.isnot(None),
                        _today_crawl_url_predicate(retry_start, retry_end),
                        CrawlUrl.attempts < max_attempts,
                        or_(CrawlUrl.retryable.is_(None),
                            CrawlUrl.retryable.is_(True)),
                        or_(CrawlUrl.next_retry_at.is_(None),
                            CrawlUrl.next_retry_at <= now))
                .filter(or_(
                    and_(CrawlUrl.status == "pending",
                         CrawlUrl.priority <= 10),
                    CrawlUrl.status == "failed",
                    CrawlUrl.status == "blocked",
                    and_(CrawlUrl.status == "fetched",
                         CrawlUrl.failure_code.isnot(None)),
                ))
                .order_by(CrawlUrl.priority.asc(),
                          CrawlUrl.attempts.asc(),
                          CrawlUrl.id.asc())
                .limit(limit)
                .all())
        urls = [row.url for row in rows if row.url]
    if not urls:
        info = FailureInfo(
            "no_failed_product_urls",
            STAGE_JOB,
            f"{site_name} 没有可重抓的失败商品 URL",
            False,
            "检查失败商品筛选条件，或先运行一次整站抓取生成 URL 明细",
        )
        raise FailedProductRetryError(info, status="skipped")
    return crawler.crawl_failed_products(urls)


def _auto_enqueue_failed_product_retry(
    s,
    *,
    job: CrawlJob,
    crawler,
) -> tuple[int | None, int]:
    """Queue one URL-level retry for a retryable partial job.

    The retry job itself never schedules another retry here; this keeps auto
    recovery bounded and makes the follow-up visible in the normal job list.
    """
    if not hasattr(crawler, "crawl_failed_products"):
        return None, 0
    if job.trigger == FAILED_PRODUCT_RETRY_TRIGGER:
        if job.status not in {"success", "partial"} or job.retryable is False:
            return None, 0
        chain_max = max(0, AUTO_FAILED_PRODUCT_RETRY_CHAIN_MAX)
        if chain_max <= 0:
            return None, 0
        chain_cutoff = datetime.utcnow() - timedelta(days=1)
        chain_count = (
            s.query(CrawlJob.id)
            .filter(
                CrawlJob.site == job.site,
                CrawlJob.trigger == FAILED_PRODUCT_RETRY_TRIGGER,
                CrawlJob.created_at >= chain_cutoff,
            )
            .count()
        )
        if chain_count >= chain_max:
            return None, 0
    else:
        if job.status != "partial" or job.retryable is False:
            return None, 0
        produced = int(job.products_count or 0)
        total = int(job.total_product_count or 0)
        gap = max(0, total - produced)
        if total > 0 and gap > AUTO_FAILED_PRODUCT_RETRY_MAX_GAP:
            return None, 0

    existing = (
        s.query(CrawlJob)
        .filter(
            CrawlJob.site == job.site,
            CrawlJob.trigger == FAILED_PRODUCT_RETRY_TRIGGER,
            CrawlJob.status.in_(("pending", "running")),
        )
        .order_by(CrawlJob.id.desc())
        .first()
    )
    if existing is not None:
        return existing.id, 0

    now = datetime.utcnow()
    retry_start, retry_end = _utc_day_window(job.created_at)
    limit = max(1, min(AUTO_FAILED_PRODUCT_RETRY_LIMIT, 2000))
    max_attempts = _failed_product_retry_max_attempts(crawler)
    candidates = (
        s.query(CrawlUrl)
        .filter(
            CrawlUrl.site == job.site,
            CrawlUrl.kind == "product",
            CrawlUrl.url.isnot(None),
            _today_crawl_url_predicate(retry_start, retry_end),
            CrawlUrl.attempts < max_attempts,
            or_(CrawlUrl.retryable.is_(None), CrawlUrl.retryable.is_(True)),
            or_(CrawlUrl.next_retry_at.is_(None), CrawlUrl.next_retry_at <= now),
            or_(
                and_(CrawlUrl.status == "pending",
                     CrawlUrl.priority <= 10),
                CrawlUrl.status.in_(("failed", "blocked")),
                and_(CrawlUrl.status == "fetched",
                     CrawlUrl.failure_code.isnot(None)),
            ),
        )
        .order_by(
            CrawlUrl.attempts.asc(),
            CrawlUrl.last_fetched_at.asc().nullsfirst(),
            CrawlUrl.id.asc(),
        )
        .limit(limit)
        .all()
    )
    if not candidates:
        return None, 0

    for row in candidates:
        row.status = "pending"
        row.next_retry_at = None
        row.failure_code = None
        row.failure_stage = None
        row.failure_detail = None
        row.retryable = None
        row.last_seen_at = now
        row.priority = min(int(row.priority or 100), 10)

    retry_job = CrawlJob(
        site=job.site,
        status="pending",
        trigger=FAILED_PRODUCT_RETRY_TRIGGER,
        created_at=now,
        requested_by_workspace_id=job.requested_by_workspace_id,
        requested_by_user_id=job.requested_by_user_id,
    )
    s.add(retry_job)
    s.flush()
    _enqueue_crawl_webhook(s, retry_job, event_type="job.triggered")
    job.suggested_action = (
        f"已自动创建失败商品补抓任务 #{retry_job.id}，"
        f"本轮选择 {len(candidates)} 个失败 URL。"
    )
    return retry_job.id, len(candidates)


def _record_crawl_usage(*, workspace_id, products_count, duration_sec,
                        api_calls, browser_opens) -> None:
    """网页/后台采集：写一行 Usage（api_key_id=None → 只记录，不扣额度）。

    计费失败绝不中断采集收尾。
    """
    try:
        record_usage(
            api_key_id=None,
            workspace_id=workspace_id,
            endpoint="/crawl/job",
            record_count=products_count,
            credits_used=max(1, min(products_count, 10_000)),
            bytes_returned=0,
            duration_ms=int((duration_sec or 0) * 1000),
            api_calls=api_calls,
            browser_opens=browser_opens,
            pages_fetched=api_calls + browser_opens,
        )
    except Exception:
        pass


def _enqueue_crawl_webhook(s, job: CrawlJob | None, *,
                           event_type: str,
                           result: dict | None = None,
                           error: str | None = None) -> None:
    if job is None:
        return
    try:
        from .webhooks import enqueue_delivery

        payload = {
            "site": job.site,
            "trigger": job.trigger,
            "products_count": job.products_count or 0,
            "total_product_count": job.total_product_count,
            "new_count": job.new_count or 0,
            "promotion_count": job.promotion_count or 0,
            "success_rate": job.success_rate,
            "duration_sec": job.duration_sec,
            "failure_code": job.failure_code,
            "failure_stage": job.failure_stage,
            "retryable": job.retryable,
            "suggested_action": job.suggested_action,
        }
        if result:
            payload.update(result)
        enqueue_delivery(
            s,
            workspace_id=job.requested_by_workspace_id,
            event_type=event_type,
            job_kind="crawl",
            job_id=job.id,
            status=job.status or "pending",
            site=job.site,
            created_at=job.created_at,
            finished_at=job.finished_at,
            error=error if error is not None else job.error,
            result=payload,
        )
    except Exception as exc:
        logger.warning("enqueue crawl webhook failed job=%s: %s",
                       getattr(job, "id", None), exc)


def execute_job(job_id: int) -> dict:
    """执行一条已领取的任务。"""
    with session_scope() as s:
        job = s.get(CrawlJob, job_id)
        if job is None:
            raise ValueError(f"任务不存在: {job_id}")
        site = s.query(Site).filter(Site.site == job.site).first()
        site_name = job.site
        if job.status != "running":              # CLI 直跑路径：补置 running
            job.status = "running"
            job.started_at = datetime.utcnow()
        preflight = crawl_preflight_issue(site, trigger=job.trigger, session=s)
        if preflight is not None:
            _skip_job(s, job, preflight)
            _enqueue_crawl_webhook(
                s,
                job,
                event_type="job.completed",
                error=preflight.detail,
                result={"failure_code": preflight.code},
            )
            return {"job_id": job_id, "site": site_name, "status": "skipped",
                    "failure_code": preflight.code,
                    "error": preflight.detail,
                    "suggested_action": preflight.suggested_action}
        crawler = get_crawler(site)
        crawler.job_id = job_id

    # 站点冷却中 —— 跳过，不再去打（反封禁）
    if in_cooldown(site_name):
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "skipped"
            job.finished_at = datetime.utcnow()
            job.error = "站点处于封禁冷却期，本次跳过"
            _enqueue_crawl_webhook(s, job, event_type="job.completed",
                                   error=job.error)
        return {"job_id": job_id, "site": site_name, "status": "skipped"}

    started = datetime.utcnow()
    try:
        if job.trigger == FAILED_PRODUCT_RETRY_TRIGGER:
            result = _crawl_failed_product_retry(crawler, job_id, site_name)
        else:
            result = crawler.crawl()
    except FailedProductRetryError as exc:
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = exc.status
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = exc.info.detail
            record_failure(s, site=site_name, job_id=job_id, info=exc.info)
            _enqueue_crawl_webhook(
                s,
                job,
                event_type="job.completed",
                error=exc.info.detail,
                result={"failure_code": exc.info.code},
            )
        return {"job_id": job_id, "site": site_name, "status": exc.status,
                "failure_code": exc.info.code,
                "error": exc.info.detail,
                "suggested_action": exc.info.suggested_action}
    except BlockedError as exc:                  # 熔断 —— 站点封锁
        set_cooldown(site_name)
        info = classify_exception(exc)
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "blocked"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"熔断：{exc}（站点已进入冷却期）"
            record_failure(s, site=site_name, job_id=job_id, info=info)
            _enqueue_crawl_webhook(s, job, event_type="job.completed",
                                   error=job.error,
                                   result={"failure_code": info.code})
        return {"job_id": job_id, "site": site_name, "status": "blocked",
                "error": str(exc)}
    except Exception as exc:                     # 采集失败 —— C-005
        info = classify_exception(exc)
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
            record_failure(s, site=site_name, job_id=job_id, info=info)
            _fsite = s.query(Site).filter(Site.site == site_name).first()
            if _fsite and _fsite.track_status != "paused":
                _fsite.track_status = "error"
            _enqueue_crawl_webhook(s, job, event_type="job.completed",
                                   error=str(exc),
                                   result={"failure_code": info.code})
        return {"job_id": job_id, "site": site_name, "status": "failed",
                "error": str(exc)}

    with session_scope() as s:
        site = s.query(Site).filter(Site.site == site_name).first()
        if getattr(result, "products_already_persisted", False):
            products = []
            produced = int(getattr(result, "persisted_products_count", 0) or 0)
            raw_stats = getattr(result, "persisted_upsert_stats", None) or {}
            stats = {
                "total": produced,
                "inserted": int(raw_stats.get("inserted", 0) or 0),
                "updated": int(raw_stats.get("updated", 0) or 0),
                "skipped": int(raw_stats.get("skipped", 0) or 0),
                "new": int(raw_stats.get("new", 0) or 0),
                "changed": int(raw_stats.get("changed", 0) or 0),
            }
        else:
            from .pipeline import upsert_products
            products, price_source_stats = enrich_products_from_site_config(
                site, result.products, counter=getattr(crawler, "counter", None))
            if price_source_stats.get("applied"):
                result.notes.append(
                    "configured_price_source: "
                    f"matched={price_source_stats['matched']}, "
                    f"updated={price_source_stats['updated']}, "
                    f"rows={price_source_stats['rows']}"
                )
            elif price_source_stats.get("error"):
                result.notes.append(
                    f"configured_price_source_failed: {price_source_stats['error']}")
            stats = upsert_products(s, site_name, products)
            produced = len(products)
        _save_categories(s, site_name, result.categories)
        s.flush()
        promo_count = _detect_promotions(s, site_name)
        crawl_total = _crawl_total_from_result(result, fallback_count=stats["total"])
        if produced > crawl_total:
            crawl_total = produced
        if (
            produced > 0
            and not getattr(result, "coverage_complete", True)
            and crawl_total <= produced
        ):
            crawl_total = produced + 1

        job = s.get(CrawlJob, job_id)
        job.status = "success"
        job.finished_at = datetime.utcnow()
        job.duration_sec = (datetime.utcnow() - started).total_seconds()
        job.products_count = produced
        job.total_product_count = crawl_total
        job.new_count = stats["new"]
        job.promotion_count = promo_count
        total = crawl_total or 1
        job.success_rate = _coverage_rate(
            produced,
            total,
        )
        duration = job.duration_sec

        site.last_crawled = datetime.utcnow()
        site.updated_at = datetime.utcnow()
        if site.track_status != "paused":
            site.track_status = "error" if produced == 0 else "tracking"
        if produced == 0:
            job.status = "failed"
            if job.failure_code:
                job.error = job.failure_detail or "本次抓取未产出有效商品"
            else:
                job.error = "; ".join(result.notes[-3:]) if result.notes else (
                    "本次抓取未产出有效商品")
        if produced == 0 and not job.failure_code:
            info = zero_products_failure(
                site_name,
                "; ".join(result.notes[-3:]) if result.notes else "",
            )
            record_failure(s, site=site_name, job_id=job_id, info=info)
        non_fatal_partial = produced > 0 and _is_non_fatal_partial(job, produced)
        coverage_code = (
            getattr(result, "coverage_code", None)
            or "incomplete_discovery"
        )
        non_fatal_coverage = produced > 0 and _is_non_fatal_partial(
            job,
            produced,
            code_override=coverage_code,
        )
        non_fatal_detail_gap = produced > 0 and _is_non_fatal_partial(
            job,
            produced,
            code_override="incomplete_detail_parse",
        )
        if produced > 0 and job.failure_code:
            if non_fatal_partial:
                result.notes.append(
                    f"忽略非致命采集噪音: {job.failure_code}"
                )
                job.failure_code = None
                job.failure_stage = None
                job.failure_detail = None
                job.retryable = None
                job.suggested_action = None
            else:
                job.status = "partial"
        if produced > 0 and not getattr(result, "coverage_complete", True):
            if non_fatal_coverage:
                result.notes.append("忽略高覆盖率的非致命覆盖噪音")
            else:
                job.status = "partial"
                if not job.failure_code:
                    job.failure_code = coverage_code
                    job.failure_stage = (
                        getattr(result, "coverage_stage", None)
                        or "discovery"
                    )
                    job.failure_detail = (
                        getattr(result, "coverage_reason", None)
                        or "本次采集未能证明商品全量覆盖"
                    )
                    retryable = getattr(result, "coverage_retryable", None)
                    job.retryable = True if retryable is None else bool(retryable)
                    job.suggested_action = (
                        getattr(result, "coverage_suggested_action", None)
                        or "补充官方 sitemap/feed 或配置明确的全量发现入口后重跑"
                    )
        if (
            produced > 0
            and crawl_total > produced
            and job.status == "success"
            and not non_fatal_detail_gap
        ):
            job.status = "partial"
            job.failure_code = "incomplete_detail_parse"
            job.failure_stage = "coverage"
            job.failure_detail = (
                f"本次产出 {produced} 个商品，当前全量分母为 {crawl_total}，"
                "未覆盖全量商品。"
            )
            job.retryable = True
            job.suggested_action = "继续分批重跑或移除采集上限，直到本次分母全量覆盖。"
        auto_retry_job_id = None
        auto_retry_url_count = 0
        try:
            if job.status == "partial":
                auto_retry_job_id, auto_retry_url_count = _auto_enqueue_failed_product_retry(
                    s,
                    job=job,
                    crawler=crawler,
                )
            elif job.status in {"failed", "blocked"}:
                auto_retry_job_id = _auto_enqueue_job_retry(
                    s,
                    job,
                    reason_code=job.failure_code,
                )
        except Exception as exc:
            auto_retry_job_id, auto_retry_url_count = None, 0
            result.notes.append(f"auto_retry_enqueue_failed: {exc}")
        if auto_retry_job_id:
            if job.status == "partial":
                result.notes.append(
                    f"auto_failed_product_retry_job={auto_retry_job_id}, "
                    f"selected_urls={auto_retry_url_count}"
                )
            else:
                result.notes.append(
                    f"auto_job_retry={auto_retry_job_id}, selected_urls=0"
                )
        ws_id = job.requested_by_workspace_id
        webhook_result = {
            "products": produced,
            "new": stats["new"],
            "promotions": promo_count,
            "notes": list(result.notes or []),
        }
        if auto_retry_job_id:
            webhook_result["auto_retry_job_id"] = auto_retry_job_id
            webhook_result["auto_retry_url_count"] = auto_retry_url_count
        _enqueue_crawl_webhook(
            s,
            job,
            event_type="job.completed",
            error=(
                job.failure_detail or job.error or "; ".join(result.notes[-3:])
                if job.status != "success" else None
            ),
            result=webhook_result,
        )

    try:
        with session_scope() as metric_session:
            refresh_site_metrics(metric_session, [site_name])
    except Exception as exc:
        logger.warning("refresh site metrics failed site=%s: %s", site_name, exc)
        result.notes.append(f"site_metrics_refresh_failed: {exc}")

    counter = getattr(crawler, "counter", None)
    _record_crawl_usage(
        workspace_id=ws_id,
        products_count=produced,
        duration_sec=duration,
        api_calls=getattr(counter, "api_calls", 0),
        browser_opens=getattr(counter, "browser_opens", 0),
    )
    payload = {
        "job_id": job_id, "site": site_name, "status": job.status,
        "products": produced, "new": stats["new"],
        "promotions": promo_count, "notes": result.notes,
        "duration_sec": round(duration, 1),
    }
    if job.status != "success":
        payload["error"] = (
            job.failure_detail or job.error or "; ".join(result.notes[-3:])
            or "本次抓取未产出有效商品"
        )
        payload["failure_code"] = job.failure_code
        payload["failure_stage"] = job.failure_stage
        payload["retryable"] = job.retryable
        payload["suggested_action"] = job.suggested_action
        if auto_retry_job_id:
            payload["auto_retry_job_id"] = auto_retry_job_id
            payload["auto_retry_url_count"] = auto_retry_url_count
    return payload


def _is_non_fatal_partial(
    job: CrawlJob,
    produced: int,
    *,
    code_override: str | None = None,
) -> bool:
    min_products = NON_FATAL_PARTIAL_SITE_MIN_PRODUCTS.get(
        job.site,
        NON_FATAL_PARTIAL_MIN_PRODUCTS,
    )
    if produced < min_products:
        return False
    code = (code_override if code_override is not None else job.failure_code or "").strip()
    if code not in NON_FATAL_PARTIAL_FAILURE_CODES:
        return False
    try:
        success_rate = float(job.success_rate or 0)
    except (TypeError, ValueError):
        success_rate = 0
    return success_rate >= NON_FATAL_PARTIAL_MIN_SUCCESS_RATE


def _coverage_rate(produced: int, total: int) -> float:
    if total <= 0:
        return 0.0
    rate = round(produced / total * 100, 1)
    if 0 < produced < total and rate >= 100.0:
        return 99.9
    return rate


def _crawl_total_from_result(result, *, fallback_count: int) -> int:
    raw = getattr(result, "total_product_count", None)
    if raw is None:
        return int(fallback_count)
    return int(raw)


def run_site(site_name: str) -> dict:
    """入队 + 立即执行（CLI 同步路径）。"""
    job_id = enqueue(site_name)
    return execute_job(job_id)


def run_brand(brand: str) -> list[dict]:
    """采集某品牌全部站点。"""
    with session_scope() as s:
        names = [r.site for r in s.query(Site).filter(Site.brand == brand)]
    return [run_site(n) for n in names]


def _save_categories(s, site_name: str, cats: list[dict]) -> None:
    if not cats:
        return
    s.query(Category).filter(Category.site == site_name).delete()
    for c in cats:
        s.add(Category(**c))


def _detect_promotions(s, site_name: str) -> int:
    """促销识别 —— 价格降价 + 站点标签/属性里的明确促销活动。"""
    s.query(Promotion).filter(Promotion.site == site_name).delete()
    rows = (s.query(Product)
            .filter(Product.site == site_name)
            .filter(or_(
                Product.original_price > Product.sale_price,
                Product.label.isnot(None),
                Product.tags.isnot(None),
                Product.attributes.isnot(None),
                Product.has_free_shipping.is_(True),
            ))
            .all())
    count = 0
    for p in rows:
        has_price_promo = (
            p.original_price is not None and p.sale_price is not None
            and p.original_price > p.sale_price
        )
        label = _promotion_label(p)
        meta_entries = _promotion_meta_entries(p)
        if p.has_free_shipping and not any(
            _promotion_meta_is_free_shipping(meta) for meta in meta_entries
        ):
            meta_entries.append({
                "promotion_name": "Free shipping",
                "promotion_type": "free_shipping",
                "_label": "Free shipping",
            })
        if not has_price_promo and not label:
            if not meta_entries:
                continue
        discount = None
        if has_price_promo and p.original_price:
            discount = round((p.original_price - p.sale_price) / p.original_price * 100)
        if discount is None and label:
            discount = _discount_from_text(label)
        if not has_price_promo and not label and not meta_entries:
            continue
        meta_entries = meta_entries or [_promotion_meta(p)]
        img = p.image_urls[0] if p.image_urls else None
        seen_promos: set[tuple[str, str, str | None]] = set()
        for meta in meta_entries:
            meta_label = meta.get("_label") or label
            meta_original = meta.get("original_price")
            meta_promo_price = meta.get("promotion_price")
            meta_discount = meta.get("discount_percent")
            promo_type = meta.get("promotion_type") or (
                "price_promotion" if has_price_promo
                else _promotion_type_from_text(str(meta_label or ""))
            )
            promo_name = meta.get("promotion_name") or meta_label or promo_type
            effective_discount = (
                None if promo_type == "free_shipping" and meta_discount is None
                else meta_discount if meta_discount is not None
                else discount
            )
            dedupe_key = (
                str(promo_type or "").lower(),
                str(promo_name or "").strip().lower(),
                meta.get("threshold"),
            )
            if dedupe_key in seen_promos:
                continue
            seen_promos.add(dedupe_key)
            s.add(Promotion(
                sku=p.sku, site=site_name,
                promotion_type=promo_type,
                promotion_name=str(promo_name)[:160],
                original_price=meta_original if meta_original is not None else p.original_price,
                promotion_price=meta_promo_price if meta_promo_price is not None else p.sale_price,
                discount_percent=effective_discount,
                threshold=meta.get("threshold"),
                start_time=meta.get("start_time"),
                end_time=meta.get("end_time"),
                product_title=p.title, product_image=img,
            ))
            count += 1
    return count


def _promotion_label(product: Product) -> str | None:
    values = _promotion_values(product)
    for value in values:
        text = value.strip()
        if text and _PROMO_KEYWORDS.search(text):
            return text[:120]
    return None


def _promotion_values(product: Product) -> list[str]:
    values = []
    if product.label:
        values.append(str(product.label))
    tags = product.tags or []
    if isinstance(tags, str):
        values.append(tags)
    elif isinstance(tags, (list, tuple, set)):
        values.extend(str(v) for v in tags if v)
    elif isinstance(tags, dict):
        for key, value in tags.items():
            if _PROMO_ATTR_KEY.search(str(key)) or (
                isinstance(value, str) and _PROMO_KEYWORDS.search(value)
            ):
                values.append(str(value))
    attrs = product.attributes or {}
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            if value in (None, "", [], {}):
                continue
            if _PROMO_ATTR_KEY.search(str(key)):
                if isinstance(value, (list, tuple, set)):
                    values.extend(str(v) for v in value if v not in (None, ""))
                elif isinstance(value, dict):
                    values.extend(str(v) for v in value.values()
                                  if isinstance(v, str) and _PROMO_KEYWORDS.search(v))
                else:
                    values.append(str(value))
            elif isinstance(value, str) and _PROMO_KEYWORDS.search(value):
                values.append(value)
            elif isinstance(value, (list, tuple, set)):
                values.extend(str(v) for v in value
                              if isinstance(v, str) and _PROMO_KEYWORDS.search(v))
    elif isinstance(attrs, str) and _PROMO_KEYWORDS.search(attrs):
        values.append(attrs)
    return values


def _promotion_meta(product: Product) -> dict:
    data = _flatten_promo_sources(product)
    label = _promotion_label(product)
    return _promotion_meta_from_mapping(data, label)


def _promotion_meta_from_mapping(data: dict[str, object],
                                 label: str | None = None) -> dict:
    threshold = _first_meta_value(data, _PROMO_THRESHOLD_KEYS)
    if threshold is None and label:
        threshold = _threshold_from_text(label)
    promo_type = _first_meta_value(data, _PROMO_TYPE_KEYS)
    if promo_type is None and label:
        promo_type = _promotion_type_from_text(label)
    return {
        "promotion_name": _first_meta_value(data, _PROMO_NAME_KEYS),
        "promotion_type": promo_type,
        "threshold": str(threshold).strip()[:160] if threshold not in (None, "") else None,
        "start_time": _first_meta_datetime(data, _PROMO_START_KEYS),
        "end_time": _first_meta_datetime(data, _PROMO_END_KEYS),
        "original_price": _first_meta_price(data, (
            "original_price", "pre_price", "was_price", "high_price",
            "highprice", "regular_price", "list_price", "compare_at_price",
            "msrp", "rrp",
        )),
        "promotion_price": _first_meta_price(data, (
            "promotion_price", "post_price", "sale_price", "price",
            "low_price", "lowprice", "offer_price", "discount_price",
            "final_price", "current_price",
        )),
        "discount_percent": _first_discount_percent(data, label),
        "_label": label,
    }


def _promotion_meta_entries(product: Product) -> list[dict]:
    entries = []
    for item in _explicit_promotion_items(product):
        data = _flatten_mapping(item)
        label = _entry_label(item)
        meta = _promotion_meta_from_mapping(data, label)
        if not meta.get("promotion_name") and label:
            meta["promotion_name"] = label
        if _promotion_meta_has_signal(meta):
            entries.append(meta)
    return entries


def _explicit_promotion_items(product: Product) -> list[dict]:
    items: list[dict] = []

    def scan(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key or "").lower()
                is_promo_key = bool(
                    _PROMO_ATTR_KEY.search(key_text)
                    or key_text in {"promotions", "promotion", "offers", "coupons", "deals"}
                )
                if is_promo_key and isinstance(item, list):
                    for child in item:
                        if isinstance(child, dict):
                            items.append(child)
                        elif _value_has_promo_signal(child):
                            items.append({"promotion_name": str(child).strip()})
                elif isinstance(item, dict):
                    scan(item)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict) and _entry_label(child):
                    items.append(child)
                elif _value_has_promo_signal(child):
                    items.append({"promotion_name": str(child).strip()})

    scan(product.attributes or {})
    scan(product.tags or {})
    return items


def _value_has_promo_signal(value) -> bool:
    if value in (None, "", [], {}):
        return False
    return isinstance(value, (str, int, float)) and bool(
        _PROMO_KEYWORDS.search(str(value))
    )


def _promotion_meta_is_free_shipping(meta: dict) -> bool:
    promo_type = str(meta.get("promotion_type") or "").lower()
    label = " ".join(str(meta.get(key) or "") for key in (
        "promotion_name", "_label", "threshold",
    ))
    return promo_type == "free_shipping" or _promotion_type_from_text(label) == "free_shipping"


def _promotion_meta_has_signal(meta: dict) -> bool:
    original = meta.get("original_price")
    promo = meta.get("promotion_price")
    if original is not None and promo is not None and original > promo:
        return True
    for key in ("promotion_name", "promotion_type", "threshold",
                "start_time", "end_time", "discount_percent"):
        if meta.get(key) not in (None, "", [], {}):
            return True
    label = str(meta.get("_label") or "")
    return bool(label and _PROMO_KEYWORDS.search(label))


def _entry_label(item: dict) -> str | None:
    for key in (*_PROMO_NAME_KEYS, "name", "title", "label", "text", "description"):
        value = item.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()[:160]
    text = " ".join(str(v) for v in item.values()
                    if isinstance(v, (str, int, float)) and v not in (None, ""))
    return text[:160] if text and _PROMO_KEYWORDS.search(text) else None


def _flatten_promo_sources(product: Product) -> dict[str, object]:
    out: dict[str, object] = {}

    def add_mapping(prefix: str, value) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            key_text = str(key or "").strip().lower()
            if not key_text:
                continue
            out[key_text] = item
            out[f"{prefix}_{key_text}"] = item

    add_mapping("attr", product.attributes or {})
    add_mapping("tag", product.tags or {})
    if product.label:
        out["label"] = product.label
    return out


def _flatten_mapping(value: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, item in value.items():
        key_text = str(key or "").strip().lower()
        if not key_text:
            continue
        full_key = f"{prefix}_{key_text}" if prefix else key_text
        if isinstance(item, dict):
            out.update(_flatten_mapping(item, full_key))
        else:
            out[key_text] = item
            out[full_key] = item
    return out


def _first_meta_value(data: dict[str, object], keys: tuple[str, ...]):
    for key in keys:
        for candidate in (key, f"promo_{key}", f"promotion_{key}", f"coupon_{key}"):
            if candidate in data and data[candidate] not in (None, "", [], {}):
                value = data[candidate]
                if isinstance(value, (list, tuple, set)):
                    value = next((v for v in value if v not in (None, "")), None)
                if isinstance(value, dict):
                    value = value.get("label") or value.get("name") or value.get("value")
                if value not in (None, ""):
                    return value
    return None


def _first_meta_datetime(data: dict[str, object], keys: tuple[str, ...]):
    value = _first_meta_value(data, keys)
    return parse_dt(value)


def _first_meta_price(data: dict[str, object], keys: tuple[str, ...]) -> float | None:
    value = _first_meta_value(data, keys)
    return to_price(value)


def _first_discount_percent(data: dict[str, object], label: str | None = None) -> int | None:
    value = _first_meta_value(data, ("discount_percent", "discount", "saving", "savings"))
    if isinstance(value, (int, float)) and 0 < float(value) < 100:
        return round(float(value))
    text_value = str(value or label or "")
    return _discount_from_text(text_value)


def _promotion_type_from_text(text: str | None) -> str:
    lowered = (text or "").lower()
    if (
        "free shipping" in lowered or "free delivery" in lowered
        or "delivery included" in lowered or "shipping included" in lowered
        or "versandkostenfrei" in lowered or "kostenloser versand" in lowered
        or "livraison gratuite" in lowered or "spedizione gratuita" in lowered
        or "envío gratis" in lowered or "envio gratis" in lowered
        or "gratis verzending" in lowered or "darmowa dostawa" in lowered
        or "包邮" in lowered or "免运费" in lowered
    ):
        return "free_shipping"
    if (
        "coupon" in lowered or "code" in lowered or "gutschein" in lowered
        or "cupón" in lowered or "cupon" in lowered or "券" in lowered
    ):
        return "coupon"
    if (
        "bundle" in lowered or "multibuy" in lowered
        or "multi-buy" in lowered or "multi buy" in lowered
        or "buy " in lowered or "gift" in lowered
    ):
        return "bundle"
    if "clearance" in lowered or "wyprzeda" in lowered or "soldes" in lowered:
        return "clearance"
    if (
        "sale" in lowered or "price" in lowered or "off" in lowered
        or "rabatt" in lowered or "remise" in lowered
        or "réduction" in lowered or "reduction" in lowered
        or "sconto" in lowered or "descuento" in lowered
        or "korting" in lowered or "desconto" in lowered
        or "rabat" in lowered or "zniżka" in lowered or "znizka" in lowered
    ):
        return "price_promotion"
    return "site_promotion"


def _threshold_from_text(text: str | None) -> str | None:
    raw = text or ""
    patterns = (
        r"(?:orders?|order|spend|minimum|min\.?)\s*(?:over|above|of|from|>=)?\s*([$€£¥]?\s*\d[\d\s\u00a0.,]*)",
        r"(?:满|订单满)\s*([$€£¥]?\s*\d[\d\s\u00a0.,]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            amount = match.group(1).strip()
            return f"orders over {amount}"
    return None


def _discount_from_text(text: str) -> int | None:
    match = _PERCENT_DISCOUNT_RE.search(text or "")
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            value = None
        if value is not None and 0 < value < 100:
            return round(value)
    return None
