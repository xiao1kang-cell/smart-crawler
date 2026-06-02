from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.access import DEFAULT_API_KEY_SCOPES, require_api_key_scope
from app.apikey import generate, hash_key, short
from app.mcp_context import (
    McpApiKeyContext,
    reset_current_api_key,
    set_current_api_key,
)
from app.models import ApiKey


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
