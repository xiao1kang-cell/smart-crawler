from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crawl_diagnostics import FailureInfo, HTTP_403, STAGE_FETCH
from app.db import Base
from app.frontier import claim_urls, mark_failed, mark_parsed, register_urls, summary
from app.proxy_health import (
    proxy_hash,
    proxy_health_summary,
    record_proxy_result,
    unhealthy_proxy_hashes,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_frontier_register_claim_and_summary(session):
    register_urls(session, site="x", urls=["https://x.com/p/1", "https://x.com/p/2"],
                  source="sitemap", priority=10)
    session.commit()

    assert claim_urls(session, site="x", limit=1) == ["https://x.com/p/1"]

    mark_parsed(session, site="x", url="https://x.com/p/1")
    info = FailureInfo(HTTP_403, STAGE_FETCH, "blocked", True, "use proxy", 403)
    mark_failed(session, site="x", url="https://x.com/p/2", failure=info)
    session.commit()

    data = summary(session, site="x")
    assert data["by_status"]["parsed"] == 1
    assert data["by_status"]["failed"] == 1
    assert data["by_failure"][HTTP_403] == 1


def test_proxy_health_records_success_and_failure(session):
    proxy = "http://user:pass@127.0.0.1:3128"
    record_proxy_result(session, proxy_url=proxy, tier="residential", success=True)
    info = FailureInfo(HTTP_403, STAGE_FETCH, "blocked", True, "use proxy", 403)
    record_proxy_result(session, proxy_url=proxy, tier="residential",
                        success=False, failure=info)
    session.commit()

    data = proxy_health_summary(session)
    assert data["total"] == 1
    assert data["details"][0]["proxy"] == "http://user:****@127.0.0.1:3128"
    assert data["details"][0]["failure_count"] == 1


def test_unhealthy_proxy_hashes_returns_blocked_proxy(session):
    proxy = "http://user:pass@127.0.0.1:3128"
    info = FailureInfo("proxy_auth_failed", STAGE_FETCH, "bad auth", False,
                       "check credentials")
    record_proxy_result(session, proxy_url=proxy, tier="residential",
                        success=False, failure=info)
    session.commit()

    assert proxy_hash(proxy) in unhealthy_proxy_hashes(session)


def test_unhealthy_proxy_hashes_keeps_down_proxy_out_after_cooldown(session):
    from datetime import datetime, timedelta
    from app.models import ProxyHealth

    down_proxy = "http://user:pass@127.0.0.2:3128"
    degraded_proxy = "http://user:pass@127.0.0.3:3128"
    session.add(ProxyHealth(
        proxy_hash=proxy_hash(down_proxy),
        proxy_redacted=down_proxy,
        tier="residential",
        status="down",
        blocked_until=datetime.utcnow() - timedelta(minutes=1),
    ))
    session.add(ProxyHealth(
        proxy_hash=proxy_hash(degraded_proxy),
        proxy_redacted=degraded_proxy,
        tier="residential",
        status="degraded",
        blocked_until=datetime.utcnow() - timedelta(minutes=1),
    ))
    session.commit()

    unhealthy = unhealthy_proxy_hashes(session)
    assert proxy_hash(down_proxy) in unhealthy
    assert proxy_hash(degraded_proxy) not in unhealthy
