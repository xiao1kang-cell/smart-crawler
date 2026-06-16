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


def test_jobs_stats_aggregates_all_queue_tables():
    init_db()
    from datetime import datetime, timedelta
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
    s.add(CrawlJob(site="site_d", status="pending",
                   created_at=datetime.utcnow() - timedelta(hours=2)))
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
    failed = admin_spine.jobs_list(status="failed", dataset=None, tenant=None,
                                   source="all", page=1, size=20,
                                   user="admin", db=s)

    assert stats["success"] == 1
    assert stats["blocked"] == 1
    assert stats["stuck"] == 1
    assert stats["pending"] == 2
    assert stats["stale_pending"] == 1
    assert stats["partial"] == 1
    assert stats["by_queue"]["crawl"]["total"] == 4
    assert stats["by_queue"]["crawl"]["stale_pending"] == 1
    assert stats["by_queue"]["ondemand"]["total"] == 2
    assert stats["breakdowns"]["crawl_blocked_by_site"] == [{"key": "site_b", "count": 1}]
    assert stats["breakdowns"]["crawl_stale_pending_by_site"] == [{"key": "site_d", "count": 1}]
    assert stats["breakdowns"]["crawl_failure_codes"] == [{"key": "anti_bot_challenge", "count": 1}]
    assert {row["source"] for row in listed["items"]} == {"crawl", "ondemand"}
    assert running["total"] == 0
    assert stuck["total"] == 1
    assert failed["total"] == 0
    blocked = admin_spine.jobs_list(status="blocked", dataset="site_b", tenant=None,
                                    source="crawl", page=1, size=20,
                                    failure_code="anti_bot_challenge",
                                    user="admin", db=s)
    assert blocked["total"] == 1
    assert blocked["items"][0]["failure_stage"] == "fetch"
    assert blocked["items"][0]["retryable"] is True
    assert blocked["items"][0]["suggested_action"] == "use residential proxy"
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
                   created_at=datetime.utcnow() - timedelta(hours=2)))
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
