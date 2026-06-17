from app.db import SessionLocal, init_db


def _clean_proxy_config(s):
    from app.models import (ProxyEndpoint, ProxyHealth, ProxyPoolConfig,
                            ProxyPoolMember, ProxyRule)

    s.query(ProxyPoolMember).delete()
    s.query(ProxyRule).delete()
    s.query(ProxyEndpoint).delete()
    s.query(ProxyPoolConfig).delete()
    s.query(ProxyHealth).delete()
    s.commit()


def test_import_proxy_file_creates_endpoints_and_default_pools(tmp_path):
    init_db()
    from app.models import ProxyEndpoint, ProxyPoolConfig, ProxyPoolMember
    from app.proxy_config import import_proxy_file

    f = tmp_path / "proxies.txt"
    f.write_text("""
[datacenter]
http://u:p@10.0.0.1:3128 # no:amazon
[residential]
socks5://u:p@10.0.0.2:1080
""", encoding="utf-8")
    s = SessionLocal()
    _clean_proxy_config(s)

    result = import_proxy_file(s, f)
    s.commit()

    assert result["added"] == 2
    assert s.query(ProxyEndpoint).count() == 2
    assert {p.slug for p in s.query(ProxyPoolConfig).all()} >= {"datacenter", "residential", "all"}
    assert s.query(ProxyPoolMember).count() >= 4
    dc = s.query(ProxyEndpoint).filter(ProxyEndpoint.endpoint_type == "datacenter").first()
    assert dc.exclude_sites == ["amazon"]
    s.close()


def test_proxy_pool_uses_db_and_rule_override(tmp_path):
    init_db()
    from app.models import ProxyRule
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_pool import ProxyPool

    s = SessionLocal()
    _clean_proxy_config(s)
    upsert_proxy_endpoint(s, proxy_url="http://u:p@10.0.1.1:3128",
                          endpoint_type="datacenter", source="test")
    upsert_proxy_endpoint(s, proxy_url="http://u:p@10.0.1.2:3128",
                          endpoint_type="residential", source="test")
    s.add(ProxyRule(site_pattern="vidaxl_ca", match_type="exact",
                    proxy_mode="pool", pool_slug="datacenter",
                    priority=1, enabled=True))
    s.commit()
    s.close()

    pool = ProxyPool(prefer_db=True)

    assert pool.get("residential", site="vidaxl_ca") == "http://u:p@10.0.1.1:3128"


def test_proxy_pool_excludes_persistently_down_endpoint(tmp_path):
    init_db()
    from datetime import datetime, timedelta
    from app.models import ProxyHealth
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_health import proxy_hash, redact_proxy
    from app.proxy_pool import ProxyPool

    down_proxy = "http://u:p@10.0.2.1:3128"
    healthy_proxy = "http://u:p@10.0.2.2:3128"
    s = SessionLocal()
    _clean_proxy_config(s)
    upsert_proxy_endpoint(s, proxy_url=down_proxy,
                          endpoint_type="residential", source="test")
    upsert_proxy_endpoint(s, proxy_url=healthy_proxy,
                          endpoint_type="residential", source="test")
    s.add(ProxyHealth(
        proxy_hash=proxy_hash(down_proxy),
        proxy_redacted=redact_proxy(down_proxy),
        tier="residential",
        status="down",
        blocked_until=datetime.utcnow() - timedelta(minutes=1),
        updated_at=datetime.utcnow(),
    ))
    s.commit()
    s.close()

    pool = ProxyPool(prefer_db=True, use_persistent_health=True)

    assert pool.get("residential", site="vidaxl_ca") == healthy_proxy
    status = pool.status()
    rows = {row["url"]: row for row in status["details"]}
    assert rows["http://u:****@10.0.2.1:3128"]["available"] is False
    assert rows["http://u:****@10.0.2.2:3128"]["available"] is True


def test_proxy_pool_uses_rule_fallback_when_primary_pool_unavailable(tmp_path):
    init_db()
    from datetime import datetime, timedelta
    from app.models import ProxyHealth, ProxyRule
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_health import proxy_hash, redact_proxy
    from app.proxy_pool import ProxyPool

    residential = "http://u:p@10.0.3.1:3128"
    datacenter = "http://u:p@10.0.3.2:3128"
    s = SessionLocal()
    _clean_proxy_config(s)
    upsert_proxy_endpoint(s, proxy_url=residential,
                          endpoint_type="residential", source="test")
    upsert_proxy_endpoint(s, proxy_url=datacenter,
                          endpoint_type="datacenter", source="test")
    s.add(ProxyRule(site_pattern="vidaxl_us", match_type="exact",
                    proxy_mode="pool", pool_slug="residential",
                    fallback_pool_slug="datacenter",
                    priority=1, enabled=True))
    s.add(ProxyHealth(
        proxy_hash=proxy_hash(residential),
        proxy_redacted=redact_proxy(residential),
        tier="residential",
        status="down",
        blocked_until=datetime.utcnow() - timedelta(minutes=1),
        updated_at=datetime.utcnow(),
    ))
    s.commit()
    s.close()

    pool = ProxyPool(prefer_db=True, use_persistent_health=True)

    assert pool.get("residential", site="vidaxl_us") == datacenter


