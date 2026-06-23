from __future__ import annotations

import pytest

from app.db import SessionLocal, init_db
from app.models import ProxyHealth

pytestmark = pytest.mark.unit


def test_proxy_probe_records_timeout(monkeypatch):
    init_db()
    import app.proxy_probe as probe

    # 清理残留数据，确保跨测试隔离（test_proxy_config_db 等可能留下脏行）
    _cleanup = SessionLocal()
    try:
        _cleanup.query(ProxyHealth).delete()
        _cleanup.commit()
    finally:
        _cleanup.close()

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


def test_proxy_probe_rejects_egress_mismatch(monkeypatch):
    init_db()
    import app.proxy_probe as probe

    failures = []
    monkeypatch.setattr(probe.proxy_pool, "report_failure",
                        lambda proxy, hard=False: failures.append((proxy, hard)))

    class FakeResp:
        status_code = 200
        text = "10.0.0.99"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.proxies = {}

        def get(self, url, timeout):
            assert url == "https://api.ipify.org"
            return FakeResp()

    monkeypatch.setattr(probe.creq, "Session", FakeSession)

    result = probe.probe_proxy_url(
        proxy_url="http://u:p@10.0.0.1:3128",
        tier="datacenter",
        url="https://api.ipify.org",
        timeout=1,
        expected_egress_ip="10.0.0.1",
    )

    assert result.ok is False
    assert result.expected_egress_ip == "10.0.0.1"
    assert result.observed_egress_ip == "10.0.0.99"
    assert result.failure is not None
    assert result.failure.code == "proxy_egress_mismatch"
    assert failures == [("http://u:p@10.0.0.1:3128", False)]
