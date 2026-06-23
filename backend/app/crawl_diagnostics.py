"""Structured crawl diagnostics.

This module is intentionally small and framework-neutral so existing crawlers
can adopt it incrementally.  It classifies common failures, stores URL frontier
state, and records failure events for UI/reporting.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from .models import CrawlFailure, CrawlJob, CrawlUrl, Site


NETWORK_TIMEOUT = "network_timeout"
DNS_ERROR = "dns_error"
PROXY_UNAVAILABLE = "proxy_unavailable"
PROXY_AUTH_FAILED = "proxy_auth_failed"
HTTP_401 = "http_401"
HTTP_403 = "http_403"
HTTP_429 = "http_429"
HTTP_5XX = "http_5xx"
ANTI_BOT_CHALLENGE = "anti_bot_challenge"
MARKET_PAUSED = "market_paused"
TRACKING_PAUSED = "tracking_paused"
WORKSPACE_HIDDEN = "workspace_hidden"
EMPTY_SITEMAP = "empty_sitemap"
NO_PRODUCT_URLS = "no_product_urls"
PARSE_NO_JSONLD = "parse_no_jsonld"
PARSE_NO_PRODUCT = "parse_no_product"
PARSE_NO_PRICE = "parse_no_price"
VALIDATION_FAILED = "validation_failed"
ZERO_PRODUCTS = "zero_products"
JOB_TIMEOUT = "job_timeout"
BROWSER_DEPENDENCY_MISSING = "browser_dependency_missing"
UNSUPPORTED_PLATFORM = "unsupported_platform"
RESOURCE_EXHAUSTED = "resource_exhausted"
WORKER_INTERRUPTED = "worker_interrupted"
QUEUE_STALLED = "queue_stalled"
MANUAL_MAINTENANCE = "manual_maintenance"
UNKNOWN = "unknown"

STAGE_DISCOVER = "discover"
STAGE_FETCH = "fetch"
STAGE_PARSE = "parse"
STAGE_VALIDATE = "validate"
STAGE_PERSIST = "persist"
STAGE_JOB = "job"


@dataclass(frozen=True)
class FailureInfo:
    code: str
    stage: str
    detail: str
    retryable: bool
    suggested_action: str
    http_status: int | None = None


def classify_http_status(status: int, detail: str = "") -> FailureInfo | None:
    if status == 401:
        return FailureInfo(
            HTTP_401,
            STAGE_FETCH,
            detail or "HTTP 401 authorization required",
            True,
            "检查代理出口、站点鉴权或切换官方/API 数据源",
            status,
        )
    if status == 403:
        return FailureInfo(
            HTTP_403,
            STAGE_FETCH,
            detail or "HTTP 403 forbidden",
            True,
            "配置可用住宅代理或启用浏览器/外部数据源",
            status,
        )
    if status == 429:
        return FailureInfo(
            HTTP_429,
            STAGE_FETCH,
            detail or "HTTP 429 rate limited",
            True,
            "降低并发和频率，延长冷却时间或更换代理出口",
            status,
        )
    if status >= 500:
        return FailureInfo(
            HTTP_5XX,
            STAGE_FETCH,
            detail or f"HTTP {status} server error",
            True,
            "稍后重试；若持续出现则检查目标站或代理稳定性",
            status,
        )
    return None


def classify_exception(exc: Exception, *, stage: str = STAGE_JOB) -> FailureInfo:
    text = str(exc)
    low = text.lower()
    if "auto-canceled" in low or "stuck running" in low or "job exceeded" in low:
        return FailureInfo(
            JOB_TIMEOUT, STAGE_JOB, text, True,
            "任务超过运行时限；检查代理/目标站响应和 URL 失败分布后重跑")
    if "executable doesn't exist" in low or "playwright install" in low:
        return FailureInfo(
            BROWSER_DEPENDENCY_MISSING, STAGE_JOB, text, False,
            "运行 playwright install chromium 安装浏览器依赖后重跑")
    if ("未知平台" in text or "unknown platform" in low
            or "unsupported platform" in low
            or "unsupported crawler platform" in low):
        return FailureInfo(
            UNSUPPORTED_PLATFORM, STAGE_JOB, text, False,
            "修正站点 platform 配置，或在 crawler registry 中补充平台适配")
    if "timed out" in low or "timeout" in low:
        return FailureInfo(
            NETWORK_TIMEOUT, stage, text, True,
            "检查代理连通性，必要时降低超时和并发")
    if "can't start new thread" in low or "cannot start new thread" in low:
        return FailureInfo(
            RESOURCE_EXHAUSTED, STAGE_JOB, text, True,
            "worker 线程/资源耗尽；降低并发、重启 worker 后重跑")
    if "worker did not consume" in low or ("queued job" in low and "worker" in low):
        return FailureInfo(
            QUEUE_STALLED, STAGE_JOB, text, True,
            "队列入队后未被 worker 消费；检查 worker 存活、触发类型白名单和队列积压")
    if (
        "broken pipe" in low
        or "manual rerun interrupted" in low
        or "stale-worker-cleanup" in low
        or "stale running job" in low
        or "watchdog-orphan-cleanup" in low
        or "orphaned:" in low
        or "zombie" in low
        or "容器重建" in text
        or "manual cleanup" in low
        or "killed" in low
        or "reset for fast iter" in low
    ):
        return FailureInfo(
            WORKER_INTERRUPTED, STAGE_JOB, text, True,
            "任务被重启/清理/人工中断；确认当前 worker 正常后重跑")
    if (
        "reschedule after fix" in low
        or "regex fix retry" in low
        or "fix retry" in low
    ):
        return FailureInfo(
            MANUAL_MAINTENANCE, STAGE_JOB, text, True,
            "历史人工维护/重排记录；如该站点仍缺数据，可从后台重跑")
    if "proxy" in low and ("auth" in low or "credentials" in low):
        return FailureInfo(
            PROXY_AUTH_FAILED, STAGE_FETCH, text, False,
            "检查代理用户名、密码和协议类型")
    if "proxy" in low or "connect" in low:
        return FailureInfo(
            PROXY_UNAVAILABLE, STAGE_FETCH, text, True,
            "检查代理服务、端口、防火墙和来源 IP 白名单")
    if (
        "anti_bot_challenge" in low
        or "verify you are human" in low
        or "challenge" in low
        or "kasada" in low
        or "kpsdk" in low
        or "captcha" in low
        or "access denied" in low
        or "连续被拦截" in text
        or ("连续" in text and "封锁" in text)
        or "熔断 ok=0" in low
        or ("熔断" in text and ("403" in text or "429" in text or "target" in low))
    ):
        return FailureInfo(
            ANTI_BOT_CHALLENGE, STAGE_FETCH, text, True,
            "切换可用住宅代理或启用浏览器/外部数据源")
    if "pausing orders" in low or "market" in low and "paused" in low:
        return FailureInfo(
            MARKET_PAUSED, STAGE_DISCOVER, text, False,
            "目标市场暂停运营；等待恢复或改用官方/API 数据源")
    if "无 custom-product 子 sitemap" in text or "empty sitemap" in low:
        return FailureInfo(
            EMPTY_SITEMAP, STAGE_DISCOVER, text, False,
            "目标站 sitemap 当前无商品；检查市场状态或改用 API")
    if "返回 401" in text:
        return classify_http_status(401, text) or _unknown(text, stage)
    if "返回 403" in text:
        return classify_http_status(403, text) or _unknown(text, stage)
    if "返回 429" in text:
        return classify_http_status(429, text) or _unknown(text, stage)
    if "http error 503" in low or "http 503" in low:
        return classify_http_status(503, text) or _unknown(text, stage)
    if "http error 502" in low or "http 502" in low:
        return classify_http_status(502, text) or _unknown(text, stage)
    if "http error 500" in low or "http 500" in low:
        return classify_http_status(500, text) or _unknown(text, stage)
    return _unknown(text, stage)


def zero_products_failure(site: str, detail: str = "") -> FailureInfo:
    return FailureInfo(
        ZERO_PRODUCTS,
        STAGE_PARSE,
        detail or f"{site} 本次抓取未产出有效商品",
        True,
        "查看 URL 发现数、HTTP 状态和解析失败原因；必要时配置站点 hints 或代理",
    )


def job_timeout_failure(site: str, timeout_sec: int, detail: str = "") -> FailureInfo:
    return FailureInfo(
        JOB_TIMEOUT,
        STAGE_JOB,
        detail or f"{site} 抓取任务超过 {timeout_sec}s，被 worker 兜底终止",
        True,
        "检查该站点最近 URL/代理失败分布；降低单次任务量或切换可用代理后重跑",
    )


def record_failure(
    session: Session,
    *,
    site: str,
    info: FailureInfo,
    job_id: int | None = None,
    url: str | None = None,
    fetcher: str | None = None,
    proxy_tier: str | None = None,
    apply_to_job: bool = True,
) -> CrawlFailure:
    row = CrawlFailure(
        site=site,
        job_id=job_id,
        url=url,
        stage=info.stage,
        code=info.code,
        detail=info.detail[:2000] if info.detail else None,
        retryable=info.retryable,
        suggested_action=info.suggested_action,
        http_status=info.http_status,
        fetcher=fetcher,
        proxy_tier=proxy_tier,
    )
    session.add(row)
    if apply_to_job:
        _apply_to_job(session, job_id, info)
    return row


def record_url_state(
    session: Session,
    *,
    site: str,
    url: str,
    kind: str = "product",
    source: str = "unknown",
    status: str = "pending",
    http_status: int | None = None,
    failure: FailureInfo | None = None,
    final_url: str | None = None,
    fetcher: str | None = None,
    content_hash: str | None = None,
    priority: int = 100,
) -> CrawlUrl:
    now = datetime.utcnow()
    url_hash = hash_url(url)
    row = (session.query(CrawlUrl)
           .filter(CrawlUrl.site == site, CrawlUrl.url_hash == url_hash)
           .first())
    if row is None:
        row = CrawlUrl(
            site=site,
            url_hash=url_hash,
            url=url,
            kind=kind,
            source=source,
            priority=priority,
            first_seen_at=now,
        )
        session.add(row)
    row.last_seen_at = now
    row.status = status
    row.http_status = http_status
    row.final_url = final_url
    row.fetcher = fetcher
    row.content_hash = content_hash
    if status in ("fetched", "parsed", "failed", "blocked"):
        row.attempts = (row.attempts or 0) + 1
        row.last_fetched_at = now
    if failure:
        row.failure_code = failure.code
        row.failure_stage = failure.stage
        row.failure_detail = failure.detail[:2000] if failure.detail else None
        row.retryable = failure.retryable
    elif status in ("pending", "fetched", "parsed", "skipped"):
        row.failure_code = None
        row.failure_stage = None
        row.failure_detail = None
        row.retryable = None
    return row


def mark_job_failure(session: Session, job_id: int | None, info: FailureInfo) -> None:
    _apply_to_job(session, job_id, info)


def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()


def _apply_to_job(session: Session, job_id: int | None, info: FailureInfo) -> None:
    if not job_id:
        return
    job = session.get(CrawlJob, job_id)
    if not job:
        return
    job.failure_code = info.code
    job.failure_stage = info.stage
    job.failure_detail = info.detail[:2000] if info.detail else None
    job.retryable = info.retryable
    job.suggested_action = info.suggested_action


def _unknown(detail: str, stage: str) -> FailureInfo:
    return FailureInfo(
        UNKNOWN,
        stage,
        detail,
        True,
        "查看原始错误、快照和目标站状态后决定是否重试或新增适配",
    )