def test_admin_proxy_endpoint_create_redacts_secret():
    init_db()
    from app.api import admin_spine

    s = SessionLocal()
    _clean_proxy_config(s)

    out = admin_spine.proxy_endpoint_create(
        payload={
            "proxy_url": "http://user:secret@example.test:3128",
            "endpoint_type": "residential",
            "provider": "provider-x",
        },
        user="admin",
        db=s,
        ip="127.0.0.1",
    )

    endpoint = out["endpoints"][0]
    assert endpoint["endpoint_type"] == "residential"
    assert "secret" not in endpoint["proxy"]
    assert endpoint["proxy"] == "http://user:****@example.test:3128"
    s.close()


def test_admin_proxy_pools_expose_effective_fallback_availability():
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.models import ProxyHealth, ProxyPoolConfig
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_health import proxy_hash, redact_proxy
    from app.proxy_pool import reload_pool

    residential = "http://u:p@10.0.4.1:3128"
    datacenter = "http://u:p@10.0.4.2:3128"
    s = SessionLocal()
    _clean_proxy_config(s)
    upsert_proxy_endpoint(s, proxy_url=residential,
                          endpoint_type="residential", source="test")
    upsert_proxy_endpoint(s, proxy_url=datacenter,
                          endpoint_type="datacenter", source="test")
    residential_pool = (s.query(ProxyPoolConfig)
                        .filter(ProxyPoolConfig.slug == "residential")
                        .one())
    residential_pool.fallback_pool_slug = "datacenter"
    s.add(ProxyHealth(
        proxy_hash=proxy_hash(residential),
        proxy_redacted=redact_proxy(residential),
        tier="residential",
        status="down",
        blocked_until=datetime.utcnow() + timedelta(minutes=10),
        updated_at=datetime.utcnow(),
    ))
    s.commit()
    reload_pool()

    out = admin_spine.proxies_status(user="admin", db=s)
    pools = {row["slug"]: row for row in out["pools"]}

    residential_payload = pools["residential"]
    assert residential_payload["available_count"] == 0
    assert residential_payload["primary_available_count"] == 0
    assert residential_payload["fallback_pool_slug"] == "datacenter"
    assert residential_payload["fallback_available_count"] == 1
    assert residential_payload["effective_available_count"] == 1
    assert residential_payload["effective_status"] == "fallback_available"

    s.close()


def test_admin_proxy_endpoint_check_records_endpoint_health(monkeypatch):
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.models import ProxyHealth
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_health import proxy_hash, redact_proxy
    from app import proxy_probe

    proxy = "http://u:p@10.0.5.1:3128"

    class FakeResponse:
        status_code = 204

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.proxies = {}

        def get(self, url, timeout):
            assert self.proxies == {"http": proxy, "https": proxy}
            assert url == "https://probe.example.test/health"
            assert timeout == 5
            return FakeResponse()

    monkeypatch.setattr(proxy_probe.creq, "Session", FakeSession)

    s = SessionLocal()
    _clean_proxy_config(s)
    endpoint = upsert_proxy_endpoint(
        s,
        proxy_url=proxy,
        endpoint_type="residential",
        source="test",
    )
    s.add(ProxyHealth(
        proxy_hash=proxy_hash(proxy),
        proxy_redacted=redact_proxy(proxy),
        tier="residential",
        status="down",
        blocked_until=datetime.utcnow() + timedelta(minutes=10),
        consecutive_failures=3,
        updated_at=datetime.utcnow(),
    ))
    s.commit()

    out = admin_spine.proxy_endpoint_check(
        endpoint.id,
        {"url": "https://probe.example.test/health", "timeout": 5},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    s.expire_all()
    health = (s.query(ProxyHealth)
              .filter(ProxyHealth.proxy_hash == proxy_hash(proxy))
              .one())
    endpoint_payload = next(row for row in out["endpoints"] if row["id"] == endpoint.id)

    assert out["probe"]["ok"] is True
    assert out["probe"]["endpoint_id"] == endpoint.id
    assert health.status == "healthy"
    assert health.consecutive_failures == 0
    assert health.blocked_until is None
    assert endpoint_payload["health_status"] == "healthy"

    s.close()
