"""端到端集成：网页采集计费链路验证。

验证核心路径：_record_crawl_usage → billing.record_usage → Usage 行落库
且 api_key_id=None（不挂 key，不扣额度）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import billing, runner
from app.db import Base
from app.models import Usage

pytestmark = pytest.mark.unit


def test_web_crawl_writes_usage_without_charging_key(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(billing, "SessionLocal", Session)

    runner._record_crawl_usage(
        workspace_id=42, products_count=37, duration_sec=4.2,
        api_calls=15, browser_opens=3,
    )

    with Session() as s:
        row = s.query(Usage).filter(Usage.endpoint == "/crawl/job").first()
        assert row is not None
        assert row.api_key_id is None          # 关键：不挂 key → 不扣额度
        assert row.workspace_id == 42
        assert row.api_calls == 15
        assert row.browser_opens == 3
        assert row.pages_fetched == 18         # api_calls + browser_opens
        assert row.credits_used == 37
