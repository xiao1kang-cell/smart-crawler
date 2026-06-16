"""URL frontier service.

The frontier is a durable URL state machine.  Crawlers can register discovered
URLs and later claim retryable pending/failed URLs in priority order.  This is
the Scrapy-like scheduler layer without forcing a full framework migration.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .crawl_diagnostics import FailureInfo, hash_url, record_url_state
from .models import CrawlUrl

TERMINAL_STATUSES = {"parsed", "blocked"}
RETRYABLE_STATUSES = {"pending", "failed", "fetched"}


def register_urls(
    session: Session,
    *,
    site: str,
    urls: Iterable[str],
    kind: str = "product",
    source: str = "unknown",
    priority: int = 100,
) -> int:
    count = 0
    for url in urls:
        if not url:
            continue
        record_url_state(
            session,
            site=site,
            url=url,
            kind=kind,
            source=source,
            status="pending",
            priority=priority,
        )
        count += 1
    return count


def claim_urls(
    session: Session,
    *,
    site: str,
    limit: int,
    kinds: tuple[str, ...] | None = None,
    max_attempts: int = 3,
) -> list[str]:
    now = datetime.utcnow()
    q = (session.query(CrawlUrl)
         .filter(CrawlUrl.site == site)
         .filter(CrawlUrl.status.in_(RETRYABLE_STATUSES))
         .filter(CrawlUrl.attempts < max_attempts)
         .filter(or_(CrawlUrl.next_retry_at.is_(None),
                     CrawlUrl.next_retry_at <= now)))
    if kinds:
        q = q.filter(CrawlUrl.kind.in_(kinds))
    rows = (q.order_by(CrawlUrl.priority.asc(),
                       CrawlUrl.attempts.asc(),
                       CrawlUrl.id.asc())
            .limit(limit).all())
    return [r.url for r in rows if r.url]


def mark_parsed(session: Session, *, site: str, url: str) -> None:
    row = _get_row(session, site, url)
    if not row:
        return
    row.status = "parsed"
    row.failure_code = None
    row.failure_stage = None
    row.failure_detail = None
    row.retryable = None
    row.last_seen_at = datetime.utcnow()


def mark_failed(
    session: Session,
    *,
    site: str,
    url: str,
    failure: FailureInfo,
    retry_delay_sec: int = 900,
) -> None:
    row = record_url_state(
        session,
        site=site,
        url=url,
        status="failed",
        failure=failure,
        http_status=failure.http_status,
    )
    row.next_retry_at = (datetime.utcnow() + timedelta(seconds=retry_delay_sec)
                         if failure.retryable else None)


def summary(session: Session, *, site: str) -> dict:
    rows = session.query(CrawlUrl.status, CrawlUrl.failure_code).filter(
        CrawlUrl.site == site).all()
    status_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    for status, failure_code in rows:
        status_counts[status or "unknown"] = status_counts.get(status or "unknown", 0) + 1
        if failure_code:
            failure_counts[failure_code] = failure_counts.get(failure_code, 0) + 1
    return {
        "site": site,
        "total": len(rows),
        "by_status": status_counts,
        "by_failure": failure_counts,
    }


def _get_row(session: Session, site: str, url: str) -> CrawlUrl | None:
    return (session.query(CrawlUrl)
            .filter(CrawlUrl.site == site, CrawlUrl.url_hash == hash_url(url))
            .first())
