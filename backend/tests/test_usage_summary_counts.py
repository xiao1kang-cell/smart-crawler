"""TDD B1 — 用量 summary 端点暴露 api_calls/browser_opens/pages_fetched 三个计数。

覆盖：
  · billing.get_usage_summary（前台 AccountPage 用）
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import billing
from app.db import Base
from app.models import ApiKey, Usage

pytestmark = pytest.mark.unit


@pytest.fixture
def mem(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(billing, "SessionLocal", Session)
    return Session


def test_get_usage_summary_aggregates_counts(mem):
    """billing.get_usage_summary 应聚合三个计数字段。"""
    with mem() as s:
        s.add(ApiKey(id=1, name="k", key_prefix="x", key_hash="h", active=True))
        s.add(Usage(api_key_id=1, endpoint="/api/v2/scrape", record_count=1,
                    credits_used=2, bytes_returned=0, duration_ms=10,
                    api_calls=1, browser_opens=0, pages_fetched=1))
        s.add(Usage(api_key_id=1, endpoint="/api/v2/scrape", record_count=1,
                    credits_used=3, bytes_returned=0, duration_ms=20,
                    api_calls=0, browser_opens=1, pages_fetched=1))
        s.commit()
    summary = billing.get_usage_summary(1, days=30)
    assert summary["total_api_calls"] == 1
    assert summary["total_browser_opens"] == 1
    assert summary["total_pages_fetched"] == 2


def test_get_usage_summary_counts_default_zero(mem):
    """api_key 存在但没有用量记录时，三个计数应为 0。"""
    with mem() as s:
        s.add(ApiKey(id=2, name="empty", key_prefix="y", key_hash="h2", active=True))
        s.commit()
    summary = billing.get_usage_summary(2, days=30)
    assert summary["total_api_calls"] == 0
    assert summary["total_browser_opens"] == 0
    assert summary["total_pages_fetched"] == 0
