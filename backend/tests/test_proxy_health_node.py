from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crawl_diagnostics import FailureInfo, STAGE_FETCH
from app.db import Base
from app.models import ProxyHealth
from app.proxy_health import proxy_hash, record_proxy_result, unhealthy_proxy_hashes


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


def test_same_hash_different_node_coexist(session):
    """同一 proxy_hash 在不同 node 下可各存一行（唯一键是组合键）。"""
    session.add(ProxyHealth(proxy_hash="abc", node="nas", status="down"))
    session.add(ProxyHealth(proxy_hash="abc", node="US-macmini1", status="healthy"))
    session.commit()

    rows = session.query(ProxyHealth).filter(ProxyHealth.proxy_hash == "abc").all()
    assert len(rows) == 2
    by_node = {r.node: r.status for r in rows}
    assert by_node == {"nas": "down", "US-macmini1": "healthy"}


def _net_failure():
    return FailureInfo("network_timeout", STAGE_FETCH, "timeout", True, "retry")


def test_record_writes_per_node(session):
    """同一 proxy_url 在两个 node 上各记录独立健康行。"""
    url = "http://user:pass@1.2.3.4:8000"
    # nas 上连续 3 次失败 → down
    for _ in range(3):
        record_proxy_result(session, proxy_url=url, tier="residential",
                            success=False, failure=_net_failure(), node="nas")
    # mini 上成功
    record_proxy_result(session, proxy_url=url, tier="residential",
                        success=True, node="US-macmini1")
    session.commit()

    rows = session.query(ProxyHealth).all()
    assert len(rows) == 2
    by_node = {r.node: r.status for r in rows}
    assert by_node["nas"] == "down"
    assert by_node["US-macmini1"] == "healthy"


def test_unhealthy_is_node_scoped(session):
    """nas 标 down 的 IP，查 mini node 的黑名单不应包含它。"""
    url = "http://user:pass@1.2.3.4:8000"
    for _ in range(3):
        record_proxy_result(session, proxy_url=url, tier="residential",
                            success=False, failure=_net_failure(), node="nas")
    record_proxy_result(session, proxy_url=url, tier="residential",
                        success=True, node="US-macmini1")
    session.commit()

    h = proxy_hash(url)
    assert h in unhealthy_proxy_hashes(session, node="nas")
    assert h not in unhealthy_proxy_hashes(session, node="US-macmini1")


def test_node_id_env(monkeypatch):
    """NODE_ID 从环境变量读取，默认 nas。"""
    import importlib
    import app.proxy_pool as pp
    monkeypatch.setenv("NODE_ID", "US-macmini1")
    importlib.reload(pp)
    assert pp.NODE_ID == "US-macmini1"
    monkeypatch.delenv("NODE_ID", raising=False)
    importlib.reload(pp)
    assert pp.NODE_ID == "nas"
