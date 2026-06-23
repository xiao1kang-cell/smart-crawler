"""Persistent proxy health tracking."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from .crawl_diagnostics import FailureInfo
from .models import ProxyEndpoint, ProxyHealth

_PROXY_HEALTH_FAILURE_CODES = {
    "proxy_auth_failed",
    "proxy_unavailable",
    "proxy_egress_mismatch",
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
    node: str = "nas",
) -> ProxyHealth | None:
    if not proxy_url:
        return None
    now = datetime.utcnow()
    h = proxy_hash(proxy_url)
    endpoint_tier = _endpoint_tier(session, h)
    health_tier = endpoint_tier or _normalized_health_tier(tier)
    row = (session.query(ProxyHealth)
           .filter(ProxyHealth.proxy_hash == h, ProxyHealth.node == node)
           .first())
    if row is None:
        row = ProxyHealth(
            proxy_hash=h,
            node=node,
            proxy_redacted=redact_proxy(proxy_url),
            tier=health_tier,
        )
        session.add(row)
    if health_tier:
        row.tier = health_tier
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
        row.blocked_until = now + timedelta(seconds=cooldown_sec)
    return row


def _endpoint_tier(session: Session, proxy_hash_value: str) -> str | None:
    row = (session.query(ProxyEndpoint.endpoint_type)
           .filter(ProxyEndpoint.proxy_hash == proxy_hash_value)
           .first())
    if not row:
        return None
    value = (row[0] or "").strip().lower()
    return value or None


def _normalized_health_tier(tier: str | None) -> str | None:
    """Normalize observed/requested proxy tier for legacy health rows.

    ``tier`` historically came from the site request (for example
    ``pool:residential``), not from the configured endpoint.  Keep it only as a
    fallback when the endpoint is unknown, and avoid persisting pool routing
    labels as if they were endpoint types.
    """
    value = (tier or "").strip().lower()
    if not value:
        return None
    if value.startswith("pool:"):
        value = value.split(":", 1)[1].strip()
    return value or None


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


def unhealthy_proxy_hashes(session: Session, node: str | None = None) -> set[str]:
    """返回不健康（blocked/down/未过冷却的 degraded）的代理 proxy_hash 集合。

    node：指定出口节点时只返回该节点判定为不健康的代理；node=None 表示
    跨所有节点合并（向后兼容回退，非等价于任何单节点）——调用方应传入
    明确的 node 值以获得按节点隔离的黑名单。
    """
    now = datetime.utcnow()
    query = session.query(ProxyHealth.proxy_hash).filter(
        or_(
            ProxyHealth.status == "blocked",
            ProxyHealth.status == "down",
            and_(
                ProxyHealth.status == "degraded",
                or_(
                    ProxyHealth.blocked_until.is_(None),
                    ProxyHealth.blocked_until > now,
                ),
            ),
        )
    )
    if node is not None:
        query = query.filter(ProxyHealth.node == node)
    rows = query.all()
    return {row[0] for row in rows if row[0]}


def proxy_hash(proxy_url: str) -> str:
    return hashlib.sha256(proxy_url.encode("utf-8", "ignore")).hexdigest()


def redact_proxy(proxy_url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", proxy_url)
