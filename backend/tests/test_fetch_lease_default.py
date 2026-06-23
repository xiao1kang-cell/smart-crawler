from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _make_ctx():
    from app.fetching import FetchContext
    from app.models import Site
    site = Site(site="x", url="https://x.com", platform="generic")
    return FetchContext(site=site)


def test_lease_ttl_defaults_to_300(monkeypatch):
    monkeypatch.delenv("PROXY_LEASE_TTL_SEC", raising=False)
    ctx = _make_ctx()
    assert ctx.proxy_lease_ttl_sec == 300


def test_lease_ttl_env_override(monkeypatch):
    monkeypatch.setenv("PROXY_LEASE_TTL_SEC", "120")
    ctx = _make_ctx()
    assert ctx.proxy_lease_ttl_sec == 120


def test_lease_ttl_zero_disables(monkeypatch):
    monkeypatch.setenv("PROXY_LEASE_TTL_SEC", "0")
    ctx = _make_ctx()
    assert ctx.proxy_lease_ttl_sec == 0
