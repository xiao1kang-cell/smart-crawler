from __future__ import annotations

import pytest

from app.db import SessionLocal, init_db
from app.models import ProxyHealth

pytestmark = pytest.mark.unit


def test_proxy_probe_records_timeout(monkeypatch):
    init_db()
    import app.proxy_probe as probe

    monkeypatch.setattr(probe.proxy_pool, "get_proxy",
                        lambda tier, site=None: "http://u:p@127.0.0.1:3128")
    monkeypatch.setattr(probe.proxy_pool, "report_failure",
                        lambda proxy, hard=False: None)

    class FakeSession:
        proxies = {}

        def __init__(self, *args, **kwargs):
            self.proxies = {}

        def get(self, url, timeout):
            raise TimeoutError("proxy timed out")

    monkeypatch.setattr(probe.creq, "Session", FakeSession)

    result = probe.probe_proxy_for_url(
        tier="residential",
        site="vidaxl_de",
        url="https://www.vidaxl.de/sitemap_index.xml",
        timeout=1,
    )

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.code == "network_timeout"

    s = SessionLocal()
    try:
        row = s.query(ProxyHealth).filter(ProxyHealth.tier == "residential").first()
        assert row is not None
        assert row.failure_count >= 1
        assert row.last_failure_code == "network_timeout"
    finally:
        s.close()


def test_proxy_probe_treats_target_401_as_not_ok(monkeypatch):
    init_db()
    import app.proxy_probe as probe

    failures = []
    successes = []
    monkeypatch.setattr(probe.proxy_pool, "get_proxy",
                        lambda tier, site=None: "http://u:p@127.0.0.1:3128")
    monkeypatch.setattr(probe.proxy_pool, "report_failure",
                        lambda proxy, hard=False: failures.append((proxy, hard)))
    monkeypatch.setattr(probe.proxy_pool, "report_success",
                        lambda proxy: successes.append(proxy))

    class FakeResp:
        status_code = 401

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.proxies = {}

        def get(self, url, timeout):
            return FakeResp()

    monkeypatch.setattr(probe.creq, "Session", FakeSession)

    result = probe.probe_proxy_for_url(
        tier="residential",
        site="vidaxl_us",
        url="https://www.vidaxl.com/sitemap_index.xml",
        timeout=1,
    )

    assert result.ok is False
    assert result.status_code == 401
    assert result.failure is not None
    assert result.failure.code == "http_401"
    assert successes
    assert not failures
