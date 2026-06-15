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
