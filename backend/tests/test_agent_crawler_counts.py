"""TDD tests: scrape_url 成功时回填 api_calls / browser_opens.

live scrape 成功  → api_calls=1, browser_opens=0
advanced scrape 成功 → api_calls=0, browser_opens=1
warehouse 命中     → api_calls=0, browser_opens=0  (不测，由现有测试覆盖)
extract_structured_data 聚合 → 累加子调用计数
"""
from __future__ import annotations

import pytest

from app import agent_crawler

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_live_success(url, **kw):
    return {
        "success": True,
        "crawl_url": url,
        "status_code": 200,
        "metadata": {},
        "structured": {"title": "x"},
        "markdown": "x",
        "html": "<html></html>",
        "links": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# scrape_url — standard mode → api_calls=1, browser_opens=0
# ---------------------------------------------------------------------------

def test_standard_scrape_counts_one_api_call(monkeypatch):
    monkeypatch.setattr(agent_crawler, "match_site", lambda db, url: None)
    monkeypatch.setattr(agent_crawler, "live_scrape_url", _fake_live_success)
    res = agent_crawler.scrape_url(None, "https://example.com/p/1")
    usage = res["usage"]
    assert usage["api_calls"] == 1
    assert usage["browser_opens"] == 0


# ---------------------------------------------------------------------------
# scrape_url — advanced mode → api_calls=0, browser_opens=1
# ---------------------------------------------------------------------------

def test_advanced_scrape_counts_one_browser_open(monkeypatch):
    monkeypatch.setattr(agent_crawler, "match_site", lambda db, url: None)
    monkeypatch.setattr(agent_crawler, "advanced_scrape_url", _fake_live_success)
    res = agent_crawler.scrape_url(None, "https://example.com/p/1", mode="advanced")
    usage = res["usage"]
    assert usage["browser_opens"] == 1
    assert usage["api_calls"] == 0


# ---------------------------------------------------------------------------
# extract_structured_data — cumulates counts across multiple URLs
# ---------------------------------------------------------------------------

def test_extract_structured_data_accumulates_counts(monkeypatch):
    """Two URLs → api_calls=2, browser_opens=0 when live scrape succeeds."""
    monkeypatch.setattr(agent_crawler, "match_site", lambda db, url: None)
    monkeypatch.setattr(agent_crawler, "live_scrape_url", _fake_live_success)
    res = agent_crawler.extract_structured_data(
        None,
        ["https://example.com/p/1", "https://example.com/p/2"],
    )
    usage = res["usage"]
    assert usage["api_calls"] == 2
    assert usage["browser_opens"] == 0
