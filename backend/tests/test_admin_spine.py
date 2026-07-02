"""后台管理系统(admin spine)测试。"""
from app.db import SessionLocal, init_db


def test_admin_audit_log_table_and_record():
    init_db()
    from sqlalchemy import inspect
    from app.db import engine
    cols = {c["name"] for c in inspect(engine).get_columns("admin_audit_logs")}
    for c in ("id", "actor_user_id", "actor_name", "action", "target_type",
              "target_id", "detail", "ip", "created_at"):
        assert c in cols, f"admin_audit_logs 缺列 {c}"
    from app.audit import record_audit
    from app.models import AdminAuditLog
    s = SessionLocal()
    n0 = s.query(AdminAuditLog).count()
    record_audit(s, actor_user_id=1, actor_name="admin", action="test.action",
                 target_type="job", target_id="42", detail={"k": "v"}, ip="1.2.3.4")
    s.commit()
    n1 = s.query(AdminAuditLog).count()
    assert n1 == n0 + 1
    row = s.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
    assert row.action == "test.action" and row.target_id == "42"
    assert row.detail == {"k": "v"} and row.actor_name == "admin"
    s.close()


def test_jobs_stats_requires_super_admin():
    from fastapi.testclient import TestClient
    from app.main import app
    init_db()
    client = TestClient(app)
    r = client.get("/api/admin/spine/jobs/stats")
    assert r.status_code in (401, 403)


def test_jobs_stats_ok_for_admin():
    init_db()
    from app.api.admin_spine import jobs_stats
    from app.db import SessionLocal
    s = SessionLocal()
    out = jobs_stats(user="admin", db=s)
    s.close()
    for k in ("pending", "running", "success", "failed", "stuck"):
        assert k in out


