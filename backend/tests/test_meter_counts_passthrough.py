from __future__ import annotations

import pytest

from app.api import v2

pytestmark = pytest.mark.unit


def test_meter_passes_counts(monkeypatch):
    captured = {}
    monkeypatch.setattr(v2, "record_usage",
                        lambda **kw: captured.update(kw))

    class _Key:
        id = 1
    monkeypatch.setattr(v2, "_api_key_row", lambda *a, **k: _Key())

    result = {
        "usage": {"credits_used": 2, "records": 1, "duration_ms": 100,
                  "api_calls": 1, "browser_opens": 0},
        "data": {"x": 1},
    }
    # _meter 真实签名: (db, authorization, x_api_key, endpoint, result)
    v2._meter(None, "Bearer t", "", "/api/v2/scrape", result)

    assert captured["api_calls"] == 1
    assert captured["browser_opens"] == 0
    assert captured["pages_fetched"] == 1


def test_mcp_passes_counts(monkeypatch):
    """MCP 路径: _record_mcp_usage 正确透传 api_calls/browser_opens/pages_fetched。"""
    from app import mcp_server

    captured = {}
    monkeypatch.setattr(mcp_server, "record_usage",
                        lambda **kw: captured.update(kw))

    result = {
        "usage": {"credits_used": 3, "records": 1, "api_calls": 3, "browser_opens": 1},
        "data": {"x": 1},
    }
    # _record_mcp_usage 签名: (api_key_id, tool_name, result, duration_ms)
    mcp_server._record_mcp_usage(42, "scrape_url", result, 250)

    assert captured["api_calls"] == 3
    assert captured["browser_opens"] == 1
    assert captured["pages_fetched"] == 4  # api_calls(3) + browser_opens(1)


def test_spine_passes_counts(monkeypatch):
    """Spine worker 路径: _record_execute_usage 正确透传 api_calls/browser_opens/pages_fetched。

    record_usage 在函数体内用 `from .billing import record_usage` 局部导入,
    因此通过 app.billing 模块级打桩来拦截调用。
    """
    from app import spine_queue
    import app.billing as billing_mod

    captured = {}
    monkeypatch.setattr(billing_mod, "record_usage",
                        lambda **kw: captured.update(kw))

    out = {
        "credits_used": 2,
        "record_id": 99,
        "api_calls": 2,
        "browser_opens": 0,
    }
    # _record_execute_usage 签名: (api_key_id, workspace_id, out)
    spine_queue._record_execute_usage(7, 3, out)

    assert captured["api_calls"] == 2
    assert captured["browser_opens"] == 0
    assert captured["pages_fetched"] == 2  # api_calls(2) + browser_opens(0)
