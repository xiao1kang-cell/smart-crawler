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
