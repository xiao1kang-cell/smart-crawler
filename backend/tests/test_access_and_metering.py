from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.access import DEFAULT_API_KEY_SCOPES, require_api_key_scope
from app.apikey import generate, hash_key, short
from app.db import Base
from app.mcp_context import (
    McpApiKeyContext,
    reset_current_api_key,
    set_current_api_key,
)
from app.models import ApiKey, Product, Site, Usage


pytestmark = pytest.mark.unit


def _api_key(scopes=None) -> ApiKey:
    raw = generate()
    return ApiKey(
        id=1,
        name="test-key",
        key_prefix=short(raw),
        key_hash=hash_key(raw),
        scopes=scopes,
        active=True,
    )


def test_default_api_key_scopes_allow_scrape_but_not_crawl():
    key = _api_key(scopes=None)

    require_api_key_scope(key, "crawler:scrape")

    with pytest.raises(HTTPException) as exc:
        require_api_key_scope(key, "crawler:crawl")
    assert exc.value.status_code == 403
    assert exc.value.detail["required_scope"] == "crawler:crawl"
    assert exc.value.detail["granted_scopes"] == DEFAULT_API_KEY_SCOPES


def test_admin_scope_allows_crawl():
    key = _api_key(scopes=["admin:*"])

    require_api_key_scope(key, "crawler:crawl")


