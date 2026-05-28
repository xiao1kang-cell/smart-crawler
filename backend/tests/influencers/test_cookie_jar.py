"""Unit tests for cookie jar."""
from __future__ import annotations

import json

import pytest

from app.influencers.cookie_jar import (
    CookieExpiredError,
    invalidate,
    load_cookies,
    redact,
)


pytestmark = pytest.mark.unit


def _write_jar(tmp_path, name="ig.json"):
    path = tmp_path / name
    path.write_text(json.dumps([
        {"name": "sessionid", "value": "ABCDEFG123", "domain": ".instagram.com", "path": "/"},
        {"name": "csrftoken", "value": "xyz", "domain": ".instagram.com", "path": "/"},
    ]))
    return str(path)


def test_load_cookies_returns_dict_from_env(monkeypatch, tmp_path):
    path = _write_jar(tmp_path)
    monkeypatch.setenv("IG_COOKIES_PATH", path)
    invalidate("instagram")
    jar = load_cookies("instagram")
    assert jar["sessionid"] == "ABCDEFG123"
    assert jar["csrftoken"] == "xyz"


def test_load_cookies_caches(monkeypatch, tmp_path):
    path = _write_jar(tmp_path)
    monkeypatch.setenv("IG_COOKIES_PATH", path)
    invalidate("instagram")
    jar1 = load_cookies("instagram")  # noqa: F841
    (tmp_path / "ig.json").write_text(json.dumps([
        {"name": "sessionid", "value": "CHANGED", "domain": ".instagram.com", "path": "/"},
    ]))
    jar2 = load_cookies("instagram")
    assert jar2["sessionid"] == "ABCDEFG123"
    invalidate("instagram")
    jar3 = load_cookies("instagram")
    assert jar3["sessionid"] == "CHANGED"


def test_load_cookies_missing_env_raises(monkeypatch):
    monkeypatch.delenv("IG_COOKIES_PATH", raising=False)
    invalidate("instagram")
    with pytest.raises(CookieExpiredError) as ei:
        load_cookies("instagram")
    assert "instagram" in str(ei.value)


def test_load_cookies_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("FB_COOKIES_PATH", str(tmp_path / "nope.json"))
    invalidate("facebook")
    with pytest.raises(CookieExpiredError):
        load_cookies("facebook")


def test_redact_removes_cookie_values():
    s = "Cookie: sessionid=SECRETVAL; csrftoken=xyz"
    out = redact(s, {"sessionid": "SECRETVAL", "csrftoken": "xyz"})
    assert "SECRETVAL" not in out
    assert "xyz" not in out
    assert "[REDACTED]" in out
