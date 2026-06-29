"""Unified fetch layer for crawlers.

The goal is not to replace every crawler at once.  New and migrated crawlers
can use ``CrawlerFetcher`` to get consistent proxy handling, failure
classification, URL state tracking, and future middleware hooks.
"""
from __future__ import annotations

import hashlib
import json
import os
import random as _random
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from curl_cffi import requests as creq

from . import proxy_pool
from .antiban import BlockedError, acquire_rate, acquire_rate_interval
from .crawl_diagnostics import (
    ANTI_BOT_CHALLENGE,
    FailureInfo,
    HTTP_401,
    HTTP_403,
    HTTP_429,
    PROXY_UNAVAILABLE,
    STAGE_FETCH,
    classify_exception,
    classify_http_status,
    record_failure,
    record_url_state,
)
from .db import SessionLocal
from .models import Site
from .proxy_health import is_proxy_health_failure, record_proxy_result
from .url_filters import is_system_or_challenge_url

_ANTI_BOT_STRONG_MARKERS = (
    "cf-chl",
    "/cdn-cgi/challenge-platform/",
    "challenge-platform",
    "just a moment",
    "verify you are human",
    "robot or human",
    "access denied",
    "pardon our interruption",
    "please enable cookies",
    "px-captcha",
    "sec-if-cpt",
    "datadome=",
    "geo.captcha-delivery.com",
    "ct.captcha-delivery.com",
)
_ANTI_BOT_WEAK_MARKERS = (
    "captcha",
    "unusual traffic",
)
_NORMAL_PRODUCT_MARKERS = (
    "application/ld+json",
    "schema.org/product",
    "product:price",
    "data-price",
    "__next_data__",
    "window.__initial_state__",
)

_STEALTH_FETCHER = "scrapling"
_BROWSER_FETCHERS = (_STEALTH_FETCHER, "playwright")


@dataclass
class CrawlCounter:
    """一次抓取作用域内的成功次数累计（失败/重试不计）。"""
    api_calls: int = 0
    browser_opens: int = 0

    @property
    def pages_fetched(self) -> int:
        return self.api_calls + self.browser_opens


@dataclass
class FetchContext:
    site: Site
    job_id: int | None = None
    kind: str = "product"
    source: str = "unknown"
    timeout: int = 30
    use_proxy: bool = True
    allow_stealth: bool = False
    fail_fast_blocked: bool = False
    retries: int = 1
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)
    rotate_proxy_on_retry: bool = True
    require_proxy: bool | None = None
    max_blocked_events: int = 0
    counter: CrawlCounter | None = None
    residential_fallback_threshold: int = 3
    proxy_lease_ttl_sec: int = field(
        default_factory=lambda: int(os.environ.get("PROXY_LEASE_TTL_SEC", "300"))
    )
    rate_interval_sec: float | None = None


@dataclass
class FetchResult:
    ok: bool
    url: str
    status: int | None = None
    text: str = ""
    content: bytes = b""
    final_url: str | None = None
    fetcher: str = "curl_cffi"
    proxy: str | None = None
    duration_ms: int = 0
    failure: FailureInfo | None = None
    attempt: int = 1
    retry_after: float | None = None
    proxy_lease_token: str | None = None

    def json(self):
        """把 text 解析为 JSON；失败返回 None（不抛错）。"""
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


class FetchMiddleware(Protocol):
    def before_request(self, fetcher: "CrawlerFetcher", url: str,
                       kwargs: dict) -> None: ...

    def after_response(self, fetcher: "CrawlerFetcher",
                       result: FetchResult) -> None: ...


