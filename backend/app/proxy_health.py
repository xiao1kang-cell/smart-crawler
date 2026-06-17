"""Persistent proxy health tracking."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from .crawl_diagnostics import FailureInfo
from .models import ProxyHealth

_PROXY_HEALTH_FAILURE_CODES = {
    "proxy_auth_failed",
    "proxy_unavailable",
    "network_timeout",
    "dns_error",
}


def is_proxy_health_failure(failure: FailureInfo | None) -> bool:
    """Whether a crawl failure should degrade proxy availability.

    Target-site blocks such as HTTP 401/403/429 or anti-bot challenge mean the
    chosen exit was not useful for that site, but the proxy itself still carried
    traffic. Marking those as proxy-down empties the residential pool and hides
    the real site-level blocker from operators.
    """
    if failure is None:
        return False
    return failure.code in _PROXY_HEALTH_FAILURE_CODES


def record_proxy_result(
    session: Session,
    *,
    proxy_url: str | None,
    tier: str | None,
    success: bool,
    failure: FailureInfo | None = None,
    cooldown_sec: int = 600,
) -> ProxyHealth | None:
    if not proxy_url:
        return None
    now = datetime.utcnow()
    row = (session.query(ProxyHealth)
           .filter(ProxyHealth.proxy_hash == proxy_hash(proxy_url))
           .first())
    if row is None:
        row = ProxyHealth(
            proxy_hash=proxy_hash(proxy_url),
            proxy_redacted=redact_proxy(proxy_url),
            tier=tier,
        )
        session.add(row)
    row.tier = tier or row.tier
    row.proxy_redacted = redact_proxy(proxy_url)
    row.last_checked_at = now
    row.updated_at = now
    if success or not is_proxy_health_failure(failure):
        row.status = "healthy"
        row.success_count = (row.success_count or 0) + 1
        row.consecutive_failures = 0
        row.last_success_at = now
        row.last_failure_code = None
        row.last_failure_detail = None
        row.blocked_until = None
        return row

    row.failure_count = (row.failure_count or 0) + 1
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_failure_at = now
    if failure:
        row.last_failure_code = failure.code
        row.last_failure_detail = failure.detail[:2000] if failure.detail else None
    if failure and failure.code == "proxy_auth_failed":
        row.status = "blocked"
        row.blocked_until = None
    elif row.consecutive_failures >= 3:
        row.status = "down"
        row.blocked_until = now + timedelta(seconds=cooldown_sec)
    else:
        row.status = "degraded"
    return row


def proxy_health_summary(session: Session) -> dict:
    rows = session.query(ProxyHealth).order_by(ProxyHealth.updated_at.desc()).all()
    by_status: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for row in rows:
        status = row.status or "unknown"
        tier = row.tier or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        tier_counts = by_tier.setdefault(tier, {})
        tier_counts[status] = tier_counts.get(status, 0) + 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_tier": by_tier,
        "details": [
            {
                "proxy": row.proxy_redacted,
                "tier": row.tier,
                "status": row.status,
                "success_count": row.success_count or 0,
                "failure_count": row.failure_count or 0,
                "consecutive_failures": row.consecutive_failures or 0,
                "last_failure_code": row.last_failure_code,
                "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                "blocked_until": row.blocked_until.isoformat() if row.blocked_until else None,
            }
            for row in rows[:50]
        ],
    }


def unhealthy_proxy_hashes(session: Session) -> set[str]:
    now = datetime.utcnow()
    rows = (session.query(ProxyHealth.proxy_hash)
            .filter(
                or_(
                    ProxyHealth.status.in_(("blocked", "down")),
                    and_(
                        ProxyHealth.status == "degraded",
                        ProxyHealth.blocked_until.is_not(None),
                        ProxyHealth.blocked_until > now,
                    ),
                )
            )
            .all())
    return {row[0] for row in rows if row[0]}


def proxy_hash(proxy_url: str) -> str:
    return hashlib.sha256(proxy_url.encode("utf-8", "ignore")).hexdigest()


def redact_proxy(proxy_url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", proxy_url)
