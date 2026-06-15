from __future__ import annotations

import pytest

from app import runner

pytestmark = pytest.mark.unit


def test_record_crawl_usage_emits_row(monkeypatch):
    captured = {}

    def fake_record_usage(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(runner, "record_usage", fake_record_usage)

    runner._record_crawl_usage(
        workspace_id=7, products_count=42, duration_sec=3.5,
        api_calls=12, browser_opens=2,
    )

    assert captured["api_key_id"] is None
    assert captured["workspace_id"] == 7
    assert captured["endpoint"] == "/crawl/job"
    assert captured["api_calls"] == 12
    assert captured["browser_opens"] == 2
    assert captured["pages_fetched"] == 14
    assert captured["credits_used"] == 42


def test_record_crawl_usage_credits_floor(monkeypatch):
    captured = {}
    monkeypatch.setattr(runner, "record_usage",
                        lambda **kw: captured.update(kw))
    runner._record_crawl_usage(workspace_id=None, products_count=0,
                               duration_sec=1.0, api_calls=0, browser_opens=0)
    assert captured["credits_used"] == 1


def test_record_crawl_usage_never_raises(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner, "record_usage", boom)
    runner._record_crawl_usage(workspace_id=1, products_count=1,
                               duration_sec=1.0, api_calls=1, browser_opens=0)
    # 通过条件：record_usage 抛错时 _record_crawl_usage 不上浮异常


def test_record_crawl_usage_credits_ceiling(monkeypatch):
    captured = {}
    monkeypatch.setattr(runner, "record_usage", lambda **kw: captured.update(kw))
    runner._record_crawl_usage(workspace_id=1, products_count=50_000,
                               duration_sec=10.0, api_calls=0, browser_opens=0)
    assert captured["credits_used"] == 10_000
