from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ProxyHealth


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