def test_site_crawler_config_update_masks_sensitive_values():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import AdminAuditLog, Site

    s = SessionLocal()
    site = s.query(Site).filter(Site.site == "vidaxl_us").first()
    assert site is not None
    site.crawler_config = {}
    s.commit()

    before = s.query(AdminAuditLog).count()
    out = admin_spine.site_crawler_config_update(
        "vidaxl_us",
        {"feed_url": "https://feed.example.com/very-secret-feed-token.csv",
         "price_feed_url": "https://feed.example.com/private-price-feed.csv",
         "api_token": "super-secret-token",
         "proxy_tier": "residential"},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    s.refresh(site)
    assert site.proxy_tier == "residential"
    assert out["proxy_tier"] == "residential"
    assert out["configured_keys"] == ["api_token", "feed_url", "price_feed_url"]
    assert out["crawler_config"]["feed_url"] != "https://feed.example.com/very-secret-feed-token.csv"
    assert out["crawler_config"]["price_feed_url"] != "https://feed.example.com/private-price-feed.csv"
    assert out["crawler_config"]["api_token"] != "super-secret-token"
    assert s.query(AdminAuditLog).count() == before + 1

    shown = admin_spine.site_crawler_config("vidaxl_us", user="admin", db=s)
    assert shown["configured_keys"] == ["api_token", "feed_url", "price_feed_url"]
    assert shown["crawler_config"]["api_token"] == out["crawler_config"]["api_token"]
    assert shown["crawler_config"]["price_feed_url"] == out["crawler_config"]["price_feed_url"]

    admin_spine.site_crawler_config_update(
        "vidaxl_us",
        {"crawler_config": {
            "api_token": shown["crawler_config"]["api_token"],
            "feed_url": shown["crawler_config"]["feed_url"],
            "price_feed_url": shown["crawler_config"]["price_feed_url"],
        }},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    s.refresh(site)
    assert site.crawler_config["api_token"] == "super-secret-token"
    assert site.crawler_config["feed_url"] == "https://feed.example.com/very-secret-feed-token.csv"
    assert site.crawler_config["price_feed_url"] == "https://feed.example.com/private-price-feed.csv"

    cleared = admin_spine.site_crawler_config_update(
        "vidaxl_us",
        {"api_token": None},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    assert cleared["configured_keys"] == ["feed_url", "price_feed_url"]
    s.close()


def test_site_crawler_config_test_price_source_dry_run(tmp_path):
    init_db()
    from datetime import datetime
    import uuid
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import Product, Site

    s = SessionLocal()
    site_code = f"price_test_{uuid.uuid4().hex[:8]}"
    s.add(Site(site=site_code, brand="PriceTest", country="US",
               url="https://price-test.example.com", platform="bol",
               proxy_tier="none"))
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    s.add(Product(site=site_code, brand="PriceTest", sku=sku,
                  title="Needs price", product_url="https://price-test.example.com/p/1",
                  sale_price=None, original_price=None, currency=None,
                  updated_time=datetime.utcnow()))
    s.commit()
    feed = tmp_path / "prices.csv"
    feed.write_text(
        "product_id,final_price,regular_price,currency,title\n"
        f"{sku},19.99,29.99,USD,Feed title\n",
        encoding="utf-8",
    )

    out = admin_spine.site_crawler_config_test_price_source(
        site_code,
        {
            "sample_limit": 5,
            "crawler_config": {
                "price_source_type": "feed",
                "price_feed_url": str(feed),
                "price_feed_sku_field": "product_id",
                "price_feed_sale_price_field": "final_price",
                "price_feed_original_price_field": "regular_price",
            },
        },
        user="admin",
        db=s,
    )
    assert out["status"] == "ok"
    assert out["stats"]["matched"] == 1
    assert out["stats"]["updated"] == 1
    assert out["samples"][0]["sku"] == sku
    assert out["samples"][0]["after"]["sale_price"] == 19.99
    assert out["samples"][0]["after"]["original_price"] == 29.99
    assert out["samples"][0]["after"]["currency"] == "USD"
    s.close()


def test_admin_analytics_recompute_from_review_snapshots():
    init_db()
    from datetime import date, timedelta

    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import Product, PriceHistory, Trend

    s = SessionLocal()
    site = "songmics_us"
    for sku in ("TEST-ANALYTICS-1", "TEST-ANALYTICS-2"):
        s.query(PriceHistory).filter(PriceHistory.site == site,
                                     PriceHistory.sku == sku).delete()
        s.query(Product).filter(Product.site == site, Product.sku == sku).delete()
    today = date.today()
    s.add(Product(site=site, sku="TEST-ANALYTICS-1",
                  title="Analytics product", product_url="https://example.com/a",
                  sale_price=10.0, review_count=15))
    s.add(Product(site=site, sku="TEST-ANALYTICS-2",
                  title="Analytics product 2", product_url="https://example.com/b",
                  sale_price=20.0, review_count=7))
    s.add_all([
        PriceHistory(site=site, sku="TEST-ANALYTICS-1",
                     date=today - timedelta(days=2), sale_price=10,
                     review_count=10),
        PriceHistory(site=site, sku="TEST-ANALYTICS-1",
                     date=today, sale_price=10, review_count=15),
        PriceHistory(site=site, sku="TEST-ANALYTICS-2",
                     date=today, sale_price=20, review_count=7),
    ])
    s.commit()

    out = admin_spine.admin_analytics_recompute(
        {"sites": [site]}, user="admin", db=s, ip="127.0.0.1")

    one = s.query(Product).filter(Product.site == site,
                                  Product.sku == "TEST-ANALYTICS-1").one()
    two = s.query(Product).filter(Product.site == site,
                                  Product.sku == "TEST-ANALYTICS-2").one()
    assert out["status"] == "recomputed"
    assert out["by_site"][site]["estimated_skus"] == 1
    assert out["by_site"][site]["insufficient_history_skus"] == 1
    assert one.thirty_day_sales == 200
    assert one.thirty_day_revenue == 2000
    assert two.thirty_day_sales == 0
    assert s.query(Trend).filter(Trend.site == site).count() >= 1
    s.close()


def test_admin_third_party_metrics_import_upserts_trends():
    init_db()
    from datetime import date
    import uuid
    import pytest
    from fastapi import HTTPException

    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import (AdminAuditLog, Product, Site, Trend, Workspace,
                            WorkspaceSite)

    site = f"third_party_metric_{uuid.uuid4().hex[:8]}"
    s = SessionLocal()
    s.query(Trend).filter(Trend.site == site).delete()
    s.query(Product).filter(Product.site == site).delete()
    s.query(Site).filter(Site.site == site).delete()
    s.add(Site(site=site, brand="QA", country="US",
               url="https://third-party-metric.example.com",
               platform="generic"))
    s.add(Product(site=site, sku="TP-1",
                  title="Third Party Metric Product",
                  product_url="https://example.com/tp-1",
                  sale_price=10.0, review_count=3))
    workspace = s.query(Workspace).filter(Workspace.status == "active").first()
    assert workspace is not None
    s.add(WorkspaceSite(workspace_id=workspace.id, site=site,
                        display_name=site, enabled=True, hidden=False))
    s.commit()

    template = admin_spine.admin_third_party_metrics_template(
        tenant=workspace.id, date_value="2026-06-17", user="admin", db=s)
    assert template["count"] >= 1
    assert template["summary"]["missing_traffic"] >= 1
    assert template["summary"]["missing_conversion"] >= 1
    assert site in template["csv"]
    template_item = next(item for item in template["items"] if item["site"] == site)
    assert template_item["missing_traffic"] is True
    assert template_item["missing_conversion"] is True

    validation = admin_spine.admin_third_party_metrics_validate(
        {
            "csv": (
                "site,date,traffic,conversion_rate\n"
                f"{site},2026-06-17,12345,2.5\n"
            )
        },
        user="admin",
        db=s,
    )
    assert validation["valid"] is True
    assert validation["valid_rows"] == 1
    assert validation["created"] == 1
    assert validation["updated"] == 0
    assert validation["by_site"][site]["rows"] == 1

    invalid_before = s.query(Trend).filter(Trend.site == site).count()
    invalid = admin_spine.admin_third_party_metrics_validate(
        {
            "csv": (
                "site,date,traffic,conversion_rate\n"
                f"{site},bad-date,,\n"
                "missing_site,2026-06-17,100,2.0\n"
            )
        },
        user="admin",
        db=s,
    )
    assert invalid["valid"] is False
    assert len(invalid["errors"]) == 2
    with pytest.raises(HTTPException) as exc:
        admin_spine.admin_third_party_metrics_import(
            {
                "csv": (
                    "site,date,traffic,conversion_rate\n"
                    f"{site},bad-date,,\n"
                )
            },
            user="admin",
            db=s,
            ip="127.0.0.1",
        )
    assert exc.value.status_code == 422
    assert s.query(Trend).filter(Trend.site == site).count() == invalid_before

    before_audit = s.query(AdminAuditLog).count()
    out = admin_spine.admin_third_party_metrics_import(
        {
            "csv": (
                "site,date,traffic,conversion_rate\n"
                f"{site},2026-06-17,12345,2.5\n"
            )
        },
        user="admin",
        db=s,
        ip="127.0.0.1",
    )

    trend = (s.query(Trend)
             .filter(Trend.site == site, Trend.date == date(2026, 6, 17))
             .one())
    assert out["status"] == "imported"
    assert out["rows"] == 1
    assert out["created"] == 1
    assert out["updated"] == 0
    assert out["by_site"][site]["rows"] == 1
    assert trend.traffic == 12345
    assert trend.conversion_rate == 2.5
    assert trend.sku_count == 1
    assert trend.review_total == 3
    assert s.query(AdminAuditLog).count() == before_audit + 1

    out2 = admin_spine.admin_third_party_metrics_import(
        {
            "rows": [{
                "site": site,
                "date": "2026-06-17",
                "traffic": "23,456",
                "conversion_rate": "3.1%",
            }]
        },
        user="admin",
        db=s,
        ip="127.0.0.1",
    )

    s.refresh(trend)
    assert out2["rows"] == 1
    assert out2["created"] == 0
    assert out2["updated"] == 1
    assert out2["by_site"][site]["updated"] == 1
    assert trend.traffic == 23456
    assert trend.conversion_rate == 3.1
    s.close()


def test_proxy_anti_bot_diagnostics_and_batch_check(monkeypatch):
    init_db()
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    import uuid

    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import (AdminAuditLog, CrawlJob, ProxyRule, Site, Workspace,
                            WorkspaceSite)

    site = f"anti_bot_{uuid.uuid4().hex[:8]}"
    s = SessionLocal()
    workspace = s.query(Workspace).filter(Workspace.status == "active").first()
    assert workspace is not None
    s.add(Site(site=site, brand="AntiBot", country="US",
               url="https://anti-bot.example.com", platform="generic",
               proxy_tier="residential"))
    s.add(WorkspaceSite(workspace_id=workspace.id, site=site,
                        display_name=site, enabled=True, hidden=False))
    failed = CrawlJob(site=site, status="failed",
                      requested_by_workspace_id=workspace.id,
                      failure_code="http_403",
                      failure_stage="fetch",
                      failure_detail="blocked by target",
                      retryable=True,
                      suggested_action="切换住宅代理",
                      created_at=datetime.utcnow() - timedelta(minutes=5),
                      finished_at=datetime.utcnow() - timedelta(minutes=4))
    s.add(failed)
    s.commit()

    diagnostics = admin_spine.proxies_anti_bot_diagnostics(
        tenant=workspace.id, include_hidden=False, user="admin", db=s)
    row = next(item for item in diagnostics["items"] if item["site"] == site)
    assert "anti_bot_blocked" in row["issues"]
    assert row["recommended_rule"]["pool_slug"] == "residential"
    assert diagnostics["summary"]["anti_bot_blocked"] >= 1

    before_apply_audit = s.query(AdminAuditLog).count()
    applied = admin_spine.proxies_anti_bot_apply_rules(
        {"tenant": workspace.id, "sites": [site]},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    assert applied["applied_count"] == 1
    assert applied["applied"][0]["site"] == site
    rule = (s.query(ProxyRule)
            .filter(ProxyRule.site_pattern == site,
                    ProxyRule.match_type == "exact")
            .one())
    assert rule.proxy_mode == "pool"
    assert rule.pool_slug == "residential"
    assert rule.fallback_pool_slug == "datacenter"
    assert rule.enabled is True
    assert s.query(AdminAuditLog).count() == before_apply_audit + 1

    diagnostics_after_apply = admin_spine.proxies_anti_bot_diagnostics(
        tenant=workspace.id, include_hidden=False, user="admin", db=s)
    applied_row = next(
        item for item in diagnostics_after_apply["items"]
        if item["site"] == site
    )
    assert applied_row["current_rule"]["site_pattern"] == site

    def fake_probe_proxy_for_url(*, tier, site, url, timeout):
        return SimpleNamespace(ok=True, status_code=200, failure=None)

    import app.proxy_probe as proxy_probe
    monkeypatch.setattr(proxy_probe, "probe_proxy_for_url",
                        fake_probe_proxy_for_url)
    before_audit = s.query(AdminAuditLog).count()
    checked = admin_spine.proxies_anti_bot_check(
        {"tenant": workspace.id, "sites": [site], "limit": 5, "timeout": 5},
        user="admin",
        db=s,
        ip="127.0.0.1",
    )
    assert checked["checked"] == 1
    assert checked["ok"] == 1
    assert checked["items"][0]["probe"]["ok"] is True
    assert checked["items"][0]["probe"]["site"] == site
    assert s.query(AdminAuditLog).count() == before_audit + 1
    s.close()


def test_jobs_list_detail_retry_enqueue():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app import spine_queue
    from app.models import SpineJob, AdminAuditLog
    s = SessionLocal()
    jid = spine_queue.enqueue(s, "https://x.com/p/adm", "adm-set", workspace_id=None)
    s.commit()
    job = s.get(SpineJob, jid); job.status = "failed"; job.error = "boom"; s.commit()
    lst = admin_spine.jobs_list(status="failed", dataset=None, tenant=None,
                                page=1, size=20, user="admin", db=s)
    assert lst["total"] >= 1 and any(it["id"] == jid for it in lst["items"])
    det = admin_spine.job_detail(job_id=jid, user="admin", db=s)
    assert det["id"] == jid and det["error"] == "boom"
    n_audit = s.query(AdminAuditLog).count()
    r = admin_spine.job_retry(job_id=jid, user="admin", db=s, ip="1.1.1.1")
    assert r["status"] == "pending"
    s.refresh(s.get(SpineJob, jid))
    assert s.get(SpineJob, jid).status == "pending"
    assert s.query(AdminAuditLog).count() == n_audit + 1
    n_audit2 = s.query(AdminAuditLog).count()
    e = admin_spine.job_enqueue(payload={"url": "https://x.com/p/new", "dataset": "adm-set"},
                                user="admin", db=s, ip="1.1.1.1")
    assert e["job_id"] and e["status"] == "pending"
    assert s.query(AdminAuditLog).count() == n_audit2 + 1
    s.close()


def test_jobs_list_merges_failed_product_retry_progress_for_crawl_queue():
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob

    s = SessionLocal()
    s.query(CrawlJob).delete()
    created_at = datetime(2026, 6, 28, 2, 0, 0)
    parent = CrawlJob(
        site="article_us",
        status="partial",
        trigger="scheduled",
        products_count=740,
        total_product_count=741,
        failure_code="superseded",
        created_at=created_at,
        finished_at=created_at + timedelta(hours=1),
    )
    retry = CrawlJob(
        site="article_us",
        status="success",
        trigger="failed_product_retry",
        products_count=1,
        total_product_count=1,
        created_at=created_at + timedelta(hours=6),
        finished_at=created_at + timedelta(hours=6, minutes=1),
    )
    s.add_all([parent, retry])
    s.commit()

    out = admin_spine.jobs_list(
        status=None,
        dataset=None,
        tenant=None,
        source="crawl",
        page=1,
        size=20,
        failure_code=None,
        created_from="2026-06-28T00:00:00+00:00",
        created_to="2026-06-28T23:59:59+00:00",
        user="admin",
        db=s,
    )

    assert out["total"] == 1
    row = out["items"][0]
    assert row["id"] == retry.id
    assert row["products_count"] == 741
    assert row["total_product_count"] == 741
    assert row["total_product_count_source"] == "crawl_retry_merged"
    s.close()


def test_jobs_stats_aggregates_all_queue_tables():
    init_db()
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, OnDemandJob, SpineJob
    s = SessionLocal()
    s.query(SpineJob).delete()
    s.query(CrawlJob).delete()
    s.query(OnDemandJob).delete()
    s.commit()
    s.add(CrawlJob(site="site_a", status="success", created_at=datetime.utcnow()))
    s.add(CrawlJob(site="site_b", status="blocked", created_at=datetime.utcnow(),
                   failure_code="anti_bot_challenge",
                   failure_stage="fetch",
                   failure_detail="blocked by challenge",
                   retryable=True,
                   suggested_action="use residential proxy"))
    s.add(CrawlJob(site="site_c", status="running",
                   created_at=datetime.utcnow(),
                   started_at=datetime.utcnow() - timedelta(hours=2)))
    s.add(CrawlJob(site="site_e", status="running",
                   created_at=datetime.utcnow(),
                   started_at=datetime.utcnow() - timedelta(hours=2),
                   heartbeat_at=datetime.utcnow()))
    s.add(CrawlJob(site="site_d", status="pending",
                   created_at=datetime.utcnow() - timedelta(hours=3)))
    s.add(OnDemandJob(url="https://x.test/1", platform="shop",
                      status="queued", created_at=datetime.utcnow()))
    s.add(OnDemandJob(url="https://x.test/2", platform="shop",
                      status="partial", created_at=datetime.utcnow()))
    s.commit()

    stats = admin_spine.jobs_stats(user="admin", db=s)
    listed = admin_spine.jobs_list(status=None, dataset=None, tenant=None,
                                   source="all", page=1, size=20,
                                   user="admin", db=s)
    running = admin_spine.jobs_list(status="running", dataset=None, tenant=None,
                                    source="all", page=1, size=20,
                                    user="admin", db=s)
    stuck = admin_spine.jobs_list(status="stuck", dataset=None, tenant=None,
                                  source="all", page=1, size=20,
                                  user="admin", db=s)
    stale_pending = admin_spine.jobs_list(status="stale_pending", dataset=None,
                                          tenant=None, source="all", page=1,
                                          size=20, user="admin", db=s)
    failed = admin_spine.jobs_list(status="failed", dataset=None, tenant=None,
                                   source="all", page=1, size=20,
                                   user="admin", db=s)

    assert stats["success"] == 1
    assert stats["blocked"] == 1
    assert stats["stuck"] == 1
    assert stats["running"] == 1
    assert stats["pending"] == 2
    assert stats["stale_pending"] == 1
    assert stats["partial"] == 1
    assert stats["status_meta"]["running_raw"] == 2
    assert stats["status_meta"]["running_active"] == 1
    assert stats["status_meta"]["stuck"] == 1
    assert stats["status_meta"]["stale_pending"] == 1
    assert "worker 心跳" in stats["status_count_note"]
    assert "久排阈值" in stats["status_count_note"]
    assert stats["by_queue"]["crawl"]["total"] == 5
    assert stats["by_queue"]["crawl"]["stale_pending"] == 1
    assert stats["by_queue"]["crawl"]["status_meta"]["running_raw"] == 2
    assert stats["by_queue"]["crawl"]["status_meta"]["running_active"] == 1
    assert stats["by_queue"]["ondemand"]["total"] == 2
    assert stats["breakdowns"]["crawl_blocked_by_site"] == [{"key": "site_b", "count": 1}]
    assert stats["breakdowns"]["crawl_running_by_site"] == [{"key": "site_e", "count": 1}]
    assert stats["breakdowns"]["crawl_stale_pending_by_site"] == [{"key": "site_d", "count": 1}]
    assert stats["breakdowns"]["crawl_failure_codes"] == [{"key": "anti_bot_challenge", "count": 1}]
    assert {row["source"] for row in listed["items"]} == {"crawl", "ondemand"}
    assert running["total"] == 1
    assert running["items"][0]["site"] == "site_e"
    assert running["items"][0]["heartbeat_at"] is not None
    assert stuck["total"] == 1
    assert stale_pending["total"] == 1
    assert stale_pending["items"][0]["site"] == "site_d"
    assert stale_pending["items"][0]["is_stale_pending"] is True
    assert failed["total"] == 0
    blocked = admin_spine.jobs_list(status="blocked", dataset="site_b", tenant=None,
                                    source="crawl", page=1, size=20,
                                    failure_code="anti_bot_challenge",
                                    user="admin", db=s)
    assert blocked["total"] == 1
    assert blocked["items"][0]["failure_stage"] == "fetch"
    assert blocked["items"][0]["retryable"] is True
    assert blocked["items"][0]["suggested_action"] == "use residential proxy"
    assert blocked["items"][0]["attempts"] is None
    stuck_crawl_id = s.query(CrawlJob).filter(CrawlJob.site == "site_c").one().id
    with patch("app.runner.enqueue", return_value=321):
        retried = admin_spine.job_retry(job_id=stuck_crawl_id, source="crawl",
                                        user="admin", db=s, ip="127.0.0.1")
    assert retried == {
        "job_id": 321,
        "source": "crawl",
        "status": "pending",
        "retried_from": stuck_crawl_id,
    }
    assert blocked["items"][0]["retries"] is None
    s.close()


def test_jobs_list_paginates_across_sources_by_created_at():
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, OnDemandJob, SpineJob

    s = SessionLocal()
    try:
        s.query(SpineJob).delete()
        s.query(CrawlJob).delete()
        s.query(OnDemandJob).delete()
        s.commit()
        now = datetime.utcnow()
        s.add(CrawlJob(site="crawl-new", status="success",
                       created_at=now - timedelta(minutes=1)))
        s.add(SpineJob(url="https://x.test/new", dataset="spine-new",
                       status="success", created_at=now - timedelta(minutes=2)))
        s.add(OnDemandJob(url="https://x.test/mid", platform="lazada",
                          status="success", created_at=now - timedelta(minutes=3)))
        s.add(CrawlJob(site="crawl-old", status="failed",
                       created_at=now - timedelta(minutes=4)))
        s.add(SpineJob(url="https://x.test/old", dataset="spine-old",
                       status="failed", created_at=now - timedelta(minutes=5)))
        s.commit()

        first = admin_spine.jobs_list(status=None, dataset=None, tenant=None,
                                      source="all", page=1, size=2,
                                      user="admin", db=s)
        second = admin_spine.jobs_list(status=None, dataset=None, tenant=None,
                                       source="all", page=2, size=2,
                                       user="admin", db=s)
        third = admin_spine.jobs_list(status=None, dataset=None, tenant=None,
                                      source="all", page=3, size=2,
                                      user="admin", db=s)

        assert first["total"] == 5
        assert [row["target"] for row in first["items"]] == ["crawl-new", "spine-new"]
        assert [row["target"] for row in second["items"]] == ["lazada", "crawl-old"]
        assert [row["target"] for row in third["items"]] == ["spine-old"]
    finally:
        s.close()


def test_jobs_maintenance_dry_run_and_apply(monkeypatch):
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import (AdminAuditLog, CrawlFailure, CrawlJob, OnDemandJob,
                            SpineJob)

    enqueued = []
    monkeypatch.setattr("app.ondemand.queue.enqueue",
                        lambda job_id: enqueued.append(job_id))
    s = SessionLocal()
    try:
        s.query(SpineJob).delete()
        s.query(CrawlJob).delete()
        s.query(OnDemandJob).delete()
        s.commit()
        old = datetime.utcnow() - timedelta(hours=2)
        spine = SpineJob(url="https://example.com/stuck", dataset="maint",
                         entity_type="generic", status="running",
                         created_at=old, started_at=old,
                         heartbeat_at=old, worker="spine-worker",
                         retries=0, max_retries=3)
        crawl = CrawlJob(site="maint_site", status="running",
                         created_at=old, started_at=old,
                         heartbeat_at=old, worker="crawl-worker")
        stale_pending = CrawlJob(site="maint_pending", status="pending",
                                 created_at=datetime.utcnow() - timedelta(hours=3))
        ondemand = OnDemandJob(url="https://shop.example/p/1",
                               platform="shopee", kind="listing",
                               status="running", created_at=old)
        s.add_all([spine, crawl, stale_pending, ondemand])
        s.commit()
        ids = {
            "spine": spine.id,
            "crawl": crawl.id,
            "pending": stale_pending.id,
            "ondemand": ondemand.id,
        }

        dry = admin_spine.jobs_maintenance(
            {"apply": False}, user="admin", db=s, ip="127.0.0.1")
        assert dry["dry_run"] is True
        assert dry["total_actionable"] == 4
        assert dry["counts"]["crawl_stale_pending_observed"] == 1
        assert dry["counts"]["crawl_failed_stale_pending"] == 1
        assert s.get(SpineJob, ids["spine"]).status == "running"
        assert s.get(CrawlJob, ids["crawl"]).status == "running"
        assert s.get(OnDemandJob, ids["ondemand"]).status == "running"

        before_audit = s.query(AdminAuditLog).count()
        applied = admin_spine.jobs_maintenance(
            {"apply": True}, user="admin", db=s, ip="127.0.0.1")
        assert applied["applied"] is True
        assert applied["counts"]["spine_requeued"] == 1
        assert applied["counts"]["crawl_failed_timeout"] == 1
        assert applied["counts"]["crawl_failed_stale_pending"] == 1
        assert applied["counts"]["ondemand_requeued"] == 1
        assert enqueued == [ids["ondemand"]]
        assert s.query(AdminAuditLog).count() == before_audit + 1

        spine_row = s.get(SpineJob, ids["spine"])
        crawl_row = s.get(CrawlJob, ids["crawl"])
        pending_row = s.get(CrawlJob, ids["pending"])
        ondemand_row = s.get(OnDemandJob, ids["ondemand"])
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.job_id == ids["crawl"])
                   .order_by(CrawlFailure.id.desc())
                   .first())
        assert spine_row.status == "pending"
        assert spine_row.worker is None
        assert crawl_row.status == "failed"
        assert crawl_row.failure_code == "job_timeout"
        assert failure is not None
        assert failure.code == "job_timeout"
        assert pending_row.status == "failed"
        assert pending_row.failure_code == "queue_stalled"
        assert ondemand_row.status == "queued"
    finally:
        s.close()


def test_jobs_list_exposes_queue_detail_fields_and_fuzzy_filters():
    init_db()
    import uuid
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, OnDemandJob, SpineJob

    suffix = uuid.uuid4().hex[:10]
    site = f"queue_fuzzy_{suffix}"
    dataset = f"queue_dataset_{suffix}"
    stuck_dataset = f"queue_stuck_{suffix}"
    platform = f"queue_platform_{suffix}"
    now = datetime.utcnow()
    s = SessionLocal()
    s.add(CrawlJob(
        site=site,
        status="failed",
        trigger="admin_quality_rerun",
        worker="worker-a",
        created_at=now - timedelta(minutes=7),
        started_at=now - timedelta(minutes=6),
        finished_at=now - timedelta(minutes=1),
        duration_sec=300,
        error="parse returned no products",
        failure_code="parse_none",
        failure_stage="parse",
        failure_detail="no product cards matched",
        retryable=True,
        suggested_action="检查解析规则",
    ))
    s.add(SpineJob(
        url=f"https://example.com/{suffix}/item",
        dataset=dataset,
        status="running",
        worker="spine-worker",
        created_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=9),
        heartbeat_at=now - timedelta(seconds=30),
    ))
    s.add(SpineJob(
        url=f"https://example.com/{suffix}/stuck",
        dataset=stuck_dataset,
        status="running",
        worker="spine-worker",
        created_at=now - timedelta(hours=3),
        started_at=now - timedelta(hours=3),
        heartbeat_at=now - timedelta(hours=3),
    ))
    s.add(OnDemandJob(
        url=f"https://shop.example.com/{suffix}",
        platform=platform,
        kind="listing",
        status="failed",
        batch_id=f"batch_{suffix}",
        attempts=2,
        error="platform boom",
        created_at=now - timedelta(minutes=8),
        finished_at=now - timedelta(minutes=2),
    ))
    s.commit()

    stats = admin_spine.jobs_stats(user="admin", db=s)
    assert any(row["key"] == dataset for row in stats["breakdowns"]["spine_running_by_dataset"])
    assert any(row["key"] == stuck_dataset for row in stats["breakdowns"]["spine_stuck_by_dataset"])
    assert any(row["key"] == platform for row in stats["breakdowns"]["ondemand_failed_by_platform"])

    crawl = admin_spine.jobs_list(
        status="failed",
        dataset=suffix,
        tenant=None,
        source="crawl",
        page=1,
        size=20,
        failure_code="parse_none",
        user="admin",
        db=s,
    )
    assert crawl["total"] == 1
    crawl_row = crawl["items"][0]
    assert crawl_row["site"] == site
    assert crawl_row["finished_at"] is not None
    assert crawl_row["duration_sec"] == 300
    assert crawl_row["active_sec"] >= 300
    assert crawl_row["retryable"] is True
    assert crawl_row["suggested_action"] == "检查解析规则"

    running = admin_spine.jobs_list(
        status="running",
        dataset=suffix,
        tenant=None,
        source="spine",
        page=1,
        size=20,
        user="admin",
        db=s,
    )
    assert running["total"] == 1
    assert running["items"][0]["dataset"] == dataset
    assert running["items"][0]["active_sec"] > 0
    assert running["items"][0]["stuck_reason"] is None

    stuck = admin_spine.jobs_list(
        status="stuck",
        dataset="stuck",
        tenant=None,
        source="spine",
        page=1,
        size=20,
        user="admin",
        db=s,
    )
    assert any(row["dataset"] == stuck_dataset
               and row["stuck_reason"] == "heartbeat_missing_or_expired"
               for row in stuck["items"])

    ondemand = admin_spine.jobs_list(
        status="failed",
        dataset=platform,
        tenant=None,
        source="ondemand",
        page=1,
        size=20,
        user="admin",
        db=s,
    )
    assert ondemand["total"] == 1
    assert ondemand["items"][0]["retryable"] is True
    assert ondemand["items"][0]["duration_sec"] > 0

    s.query(CrawlJob).filter(CrawlJob.site == site).delete()
    s.query(SpineJob).filter(SpineJob.dataset.in_([dataset, stuck_dataset])).delete(
        synchronize_session=False
    )
    s.query(OnDemandJob).filter(OnDemandJob.platform == platform).delete()
    s.commit()
    s.close()


def test_job_detail_supports_all_queue_sources():
    init_db()
    import pytest
    from datetime import datetime
    from fastapi import HTTPException
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, OnDemandJob, SpineJob

    s = SessionLocal()
    spine = SpineJob(url="https://x.test/1", dataset="detail_ds",
                     status="failed", error="spine boom",
                     created_at=datetime.utcnow())
    crawl = CrawlJob(site="detail_site", status="failed",
                     failure_code="zero_products",
                     failure_stage="parse",
                     failure_detail="crawl boom",
                     created_at=datetime.utcnow())
    ondemand = OnDemandJob(url="https://x.test/2", platform="shop",
                           status="failed", error="ondemand boom",
                           created_at=datetime.utcnow())
    s.add_all([spine, crawl, ondemand])
    s.commit()

    assert admin_spine.job_detail(spine.id, source="spine", user="admin", db=s)["source"] == "spine"
    crawl_detail = admin_spine.job_detail(crawl.id, source="crawl", user="admin", db=s)
    assert crawl_detail["source"] == "crawl"
    assert crawl_detail["failure_code"] == "zero_products"
    assert crawl_detail["error"] == "crawl boom"
    ondemand_detail = admin_spine.job_detail(ondemand.id, source="ondemand",
                                             user="admin", db=s)
    assert ondemand_detail["source"] == "ondemand"
    assert ondemand_detail["error"] == "ondemand boom"
    listed = admin_spine.jobs_list(status="failed", dataset=None, tenant=None,
                                   source="all", page=1, size=20,
                                   user="admin", db=s)
    source_ids = {(row["source"], row["id"]) for row in listed["items"]}
    assert ("spine", spine.id) in source_ids
    assert ("crawl", crawl.id) in source_ids
    assert ("ondemand", ondemand.id) in source_ids

    with pytest.raises(HTTPException) as exc:
        admin_spine.job_detail(spine.id, source="missing", user="admin", db=s)
    assert exc.value.status_code == 422
    s.close()


def test_crawl_claim_prioritizes_admin_rerun_over_scheduled_backlog():
    init_db()
    from datetime import datetime
    from app.db import SessionLocal
    from app.models import CrawlJob
    from app.runner import claim_job

    s = SessionLocal()
    s.query(CrawlJob).delete()
    s.commit()
    scheduled = CrawlJob(site="priority_scheduled", status="pending",
                         trigger="scheduled", created_at=datetime(2026, 6, 1))
    older_admin = CrawlJob(site="priority_admin_old", status="pending",
                           trigger="admin_quality_rerun",
                           created_at=datetime(2026, 6, 1, 12))
    admin = CrawlJob(site="priority_admin", status="pending",
                     trigger="admin_quality_rerun",
                     created_at=datetime(2026, 6, 2))
    s.add_all([scheduled, older_admin, admin])
    s.commit()
    admin_id = admin.id
    s.close()

    assert claim_job("priority-worker") == admin_id

    s = SessionLocal()
    s.query(CrawlJob).filter(CrawlJob.site.in_(
        ("priority_scheduled", "priority_admin_old", "priority_admin")
    )).delete()
    s.commit()
    s.close()


def test_admin_crawl_enqueue_creates_or_reuses_site_jobs():
    init_db()
    import pytest
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from fastapi import HTTPException
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import AdminAuditLog, CrawlJob, Site

    s = SessionLocal()
    for code in ("admin_quality_new", "admin_quality_active", "admin_quality_pending"):
        s.query(CrawlJob).filter(CrawlJob.site == code).delete()
        s.query(Site).filter(Site.site == code).delete()
    s.add(Site(site="admin_quality_new", brand="QA", country="US",
               url="https://qa-new.example.com", platform="generic"))
    s.add(Site(site="admin_quality_active", brand="QA", country="US",
               url="https://qa-active.example.com", platform="generic"))
    s.add(Site(site="admin_quality_pending", brand="QA", country="US",
               url="https://qa-pending.example.com", platform="generic"))
    s.add(CrawlJob(site="admin_quality_active", status="running",
                   created_at=datetime.utcnow()))
    s.add(CrawlJob(site="admin_quality_pending", status="pending",
                   trigger="scheduled",
                   created_at=datetime.utcnow() - timedelta(hours=3)))
    s.commit()
    active = s.query(CrawlJob).filter(CrawlJob.site == "admin_quality_active").first()
    pending = s.query(CrawlJob).filter(CrawlJob.site == "admin_quality_pending").first()
    n_audit = s.query(AdminAuditLog).count()

    with patch("app.runner.enqueue", return_value=991):
        out = admin_spine.admin_crawl_enqueue(
            {"sites": ["admin_quality_new", "admin_quality_active", "admin_quality_pending"]},
            user="admin", db=s, ip="1.1.1.1",
        )

    assert out["status"] == "mixed"
    assert out["by_site"]["admin_quality_new"] == {"job_id": 991, "status": "queued"}
    assert out["by_site"]["admin_quality_active"] == {
        "job_id": active.id, "status": "already_running"}
    assert out["by_site"]["admin_quality_pending"] == {
        "job_id": pending.id, "status": "promoted"}
    s.refresh(pending)
    assert pending.trigger == "admin_quality_rerun"
    assert pending.requested_by_user_id is not None
    assert out["promoted_jobs"] == [pending.id]
    assert s.query(AdminAuditLog).count() == n_audit + 1

    with pytest.raises(HTTPException) as exc:
        admin_spine.admin_crawl_enqueue({"site": "missing_quality_site"},
                                        user="admin", db=s, ip="")
    assert exc.value.status_code == 404
    s.close()


def test_admin_crawl_enqueue_skips_paused_pending_site():
    init_db()
    from datetime import datetime, timedelta
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, Site

    s = SessionLocal()
    for code in ("admin_quality_paused",):
        s.query(CrawlJob).filter(CrawlJob.site == code).delete()
        s.query(Site).filter(Site.site == code).delete()
    s.add(Site(site="admin_quality_paused", brand="QA", country="US",
               url="https://qa-paused.example.com", platform="generic",
               track_status="paused"))
    s.flush()
    pending = CrawlJob(site="admin_quality_paused", status="pending",
                       trigger="scheduled",
                       created_at=datetime.utcnow() - timedelta(hours=3))
    s.add(pending)
    s.commit()
    pending_id = pending.id

    out = admin_spine.admin_crawl_enqueue(
        {"site": "admin_quality_paused"},
        user="admin", db=s, ip="1.1.1.1",
    )

    s.refresh(pending)
    assert out["status"] == "skipped_precondition"
    assert out["by_site"]["admin_quality_paused"]["job_id"] == pending_id
    assert out["by_site"]["admin_quality_paused"]["failure_code"] == "tracking_paused"
    assert out["promoted_jobs"] == []
    assert pending.status == "skipped"
    assert pending.failure_code == "tracking_paused"
    s.close()


def test_promotions_rebuild_uses_existing_product_signals():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import AdminAuditLog, Product, Promotion, Site

    site = "admin_promo_rebuild"
    s = SessionLocal()
    s.query(Promotion).filter(Promotion.site == site).delete()
    s.query(Product).filter(Product.site == site).delete()
    s.query(Site).filter(Site.site == site).delete()
    s.add(Site(site=site, brand="QA", country="US",
               url="https://promo-rebuild.example.com", platform="generic"))
    s.add(Product(site=site, brand="QA", sku="PROMO-1",
                  title="Adjustable Desk", sale_price=120.0,
                  attributes={
                      "coupon": "Save 20% with code",
                      "promo_type": "coupon",
                      "minimum_order": "orders over $100",
                      "valid_from": "2026-06-01",
                      "valid_until": "2026-06-30",
                  }))
    s.commit()
    n_audit = s.query(AdminAuditLog).count()

    out = admin_spine.admin_promotions_rebuild({"site": site},
                                               user="admin", db=s, ip="1.1.1.1")

    assert out["status"] == "rebuilt"
    assert out["by_site"][site]["after"] == 1
    assert out["by_site"][site]["created"] == 1
    promo = s.query(Promotion).filter(Promotion.site == site).one()
    assert promo.sku == "PROMO-1"
    assert promo.discount_percent == 20
    assert promo.promotion_type == "coupon"
    assert promo.threshold == "orders over $100"
    assert promo.start_time.isoformat() == "2026-06-01T00:00:00"
    assert promo.end_time.isoformat() == "2026-06-30T00:00:00"
    assert s.query(AdminAuditLog).count() == n_audit + 1
    row = s.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
    assert row.action == "promotions.rebuild"
    assert row.target_id == site
    s.close()


def test_admin_data_quality_exposes_never_crawled_sites():
    init_db()
    import uuid
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import CrawlJob, Product, Site, Workspace, WorkspaceSite

    site = f"admin_quality_never_{uuid.uuid4().hex[:8]}"
    s = SessionLocal()
    workspace = s.query(Workspace).filter(Workspace.status == "active").first()
    assert workspace is not None
    s.query(CrawlJob).filter(CrawlJob.site == site).delete()
    s.query(Product).filter(Product.site == site).delete()
    s.query(WorkspaceSite).filter(WorkspaceSite.site == site).delete()
    s.query(Site).filter(Site.site == site).delete()
    s.add(Site(site=site, brand="QA", country="US",
               url="https://never-crawled.example.com", platform="generic"))
    s.add(WorkspaceSite(workspace_id=workspace.id, site=site,
                        enabled=True, hidden=False))
    s.commit()

    out = admin_spine.admin_data_quality(
        tenant=workspace.id, include_hidden=False, user="admin", db=s)
    row = next(item for item in out["items"] if item["site"] == site)

    assert row["sku_count"] == 0
    assert row["crawl_queue"]["total"] == 0
    assert "no_products" in row["issues"]
    assert "never_crawled" in row["issues"]
    assert out["summary"]["no_products"] >= 1
    assert out["summary"]["never_crawled"] >= 1
    assert out["summary"]["sites_without_jobs"] >= 1
    s.close()


def test_data_quality_products_exposes_trend_signal_details():
    init_db()
    from datetime import date
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import Product, Site, Trend

    site = "admin_quality_trend_detail"
    s = SessionLocal()
    s.query(Trend).filter(Trend.site == site).delete()
    s.query(Product).filter(Product.site == site).delete()
    s.query(Site).filter(Site.site == site).delete()
    s.add(Site(site=site, brand="QA", country="US",
               url="https://trend-detail.example.com", platform="generic"))
    s.add(Product(site=site, brand="QA", sku="TD-1",
                  title="Trend Detail Product", sale_price=10.0,
                  currency="USD"))
    s.commit()

    missing = admin_spine.admin_data_quality_products(
        site=site, issue="traffic_missing", page=1, limit=50,
        user="admin", db=s,
    )

    assert missing["kind"] == "trend"
    assert missing["total"] == 1
    assert missing["items"][0]["id"] == f"trend-missing-{site}"
    assert "traffic_missing" in missing["items"][0]["issues"]
    assert missing["issue_counts"]["traffic_missing"] == 1
    assert missing["issue_counts"]["conversion_missing"] == 1

    s.add(Trend(site=site, date=date(2026, 6, 16), sku_count=1,
                new_product_count=0, estimated_sales=5,
                estimated_revenue=50.0, traffic=None,
                conversion_rate=0.12))
    s.commit()

    trend_rows = admin_spine.admin_data_quality_products(
        site=site, issue="traffic_missing", page=1, limit=50,
        user="admin", db=s,
    )

    assert trend_rows["kind"] == "trend"
    assert trend_rows["total"] == 1
    assert trend_rows["items"][0]["date"] == "2026-06-16"
    assert trend_rows["items"][0]["estimated_revenue"] == 50.0
    assert trend_rows["items"][0]["conversion_rate"] == 0.12
    assert trend_rows["issue_counts"]["traffic_missing"] == 1
    assert trend_rows["issue_counts"]["conversion_missing"] == 0
    s.close()


def test_datasets_records_promote_delete():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app import spine
    from app.models import ExtractedRecord, AdminAuditLog
    import uuid
    s = SessionLocal()
    ds = spine.get_or_create_dataset(s, "adm-ds", workspace_id=None, entity_type="product")
    url = f"https://x.com/r1/{uuid.uuid4().hex}"
    rec = ExtractedRecord(dataset_id=ds.id, source_url=url,
                          canonical_url=url, entity_type="product",
                          data={"title": "X"}, record_key=url,
                          quality_status="staging")
    s.add(rec); s.commit(); rid = rec.id
    dsets = admin_spine.datasets_list(user="admin", db=s)
    assert any(d["id"] == ds.id and d["record_count"] >= 1 for d in dsets["items"])
    recs = admin_spine.dataset_records(dataset_id=ds.id, quality_status="staging",
                                       page=1, size=20, user="admin", db=s)
    assert recs["total"] >= 1
    det = admin_spine.record_detail(record_id=rid, user="admin", db=s)
    assert det["data"]["title"] == "X" and "provenance" in det
    na = s.query(AdminAuditLog).count()
    admin_spine.record_promote(record_id=rid, user="admin", db=s, ip="1.1.1.1")
    s.refresh(s.get(ExtractedRecord, rid))
    assert s.get(ExtractedRecord, rid).quality_status == "main"
    assert s.query(AdminAuditLog).count() == na + 1
    na2 = s.query(AdminAuditLog).count()
    admin_spine.record_delete(record_id=rid, user="admin", db=s, ip="1.1.1.1")
    assert s.get(ExtractedRecord, rid) is None
    assert s.query(AdminAuditLog).count() == na2 + 1
    s.close()


def test_usage_endpoints():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import Usage
    s = SessionLocal()
    s.add(Usage(api_key_id=5, workspace_id=1, endpoint="/spine/worker/execute",
                record_count=1, credits_used=2))
    s.add(Usage(api_key_id=5, workspace_id=1, endpoint="/api/v2/scrape",
                record_count=1, credits_used=3))
    s.commit()
    agg = admin_spine.usage_summary(start=None, end=None, endpoint=None,
                                    user="admin", db=s)
    assert agg["total_credits"] >= 5
    bykey = admin_spine.usage_by_key(user="admin", db=s)
    assert any(r["api_key_id"] == 5 and r["credits"] >= 5 for r in bykey["items"])
    bytenant = admin_spine.usage_by_tenant(user="admin", db=s)
    assert any(r["workspace_id"] == 1 for r in bytenant["items"])
    only = admin_spine.usage_summary(start=None, end=None,
                                     endpoint="/spine/worker/execute",
                                     user="admin", db=s)
    assert only["total_credits"] >= 2
    key_only = admin_spine.usage_by_key(start=None, end=None,
                                        endpoint="/spine/worker/execute",
                                        user="admin", db=s)
    assert any(r["api_key_id"] == 5 and r["credits"] >= 2
               and r["records"] >= 1 for r in key_only["items"])
    tenant_only = admin_spine.usage_by_tenant(start=None, end=None,
                                              endpoint="/spine/worker/execute",
                                              user="admin", db=s)
    assert any(r["workspace_id"] == 1 and r["credits"] >= 2
               and r["records"] >= 1 for r in tenant_only["items"])
    s.close()


def test_health_config_audit():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    s = SessionLocal()
    h = admin_spine.health(user="admin", db=s)
    assert "worker_status" in h and "reclaim_hint" in h
    c = admin_spine.config(user="admin", db=s)
    assert "heartbeat_interval" in c and "backoff" in c
    a = admin_spine.audit_list(actor=None, action=None, start=None, end=None,
                               page=1, size=20, user="admin", db=s)
    assert "items" in a and "total" in a
    s.close()


def test_health_status_does_not_call_recent_success_running():
    init_db()
    from datetime import datetime
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import SpineJob
    s = SessionLocal()
    s.query(SpineJob).delete()
    s.commit()
    s.add(SpineJob(url="https://x.com/health/success", dataset="health",
                   status="success", finished_at=datetime.utcnow()))
    s.commit()
    h = admin_spine.health(user="admin", db=s)
    assert h["worker_status"] != "running"
    assert h["worker_status"] in ("idle", "pending", "stuck")
    s.close()


def test_admin_proxy_status_and_clear_audited():
    init_db()
    from datetime import datetime, timedelta
    import uuid
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import AdminAuditLog, ProxyHealth
    from app.proxy_health import proxy_hash, redact_proxy
    s = SessionLocal()
    proxy_url = f"http://user:pass@example-{uuid.uuid4().hex}.proxy:3128"
    h = proxy_hash(proxy_url)
    row = ProxyHealth(
        proxy_hash=h,
        proxy_redacted=redact_proxy(proxy_url),
        tier="residential",
        status="down",
        failure_count=3,
        consecutive_failures=3,
        last_failure_code="network_timeout",
        blocked_until=datetime.utcnow() + timedelta(minutes=5),
        updated_at=datetime.utcnow(),
    )
    s.add(row)
    s.commit()
    status = admin_spine.proxies_status(user="admin", db=s)
    assert status["health"]["total"] >= 1
    assert any(item["hash"] == h and item["persistently_blocking"] for item in status["items"])
    n_audit = s.query(AdminAuditLog).count()
    cleared = admin_spine.proxy_clear(proxy_hash_value=h, user="admin", db=s, ip="1.1.1.1")
    s.refresh(row)
    assert cleared["cleared"] is True
    assert row.status == "unknown"
    assert row.consecutive_failures == 0
    assert row.blocked_until is None
    assert s.query(AdminAuditLog).count() == n_audit + 1
    s.close()


def test_proxy_rule_create_upserts_existing_pattern():
    init_db()
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import ProxyRule

    s = SessionLocal()
    s.query(ProxyRule).filter(ProxyRule.site_pattern == "vidaxl_ca").delete()
    s.commit()
    first = admin_spine.proxy_rule_create({
        "site_pattern": "vidaxl_ca",
        "match_type": "exact",
        "proxy_mode": "pool",
        "pool_slug": "residential",
        "priority": 10,
    }, user="admin", db=s, ip="")
    second = admin_spine.proxy_rule_create({
        "site_pattern": "vidaxl_ca",
        "match_type": "exact",
        "proxy_mode": "pool",
        "pool_slug": "residential",
        "priority": 5,
        "notes": "prefer residential",
    }, user="admin", db=s, ip="")

    rows = s.query(ProxyRule).filter(ProxyRule.site_pattern == "vidaxl_ca").all()
    assert len(rows) == 1
    assert first["rule_id"] == second["rule_id"] == rows[0].id
    assert rows[0].priority == 5
    assert rows[0].notes == "prefer residential"
    s.close()


def test_proxy_rule_status_exposes_primary_and_fallback_availability():
    init_db()
    import uuid
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import ProxyEndpoint, ProxyPoolConfig, ProxyPoolMember, ProxyRule
    from app.proxy_config import upsert_proxy_endpoint
    from app.proxy_pool import reload_pool

    suffix = uuid.uuid4().hex[:10]
    primary_slug = f"primary_{suffix}"
    fallback_slug = f"fallback_{suffix}"
    site = f"proxy_site_{suffix}"
    s = SessionLocal()
    s.query(ProxyRule).filter(ProxyRule.site_pattern == site).delete()
    s.commit()

    primary = ProxyPoolConfig(slug=primary_slug, name=primary_slug,
                              pool_type="datacenter", active=True,
                              fallback_pool_slug=fallback_slug)
    fallback = ProxyPoolConfig(slug=fallback_slug, name=fallback_slug,
                               pool_type="residential", active=True)
    s.add_all([primary, fallback])
    s.flush()
    endpoint = upsert_proxy_endpoint(
        s,
        proxy_url=f"http://user:pass@{suffix}.proxy.test:3128",
        endpoint_type="residential",
        name=f"fallback-{suffix}",
        source="test",
    )
    s.flush()
    s.add(ProxyPoolMember(pool_id=fallback.id, endpoint_id=endpoint.id,
                          active=True, priority=1, weight=1))
    s.commit()
    reload_pool()

    created = admin_spine.proxy_rule_create({
        "site_pattern": site,
        "match_type": "exact",
        "proxy_mode": "pool",
        "pool_slug": primary_slug,
        "priority": 1,
    }, user="admin", db=s, ip="")

    rule = next(r for r in created["rules"] if r["site_pattern"] == site)
    assert rule["primary_pool_slug"] == primary_slug
    assert rule["fallback_pool_slug"] == fallback_slug
    assert rule["primary_member_count"] == 0
    assert rule["primary_available_count"] == 0
    assert rule["fallback_member_count"] == 1
    assert rule["fallback_available_count"] == 1
    assert rule["effective_status"] == "fallback_available"

    s.query(ProxyRule).filter(ProxyRule.site_pattern == site).delete()
    s.query(ProxyPoolMember).filter(ProxyPoolMember.pool_id.in_([primary.id, fallback.id])).delete(
        synchronize_session=False
    )
    s.query(ProxyPoolMember).filter(ProxyPoolMember.endpoint_id == endpoint.id).delete()
    s.query(ProxyEndpoint).filter(ProxyEndpoint.id == endpoint.id).delete()
    s.query(ProxyPoolConfig).filter(ProxyPoolConfig.slug.in_([primary_slug, fallback_slug])).delete(
        synchronize_session=False
    )
    s.commit()
    reload_pool()
    s.close()


def test_non_super_admin_blocked():
    import uuid
    import pytest
    from fastapi import HTTPException
    from app.api import admin_spine
    from app.db import SessionLocal
    from app.models import User, Workspace
    from app.auth import hash_password
    init_db(); s = SessionLocal()
    sfx = uuid.uuid4().hex[:8]
    uname = f"plainuser403_{sfx}"
    ws = Workspace(name=f"t-ws-403-{sfx}", slug=f"t-ws-403-{sfx}"); s.add(ws); s.flush()
    u = User(username=uname, email=f"p403_{sfx}@e.com",
             password_hash=hash_password("Password1"), role="user",
             global_role=None, status="active", default_workspace_id=ws.id)
    s.add(u); s.commit()
    for call in (lambda: admin_spine.jobs_stats(user=uname, db=s),
                 lambda: admin_spine.datasets_list(user=uname, db=s),
                 lambda: admin_spine.usage_by_key(user=uname, db=s)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
    s.close()


def test_existing_admin_write_audited():
    init_db()
    from app.api import routes
    from app.db import SessionLocal
    from app.models import AdminAuditLog
    import uuid
    s = SessionLocal()
    na = s.query(AdminAuditLog).count()
    sfx = uuid.uuid4().hex[:8]
    wsname = "audited-ws-" + sfx
    routes.admin_create_workspace(
        payload={"name": wsname, "slug": wsname}, user="admin", db=s)
    assert s.query(AdminAuditLog).count() == na + 1
    row = s.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
    assert row.action == "workspace.create"
    s.close()


def test_admin_spa_served():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/admin/")
    # admin-app/dist 已由 Task9 构建,应返 200 html
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
