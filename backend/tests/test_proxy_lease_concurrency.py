from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
import app.models  # noqa: F401 — register all ORM classes in Base.metadata
from app.db import Base
from app.models import ProxyEndpoint

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
