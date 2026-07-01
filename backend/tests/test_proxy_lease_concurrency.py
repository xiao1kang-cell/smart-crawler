from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
import app.models  # noqa: F401 — register all ORM classes in Base.metadata
from app.db import Base
from app.models import ProxyEndpoint, ProxyLease

pytestmark = pytest.mark.unit


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # _try_create_proxy_lease 内部做 `from .db import SessionLocal`，
    # monkeypatch 必须替换 app.db 模块上的属性，使该符号在调用时拿到测试引擎。
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_max_concurrency_one_blocks_second_lease(session):
    """max_concurrency=1：第一次 lease 成功，未释放时第二次拿不到（跨进程防撞核心）。"""
    ep = ProxyEndpoint(
        name="ep1",
        proxy_hash="h1",
        proxy_url="http://1.2.3.4:8000",
        endpoint_type="residential",
        max_concurrency=1,
        active=True,
    )
    session.add(ep)
    session.commit()

    from app.proxy_pool import _try_create_proxy_lease, ProxyEntry

    cand = [ProxyEntry(url="http://1.2.3.4:8000", tier="residential", id=ep.id)]

    h1 = _try_create_proxy_lease(cand, site="x", job_id=None, worker="w1", ttl_sec=300)
    assert h1 is not None, "first lease must succeed"
    assert h1.lease_token, "lease handle must carry a token"

    h2 = _try_create_proxy_lease(cand, site="x", job_id=None, worker="w2", ttl_sec=300)
    assert h2 is None, "second lease must be blocked (max_concurrency=1, first not released)"


def test_max_concurrency_two_allows_two_leases(session):
    """max_concurrency=2：两次 lease 都成功，第三次被挡。"""
    ep = ProxyEndpoint(
        name="ep2",
        proxy_hash="h2",
        proxy_url="http://5.6.7.8:8000",
        endpoint_type="residential",
        max_concurrency=2,
        active=True,
    )
    session.add(ep)
    session.commit()

    from app.proxy_pool import _try_create_proxy_lease, ProxyEntry

    cand = [ProxyEntry(url="http://5.6.7.8:8000", tier="residential", id=ep.id)]

    h1 = _try_create_proxy_lease(cand, site="x", job_id=None, worker="w1", ttl_sec=300)
    h2 = _try_create_proxy_lease(cand, site="x", job_id=None, worker="w2", ttl_sec=300)
    h3 = _try_create_proxy_lease(cand, site="x", job_id=None, worker="w3", ttl_sec=300)

    assert h1 is not None, "first lease must succeed"
    assert h2 is not None, "second lease must succeed (max_concurrency=2)"
    assert h3 is None, "third lease must be blocked (max_concurrency=2, two not released)"


def test_proxy_hourly_and_daily_quota_blocks_additional_leases(session):
    ep = ProxyEndpoint(
        name="ep-quota",
        proxy_hash="h-quota",
        proxy_url="http://9.9.9.9:8000",
        endpoint_type="residential",
        max_concurrency=10,
        active=True,
    )
    session.add(ep)
    session.commit()

    from app.proxy_pool import _try_create_proxy_lease, ProxyEntry

    cand = [ProxyEntry(url="http://9.9.9.9:8000", tier="residential", id=ep.id)]

    h1 = _try_create_proxy_lease(
        cand,
        site="amazon",
        job_id=None,
        worker="w1",
        ttl_sec=300,
        hourly_limit=1,
        daily_limit=100,
    )
    assert h1 is not None

    h2 = _try_create_proxy_lease(
        cand,
        site="amazon",
        job_id=None,
        worker="w2",
        ttl_sec=300,
        hourly_limit=1,
        daily_limit=100,
    )
    assert h2 is None, "same endpoint is already at the hourly quota"

    session.add(ProxyLease(
        endpoint_id=ep.id,
        site="amazon",
        worker="old-worker",
        lease_token="old-daily-lease",
        expires_at=datetime.utcnow() - timedelta(hours=20),
        released_at=datetime.utcnow() - timedelta(hours=20),
        created_at=datetime.utcnow() - timedelta(hours=20),
        updated_at=datetime.utcnow() - timedelta(hours=20),
    ))
    session.commit()

    h3 = _try_create_proxy_lease(
        cand,
        site="amazon",
        job_id=None,
        worker="w3",
        ttl_sec=300,
        hourly_limit=0,
        daily_limit=2,
    )
    assert h3 is None, "same endpoint is already at the daily quota"


def test_proxy_entries_are_deduplicated_by_url():
    from app.proxy_pool import ProxyEntry, _dedupe_proxy_entries

    duplicate_file = ProxyEntry(
        url="http://dup.example:8000",
        tier="datacenter",
        exclude={"etsy"},
        source="file",
        pool_slugs={"legacy"},
    )
    duplicate_db = ProxyEntry(
        url="http://dup.example:8000",
        tier="datacenter",
        id=42,
        exclude={"amazon"},
        source="db",
        pool_slugs={"all"},
        max_concurrency=3,
    )
    unique = ProxyEntry(url="http://unique.example:8000", tier="datacenter")

    deduped = _dedupe_proxy_entries([duplicate_file, duplicate_db, unique])

    assert len(deduped) == 2
    merged = next(p for p in deduped if p.url == "http://dup.example:8000")
    assert merged.id == 42
    assert merged.exclude == {"amazon", "etsy"}
    assert merged.pool_slugs == {"all", "legacy"}
    assert merged.max_concurrency == 3