def test_mcp_usage_records_tool_endpoint(monkeypatch):
    from app import mcp_server

    calls = []

    def fake_record_usage(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(mcp_server, "record_usage", fake_record_usage)

    mcp_server._record_mcp_usage(
        api_key_id=7,
        tool_name="query_crawler_warehouse",
        result={
            "success": True,
            "data": [{"sku": "A"}, {"sku": "B"}],
            "usage": {"records": 2, "credits_used": 1},
        },
        duration_ms=12,
    )

    assert calls == [{
        "api_key_id": 7,
        "endpoint": "/mcp/query_crawler_warehouse",
        "record_count": 2,
        "credits_used": 1,
        "bytes_returned": calls[0]["bytes_returned"],
        "duration_ms": 12,
        "api_calls": 0,
        "browser_opens": 0,
        "pages_fetched": 0,
    }]
    assert calls[0]["bytes_returned"] > 0


def test_mcp_crawl_execution_requires_crawl_scope(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "record_usage", lambda **_: None)
    token = set_current_api_key(McpApiKeyContext(
        api_key_id=1,
        name="read-scrape-key",
        scopes=["crawler:read", "crawler:scrape"],
    ))
    try:
        result = mcp_server.crawl_site("https://example.com/", dry_run=False)
    finally:
        reset_current_api_key(token)

    assert result["error"] == "insufficient_scope"
    assert result["required_scope"] == "crawler:crawl"


def test_mcp_cache_payload_normalizes_positional_and_keyword_args():
    from app import mcp_server

    def scrape_url(url: str, formats=None, force_live: bool = False):
        return url, formats, force_live

    positional = mcp_server._normalized_tool_payload(
        scrape_url, ("https://example.com/",), {"formats": ["markdown"]})
    keyword = mcp_server._normalized_tool_payload(
        scrape_url, (), {"url": "https://example.com/", "formats": ["markdown"]})

    assert positional == keyword
    assert positional["force_live"] is False


def test_monthly_credit_quota_must_be_nonnegative_int():
    from app.api.routes import _parse_monthly_credit_quota

    assert _parse_monthly_credit_quota(None) is None
    assert _parse_monthly_credit_quota("20") == 20

    for bad in (-1, "abc", True):
        with pytest.raises(HTTPException) as exc:
            _parse_monthly_credit_quota(bad)
        assert exc.value.status_code == 400


def test_update_key_allows_admin_to_change_scopes_quota_and_active():
    from app.api.routes import update_key

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(ApiKey(id=3, name="old", key_prefix="sck_old",
                  key_hash="hash", active=True,
                  scopes=["crawler:read"],
                  monthly_credit_quota=None))
    db.commit()

    result = update_key(
        3,
        {
            "name": "external-customer",
            "scopes": ["crawler:read", "crawler:scrape", "crawler:crawl"],
            "monthly_credit_quota": "5000",
            "active": False,
        },
        user="admin",
        db=db,
    )

    assert result["name"] == "external-customer"
    assert result["active"] is False
    assert result["scopes"] == ["crawler:crawl", "crawler:read", "crawler:scrape"]
    assert result["monthly_credit_quota"] == 5000

    with pytest.raises(HTTPException) as exc:
        update_key(3, {"active": True}, user="apikey:test", db=db)
    assert exc.value.status_code == 403


def test_usage_summary_reports_credits_balance_and_legacy_record_cost(monkeypatch):
    from app import billing

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(ApiKey(id=9, name="quota", key_prefix="sck_q",
                  key_hash="hash", active=True,
                  monthly_credit_quota=3000))
    db.add(Usage(api_key_id=9, endpoint="/api/v2/scrape",
                 record_count=100, credits_used=2000,
                 bytes_returned=1234, duration_ms=50))
    db.commit()
    db.close()
    monkeypatch.setattr(billing, "SessionLocal", Session)

    summary = billing.get_usage_summary(9, days=30)

    assert summary["billing_basis"] == "credits"
    assert summary["total_records"] == 100
    assert summary["total_credits"] == 2000
    assert summary["monthly_credit_quota"] == 3000
    assert summary["credit_balance"] == 1000
    assert summary["cost_usd"] == 3.0
    assert summary["estimated_cost_usd_by_credits"] == 3.0
    assert summary["estimated_cost_usd_by_records"] == 0.15


def _mcp_contract_session(monkeypatch):
    from app import mcp_server

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(ApiKey(
        id=1,
        name="agent-key",
        key_prefix="sck_test",
        key_hash="hash",
        scopes=["crawler:read", "crawler:scrape"],
        active=True,
        monthly_credit_quota=100,
    ))
    db.add(Site(site="example_us", brand="Example", country="US",
                url="https://example.com/", platform="generic",
                proxy_tier="none"))
    db.add(Product(site="example_us", brand="Example", sku="CHAIR-001",
                   title="Warehouse Chair",
                   product_url="https://example.com/products/patio-chair",
                   sale_price=42.0, currency="USD"))
    db.commit()
    db.close()
    monkeypatch.setattr(mcp_server, "SessionLocal", Session)
    monkeypatch.setattr(mcp_server, "record_usage", lambda **_: None)
    return mcp_server


def _assert_usage_contract(result: dict):
    usage = result.get("usage") or {}
    for key in (
        "credits_used",
        "balance",
        "cache_hit",
        "source",
        "records",
        "duration_ms",
        "cost_if_retry",
    ):
        assert key in usage


def _assert_warning_contract(result: dict):
    warnings = result.get("warnings") or []
    assert warnings
    for key in ("code", "message", "next_step"):
        assert key in warnings[0]


def test_primary_mcp_tools_return_usage_contract(monkeypatch):
    mcp_server = _mcp_contract_session(monkeypatch)
    token = set_current_api_key(McpApiKeyContext(
        api_key_id=1,
        name="agent-key",
        scopes=["crawler:read", "crawler:scrape"],
    ))
    try:
        warehouse = mcp_server.query_warehouse("Warehouse Chair", limit=5)
        scrape_1 = mcp_server.scrape_url("https://example.com/products/patio-chair")
        scrape_2 = mcp_server.scrape_url("https://example.com/products/patio-chair")
        crawl = mcp_server.crawl_site("https://example.com/")
    finally:
        reset_current_api_key(token)

    for result in (warehouse, scrape_1, scrape_2, crawl):
        assert result["success"] is True
        _assert_usage_contract(result)

    assert warehouse["usage"]["credits_used"] == 0
    assert scrape_1["usage"]["source"] == "warehouse"
    assert scrape_2["usage"]["source"] == "agent_memory"
    assert scrape_2["usage"]["cache_hit"] is True
    assert scrape_2["usage"]["credits_used"] == 0
    assert crawl["status"] == "dry_run"
    assert crawl["usage"]["credits_used"] == 0


def test_primary_mcp_scope_error_has_agent_next_step(monkeypatch):
    mcp_server = _mcp_contract_session(monkeypatch)
    token = set_current_api_key(McpApiKeyContext(
        api_key_id=1,
        name="agent-key",
        scopes=["crawler:read", "crawler:scrape"],
    ))
    try:
        result = mcp_server.crawl_site("https://example.com/", dry_run=False)
    finally:
        reset_current_api_key(token)

    assert result["success"] is False
    assert result["error"] == "insufficient_scope"
    assert result["required_scope"] == "crawler:crawl"
    assert result["granted_scopes"] == ["crawler:read", "crawler:scrape"]
    _assert_usage_contract(result)
    _assert_warning_contract(result)


def test_mcp_tool_descriptions_are_agent_first_guarded():
    from app import mcp_server

    primary = {
        "query_warehouse": ("Agent 推荐只传", "credits", "warehouse"),
        "scrape_url": ("Agent 推荐只传", "credits", "warehouse"),
        "crawl_site": ("Agent 推荐只传", "credits", "dry_run"),
    }
    for name, needles in primary.items():
        doc = getattr(mcp_server, name).__doc__ or ""
        for needle in needles:
            assert needle in doc

    advanced = (
        "map_site",
        "extract_structured_data",
        "get_crawl_job",
    )
    for name in advanced:
        assert "[ADVANCED]" in (getattr(mcp_server, name).__doc__ or "")

    legacy = (
        "query_crawler_warehouse",
        "list_data_sources",
        "search_competitor_products",
    )
    for name in legacy:
        assert "[LEGACY]" in (getattr(mcp_server, name).__doc__ or "")

    instructions = mcp_server.mcp.instructions
    assert "query_warehouse(intent, limit)" in instructions
    assert "scrape_url(url)" in instructions
    assert "crawl_site(url)" in instructions