class CrawlerFetcher:
    def __init__(self, context: FetchContext,
                 middlewares: list[FetchMiddleware] | None = None):
        self.context = context
        self._blocked_events = 0
        self.middlewares = middlewares or [
            ProxyMiddleware(),
            RetryMiddleware(),
            AntiBotMiddleware(),
            SnapshotMetricsMiddleware(),
        ]
        self._fail_count = 0
        self._upgraded_tier: str | None = None
        self._no_proxy_diag_logged = False
        self._fail_lock = threading.Lock()

    def effective_tier(self) -> str | None:
        """当前生效的代理 tier：升级后为 residential，否则站点配置值。"""
        return self._upgraded_tier or self.context.site.proxy_tier

    def note_failure(self, result: "FetchResult") -> None:
        """累计 429/anti_bot 失败；达阈值且住宅可用则升级。线程安全。"""
        if not (result.failure and result.failure.code in (HTTP_429, ANTI_BOT_CHALLENGE)):
            return
        with self._fail_lock:
            # 已升级 or 已记过池空诊断 → 终态，不再累加/查池/写库
            if self._upgraded_tier is not None or self._no_proxy_diag_logged:
                return
            self._fail_count += 1
            if self._fail_count < self.context.residential_fallback_threshold:
                return
            if proxy_pool.has_available_proxy("residential", site=self.context.site.site):
                self._upgraded_tier = "residential"
            else:
                self._record_no_proxy_diag()
                self._no_proxy_diag_logged = True

    def _record_no_proxy_diag(self) -> None:
        """达升级阈值但住宅代理池空 —— 记一条诊断，不静默裸打。"""
        db = SessionLocal()
        try:
            info = FailureInfo(
                PROXY_UNAVAILABLE, STAGE_FETCH,
                f"{self.context.site.site} 累计 {self._fail_count} 次反爬，"
                f"但无可用住宅代理，无法升级",
                True, "检查住宅代理池余额/白名单/冷却状态")
            record_failure(db, site=self.context.site.site,
                           job_id=self.context.job_id, info=info,
                           proxy_tier=self.context.site.proxy_tier)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def get(self, url: str, **kwargs) -> FetchResult:
        return self._retry_loop("GET", url, with_stealth=True, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> FetchResult:
        """通用请求；非 GET 不走 stealth 浏览器兜底（stealth 仅适用于 HTML 抓取）。"""
        return self._retry_loop(method, url, with_stealth=False, **kwargs)

    def post(self, url: str, **kwargs) -> FetchResult:
        return self.request("POST", url, **kwargs)

    def _retry_loop(self, method: str, url: str, *, with_stealth: bool, **kwargs) -> FetchResult:
        attempts = max(1, self.context.retries + 1)
        last: FetchResult | None = None
        record_kind = kwargs.get("kind", self.context.kind)
        record_source = kwargs.get("source", self.context.source)
        if record_kind == "product" and is_system_or_challenge_url(url):
            result = FetchResult(
                ok=False,
                url=url,
                status=0,
                fetcher="url_filter",
                failure=None,
            )
            _record_fetch(self.context, result, kind=record_kind,
                          source=record_source, apply_to_job=False)
            return result
        for attempt in range(1, attempts + 1):
            request_kwargs = dict(kwargs)
            if self.context.rate_interval_sec is not None:
                acquire_rate_interval(
                    self.context.site.site,
                    max(0.0, float(self.context.rate_interval_sec)),
                )
            else:
                acquire_rate(self.context.site.site,
                             self.context.site.platform or "")
            for mw in self.middlewares:
                mw.before_request(self, url, request_kwargs)
            result = self._request_once(method, url, attempt=attempt, **request_kwargs)
            for mw in self.middlewares:
                mw.after_response(self, result)
            _record_fetch(self.context, result, kind=record_kind,
                          source=record_source, apply_to_job=False)
            last = result
            if result.ok:
                self._blocked_events = 0
                self._count(result)
                return result
            self.note_failure(result)
            self._raise_if_blocked_budget_exceeded(result)
            if with_stealth and self.context.allow_stealth and _should_stealth(result):
                stealth = self._get_stealth(url, attempt=attempt)
                for mw in self.middlewares:
                    mw.after_response(self, stealth)
                _record_fetch(self.context, stealth, kind=self.context.kind,
                              source="stealth", apply_to_job=False)
                if stealth.ok:
                    self._blocked_events = 0
                    self._count(stealth)
                    return stealth
                last = stealth
                self._raise_if_blocked_budget_exceeded(stealth)
            if not _should_retry(self.context, result, attempt, attempts):
                break
            time.sleep(_backoff_seconds(result, attempt))
        if last is not None:
            return last
        fallback = FetchResult(ok=False, url=url, failure=FailureInfo(
            "unknown", STAGE_FETCH, "fetch produced no result", True,
            "检查 fetcher 配置"))
        _record_fetch(self.context, fallback, kind=record_kind,
                      source=record_source, apply_to_job=True)
        return fallback

    def _raise_if_blocked_budget_exceeded(self, result: FetchResult) -> None:
        if not _should_stealth(result):
            return
        self._blocked_events += 1
        limit = int(self.context.max_blocked_events or 0)
        if limit and self._blocked_events >= limit:
            failure = result.failure
            code = failure.code if failure else "blocked"
            detail = failure.detail if failure else "blocked response"
            raise BlockedError(
                f"{self.context.site.site} 连续/累计 {self._blocked_events} 次反爬失败"
                f"（{code}: {detail}）"
            )

    def _request_once(self, method: str, url: str, *, attempt: int = 1, **kwargs) -> FetchResult:
        ctx = self.context
        timeout = int(kwargs.pop("timeout", ctx.timeout))
        source = kwargs.pop("source", ctx.source)
        kind = kwargs.pop("kind", ctx.kind)
        sess = creq.Session(impersonate=kwargs.pop("impersonate", "chrome"))
        sess.headers.update(kwargs.pop("headers", {}) or {})
        proxy = kwargs.pop("_proxy", None)
        proxy_lease_token = kwargs.pop("_proxy_lease_token", None)
        missing_proxy_tier = kwargs.pop("_proxy_unavailable_tier", None)
        if missing_proxy_tier:
            failure = FailureInfo(
                PROXY_UNAVAILABLE,
                STAGE_FETCH,
                f"无可用 {missing_proxy_tier} 代理",
                True,
                "检查代理池配置、冷却状态和代理余额/白名单",
            )
            result = FetchResult(
                ok=False,
                url=url,
                proxy=None,
                duration_ms=0,
                failure=failure,
                attempt=attempt,
                proxy_lease_token=proxy_lease_token,
            )
            return result
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        started = time.time()
        try:
            resp = sess.request(method, url, timeout=timeout, **kwargs)
            duration_ms = int((time.time() - started) * 1000)
            text = resp.text or ""
            content = resp.content or b""
            failure = classify_http_status(resp.status_code)
            if failure is None and _looks_like_anti_bot(text):
                failure = FailureInfo(
                    ANTI_BOT_CHALLENGE,
                    STAGE_FETCH,
                    f"疑似反爬挑战页面 status={resp.status_code} body={len(text)}",
                    True,
                    "切换可用住宅代理或启用浏览器/外部数据源",
                    resp.status_code,
                )
            result = FetchResult(
                ok=failure is None and 200 <= resp.status_code < 400,
                url=url,
                status=resp.status_code,
                text=text,
                content=content,
                final_url=getattr(resp, "url", None) or url,
                proxy=proxy,
                duration_ms=duration_ms,
                failure=failure,
                attempt=attempt,
                retry_after=_parse_retry_after(
                    getattr(resp, "headers", None) and resp.headers.get("Retry-After")
                ),
                proxy_lease_token=proxy_lease_token,
            )
            if failure and ctx.fail_fast_blocked and (
                resp.status_code in (401, 403, 429)
                or failure.code == ANTI_BOT_CHALLENGE
            ):
                raise BlockedError(f"{url} 返回 {resp.status_code} —— {failure.code}")
            return result
        except BlockedError:
            raise
        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            failure = classify_exception(exc, stage=STAGE_FETCH)
            result = FetchResult(
                ok=False,
                url=url,
                proxy=proxy,
                duration_ms=duration_ms,
                failure=failure,
                attempt=attempt,
                proxy_lease_token=proxy_lease_token,
            )
            return result

    def _get_stealth(self, url: str, *, attempt: int = 1) -> FetchResult:
        started = time.time()
        try:
            from scrapling.fetchers import StealthyFetcher
            from .crawlers._stealth_config import stealth_kwargs
            kw = stealth_kwargs(
                proxy=None,
                country=self.context.site.country,
                persist_profile_key=f"{self.context.site.site}_fetcher",
                timeout_ms=max(45_000, self.context.timeout * 1000),
            )
            page = StealthyFetcher.fetch(url, **kw)
            text = getattr(page, "html_content", None) or getattr(page, "body", None) or ""
            status = getattr(page, "status", None) or 200
            failure = classify_http_status(status)
            if failure is None and _looks_like_anti_bot(text):
                failure = FailureInfo(
                    ANTI_BOT_CHALLENGE,
                    STAGE_FETCH,
                    f"stealth 仍疑似反爬 status={status} body={len(text)}",
                    True,
                    "切换高质量住宅代理或外部/API 数据源",
                    status,
                )
            result = FetchResult(
                ok=failure is None and 200 <= status < 400,
                url=url,
                status=status,
                text=text,
                content=text.encode("utf-8", "ignore"),
                final_url=url,
                fetcher=_STEALTH_FETCHER,
                duration_ms=int((time.time() - started) * 1000),
                failure=failure,
                attempt=attempt,
            )
            return result
        except Exception as exc:
            failure = classify_exception(exc, stage=STAGE_FETCH)
            result = FetchResult(
                ok=False,
                url=url,
                fetcher=_STEALTH_FETCHER,
                duration_ms=int((time.time() - started) * 1000),
                failure=failure,
                attempt=attempt,
            )
            return result

    def _count(self, result: FetchResult) -> None:
        """仅对成功结果计数（失败/重试已被调用方过滤）。"""
        counter = self.context.counter
        if counter is None or not result.ok:  # defensive: caller already filters
            return
        if result.fetcher in _BROWSER_FETCHERS:
            counter.browser_opens += 1
        else:
            counter.api_calls += 1


class ProxyMiddleware:
    def before_request(self, fetcher: CrawlerFetcher, url: str,
                       kwargs: dict) -> None:
        ctx = fetcher.context
        if "_proxy" in kwargs:
            return
        tier = fetcher.effective_tier()
        if not ctx.use_proxy or tier in (None, "", "none"):
            return
        lease_ttl = int(ctx.proxy_lease_ttl_sec or 0)
        if lease_ttl > 0:
            handle = proxy_pool.lease_proxy(
                tier,
                site=ctx.site.site,
                job_id=ctx.job_id,
                ttl_sec=lease_ttl,
            )
            if handle:
                kwargs["_proxy"] = handle.url
                kwargs["_proxy_lease_token"] = handle.lease_token
                return
            # 租约容量满时，降级为非租约代理，避免把短暂饱和误判成
            # "无可用代理" 直接失败；后续请求仍会走同一 tier / site 规则。
        proxy = proxy_pool.get_proxy(tier, site=ctx.site.site)
        if proxy:
            kwargs["_proxy"] = proxy
            return
        require_proxy = (
            ctx.require_proxy
            if ctx.require_proxy is not None
            else tier not in (None, "", "none")
        )
        if require_proxy:
            kwargs["_proxy_unavailable_tier"] = tier

    def after_response(self, fetcher: CrawlerFetcher,
                       result: FetchResult) -> None:
        if not result.proxy:
            return
        proxy_failed = is_proxy_health_failure(result.failure)
        hard = bool(result.failure and result.failure.code == "proxy_auth_failed")
        if result.ok or not proxy_failed:
            proxy_pool.report_success(result.proxy)
        else:
            proxy_pool.report_failure(result.proxy, hard=hard)
        if result.proxy_lease_token:
            proxy_pool.release_proxy(
                result.proxy_lease_token,
                success=result.ok or not proxy_failed,
                failure_code=result.failure.code if result.failure else None,
            )
        db = SessionLocal()
        try:
            record_proxy_result(
                db,
                proxy_url=result.proxy,
                tier=fetcher.context.site.proxy_tier,
                success=result.ok or not proxy_failed,
                failure=result.failure,
                node=proxy_pool.NODE_ID,
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()


class RetryMiddleware:
    def before_request(self, fetcher: CrawlerFetcher, url: str,
                       kwargs: dict) -> None:
        return None

    def after_response(self, fetcher: CrawlerFetcher,
                       result: FetchResult) -> None:
        return None


class AntiBotMiddleware:
    def before_request(self, fetcher: CrawlerFetcher, url: str,
                       kwargs: dict) -> None:
        return None

    def after_response(self, fetcher: CrawlerFetcher,
                       result: FetchResult) -> None:
        return None


class SnapshotMetricsMiddleware:
    def before_request(self, fetcher: CrawlerFetcher, url: str,
                       kwargs: dict) -> None:
        return None

    def after_response(self, fetcher: CrawlerFetcher,
                       result: FetchResult) -> None:
        return None


def _record_fetch(ctx: FetchContext, result: FetchResult,
                  *, kind: str, source: str,
                  apply_to_job: bool = True) -> None:
    db = SessionLocal()
    try:
        content_hash = (hashlib.sha256(result.content).hexdigest()
                        if result.content else None)
        status = "fetched" if result.ok else (
            "skipped" if result.fetcher == "url_filter" else (
                "blocked" if result.status in (401, 403, 429) else "failed"))
        record_url_state(
            db,
            site=ctx.site.site,
            url=result.url,
            kind=kind,
            source=source,
            status=status,
            http_status=result.status,
            failure=result.failure,
            final_url=result.final_url,
            fetcher=result.fetcher,
            content_hash=content_hash,
        )
        if result.failure:
            record_failure(
                db,
                site=ctx.site.site,
                job_id=ctx.job_id,
                url=result.url,
                info=result.failure,
                fetcher=result.fetcher,
                proxy_tier=ctx.site.proxy_tier,
                apply_to_job=apply_to_job,
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _apply_fetch_failure_to_job(ctx: FetchContext, result: FetchResult) -> None:
    if not result.failure:
        return
    db = SessionLocal()
    try:
        record_failure(
            db,
            site=ctx.site.site,
            job_id=ctx.job_id,
            url=result.url,
            info=result.failure,
            fetcher=result.fetcher,
            proxy_tier=ctx.site.proxy_tier,
            apply_to_job=True,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _looks_like_anti_bot(text: str) -> bool:
    if not text:
        return True
    # Some legitimate SSR product pages embed bot-management beacons early in
    # <head> (for example IKEA's Cloudflare jsd script) while the real product
    # JSON-LD appears much later.  Check a wider window for product signals
    # before treating strong anti-bot marker substrings as authoritative.
    product_sample = text[:350000].lower() if len(text) >= 80_000 else text[:20000].lower()
    if any(marker in product_sample for marker in _NORMAL_PRODUCT_MARKERS):
        return False
    sample = text[:20000].lower()
    if any(marker in sample for marker in _ANTI_BOT_STRONG_MARKERS):
        return True
    has_weak_marker = any(marker in sample for marker in _ANTI_BOT_WEAK_MARKERS)
    if not has_weak_marker:
        return False
    if len(text) >= 80_000:
        return False
    return not any(marker in sample for marker in _NORMAL_PRODUCT_MARKERS)


BACKOFF_BASE = 2.0
BACKOFF_MAX_SEC = 60.0


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头。仅支持秒数形式（HTTP-date 形式返回 None 退化为指数退避）。"""
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _backoff_seconds(result: "FetchResult", attempt: int) -> float:
    """429/503 退避秒数：Retry-After 优先（封顶 60s），无则指数退避 + 抖动。"""
    ra = getattr(result, "retry_after", None)
    if ra is not None and ra >= 0:
        return min(ra, BACKOFF_MAX_SEC)
    expo = BACKOFF_BASE * (2 ** (attempt - 1)) + _random.random()
    return min(expo, BACKOFF_MAX_SEC)


def _should_retry(ctx: FetchContext, result: FetchResult,
                  attempt: int, max_attempts: int) -> bool:
    if attempt >= max_attempts:
        return False
    if result.failure and result.failure.code == PROXY_UNAVAILABLE and not result.proxy:
        return False
    if result.status in ctx.retry_statuses:
        return True
    return bool(result.failure and result.failure.code in (
        "network_timeout", "proxy_unavailable", "http_429", "http_5xx"))


def _should_stealth(result: FetchResult) -> bool:
    return bool(result.failure and result.failure.code in (
        HTTP_401, HTTP_403, HTTP_429, ANTI_BOT_CHALLENGE))
