"""Short proxy preflight checks.

These checks are intentionally cheap and conservative. They avoid sending a
full crawler into a known-dead proxy path while still letting target-site
HTTP failures be classified by the normal fetch pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass

from curl_cffi import requests as creq

from . import proxy_pool
from .crawl_diagnostics import (
    FailureInfo,
    STAGE_FETCH,
    classify_exception,
    classify_http_status,
)
from .db import SessionLocal
from .proxy_health import is_proxy_health_failure, record_proxy_result


@dataclass(frozen=True)
class ProxyProbeResult:
    ok: bool
    proxy_url: str | None = None
    failure: FailureInfo | None = None
    status_code: int | None = None


def probe_proxy_for_url(
    *,
    tier: str | None,
    site: str | None,
    url: str,
    timeout: int = 8,
) -> ProxyProbeResult:
    """Pick a proxy and verify it can reach ``url`` within a short timeout."""
    proxy = proxy_pool.get_proxy(tier, site=site)
    if not proxy:
        failure = FailureInfo(
            "proxy_unavailable",
            STAGE_FETCH,
            f"无可用 {tier or 'unknown'} 代理",
            True,
            "检查代理池配置、冷却状态和代理余额/白名单",
        )
        return ProxyProbeResult(False, None, failure)

    sess = creq.Session(impersonate="chrome")
    sess.proxies = {"http": proxy, "https": proxy}
    try:
        resp = sess.get(url, timeout=timeout)
        if 200 <= resp.status_code < 400:
            proxy_pool.report_success(proxy)
            _record(proxy, tier, True, None)
            return ProxyProbeResult(True, proxy, status_code=resp.status_code)
        failure = classify_http_status(
            resp.status_code,
            f"目标预检返回 HTTP {resp.status_code}",
        ) or FailureInfo(
            "http_4xx",
            STAGE_FETCH,
            f"目标预检返回 HTTP {resp.status_code}",
            True,
            "目标站或代理出口异常，稍后重试或切换代理",
            resp.status_code,
        )
    except Exception as exc:
        failure = classify_exception(exc, stage=STAGE_FETCH)

    proxy_failed = is_proxy_health_failure(failure)
    if proxy_failed:
        proxy_pool.report_failure(proxy, hard=failure.code == "proxy_auth_failed")
    else:
        proxy_pool.report_success(proxy)
    _record(proxy, tier, not proxy_failed, failure)
    return ProxyProbeResult(False, proxy, failure,
                            status_code=failure.http_status)


def _record(proxy_url: str | None, tier: str | None,
            success: bool, failure: FailureInfo | None) -> None:
    db = SessionLocal()
    try:
        record_proxy_result(
            db,
            proxy_url=proxy_url,
            tier=tier,
            success=success,
            failure=failure,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
