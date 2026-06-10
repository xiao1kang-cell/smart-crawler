from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base

pytestmark = pytest.mark.unit

MAX_BATCH = 1000


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


# ---- submit_batch ----

def test_submit_batch_dedups_and_creates_queued_jobs(monkeypatch):
    from app.api import ondemand_jobs as oj
    from app.models import OnDemandJob

    s = _session()
    urls = [
        "https://www.lazada.com.my/products/a-i1.html",
        "https://www.lazada.com.my/products/a-i1.html",  # 重复
        "  ",                                            # 空行
        "https://articulo.mercadolibre.com.ar/MLA-2",
    ]
    out = oj.submit_batch(s, ws_id=1, username="u", urls=urls,
                          max_items=20, review_limit=50)
    s.commit()
    assert out["queued"] == 2
    jobs = s.query(OnDemandJob).filter_by(workspace_id=1).all()
    assert len(jobs) == 2
    assert all(j.status == "queued" for j in jobs)
    # 同批共享 batch_id
    assert len({j.batch_id for j in jobs}) == 1
    assert out["batch_id"] == jobs[0].batch_id
    # 原始参数被保存(供重试)
    assert all(j.max_items == 20 and j.review_limit == 50 for j in jobs)
    # 待入队 id 随返回值带出(由路由 commit 后入队,不在此处入队)
    assert set(out["_enqueue_ids"]) == {j.id for j in jobs}
    s.close()


def test_submit_batch_skips_unrecognized_platform(monkeypatch):
    from app.api import ondemand_jobs as oj

    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    out = oj.submit_batch(s, ws_id=1, username="u",
                          urls=["https://example.com/whatever",
                                "https://shopee.sg/x-i.1.2"],
                          max_items=20, review_limit=50)
    s.commit()
    assert out["queued"] == 1
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["url"] == "https://example.com/whatever"
    s.close()


def test_submit_batch_rejects_over_limit(monkeypatch):
    from app.api import ondemand_jobs as oj

    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    urls = [f"https://www.lazada.com.my/products/a-i{i}.html"
            for i in range(MAX_BATCH + 1)]
    with pytest.raises(ValueError) as e:
        oj.submit_batch(s, ws_id=1, username="u", urls=urls,
                        max_items=20, review_limit=50)
    assert "1000" in str(e.value)
    s.close()


def test_submit_batch_allows_queue_when_pending_exists(monkeypatch):
    """并发闸已放开:有未完成任务时,新提交应入队排队,而非拒绝。"""
    from app.api import ondemand_jobs as oj
    from app.models import OnDemandJob

    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    # 已有一条 running
    s.add(OnDemandJob(url="u", platform="lazada", status="running",
                      workspace_id=1))
    s.commit()
    out = oj.submit_batch(s, ws_id=1, username="u",
                          urls=["https://www.lazada.com.my/products/a-i1.html"],
                          max_items=20, review_limit=50)
    assert out["queued"] == 1          # 不再拒绝,直接入队
    s.close()


def test_submit_batch_empty_returns_zero(monkeypatch):
    from app.api import ondemand_jobs as oj
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    out = oj.submit_batch(s, ws_id=1, username="u", urls=["  ", ""],
                          max_items=20, review_limit=50)
    assert out["queued"] == 0
    assert out["skipped"] == []
    s.close()


# ---- retry_job ----

def _terminal_job(s, status="failed", ws=1, batch="b"):
    from app.models import OnDemandJob
    j = OnDemandJob(url="https://www.lazada.com.my/products/a-i1.html",
                    platform="lazada", status=status, batch_id=batch,
                    max_items=20, review_limit=50, attempts=1, workspace_id=ws,
                    error="boom")
    s.add(j); s.commit()
    return j


def test_retry_job_requeues_terminal(monkeypatch):
    from app.api import ondemand_jobs as oj
    s = _session()
    j = _terminal_job(s, status="failed")
    out = oj.retry_job(s, ws_id=1, job_id=j.id)
    s.commit()
    assert out["status"] == "queued"
    assert j.status == "queued"
    assert j.error is None
    assert out["_enqueue_ids"] == [j.id]
    s.close()


def test_retry_job_rejects_non_terminal(monkeypatch):
    from app.api import ondemand_jobs as oj
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    j = _terminal_job(s, status="running")
    with pytest.raises(oj.NotRetryableError):
        oj.retry_job(s, ws_id=1, job_id=j.id)
    s.close()


def test_retry_job_cross_workspace_returns_none(monkeypatch):
    from app.api import ondemand_jobs as oj
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = _session()
    j = _terminal_job(s, status="failed", ws=2)
    assert oj.retry_job(s, ws_id=1, job_id=j.id) is None
    s.close()


# ---- retry_failed_batch ----

def test_retry_failed_batch_only_failed(monkeypatch):
    from app.api import ondemand_jobs as oj
    from app.models import OnDemandJob
    s = _session()
    f1 = _terminal_job(s, status="failed", batch="B")
    f2 = _terminal_job(s, status="failed", batch="B")
    ok = _terminal_job(s, status="success", batch="B")
    out = oj.retry_failed_batch(s, ws_id=1, batch_id="B")
    s.commit()
    assert out["requeued"] == 2
    assert s.get(OnDemandJob, ok.id).status == "success"
    assert set(out["_enqueue_ids"]) == {f1.id, f2.id}
    s.close()


# ---- HTTP 端点(集成) ----

def _override_ws(routes, app, monkeypatch, ws_id):
    app.dependency_overrides[routes.require_user] = lambda: "tester"
    monkeypatch.setattr(routes, "_current_workspace",
                        lambda user, db, x=None: type("W", (), {"id": ws_id})())
    monkeypatch.setattr(routes, "_current_user",
                        lambda user, db: type("U", (), {"username": "tester"})())


def test_batch_endpoint_queues_and_allows_second(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import SessionLocal, init_db
    from app.models import OnDemandJob
    import app.api.ondemand_jobs as oj

    init_db()
    # 不真正起 worker;入队设为 no-op
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = SessionLocal()
    s.query(OnDemandJob).filter(OnDemandJob.workspace_id == 5151).delete()
    s.commit(); s.close()

    _override_ws(routes, app, monkeypatch, 5151)
    client = TestClient(app)

    r = client.post("/api/ondemand/batch", json={
        "urls": ["https://www.lazada.com.my/products/a-i1.html",
                 "https://example.com/nope"]})
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 1
    assert len(body["skipped"]) == 1

    # 并发闸已放开:仍有 queued 时第二次提交照样入队(不再 409)
    r2 = client.post("/api/ondemand/batch", json={
        "urls": ["https://articulo.mercadolibre.com.ar/MLA-2"]})
    assert r2.status_code == 200
    assert r2.json()["queued"] == 1
    app.dependency_overrides.clear()


def test_batch_endpoint_over_limit_400(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import init_db
    import app.api.ondemand_jobs as oj

    init_db()
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    _override_ws(routes, app, monkeypatch, 5252)
    client = TestClient(app)
    urls = [f"https://www.lazada.com.my/products/a-i{i}.html"
            for i in range(MAX_BATCH + 1)]
    r = client.post("/api/ondemand/batch", json={"urls": urls})
    assert r.status_code == 400
    assert "1000" in r.json()["detail"]
    app.dependency_overrides.clear()


def test_retry_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import SessionLocal, init_db
    from app.models import OnDemandJob
    import app.api.ondemand_jobs as oj

    init_db()
    monkeypatch.setattr(oj, "enqueue", lambda jid: None)
    s = SessionLocal()
    s.query(OnDemandJob).filter(OnDemandJob.workspace_id == 5353).delete()
    s.commit()
    j = OnDemandJob(url="https://www.lazada.com.my/products/a-i1.html",
                    platform="lazada", status="failed", batch_id="bx",
                    max_items=20, review_limit=50, attempts=1,
                    workspace_id=5353, error="boom")
    s.add(j); s.commit(); jid = j.id; s.close()

    _override_ws(routes, app, monkeypatch, 5353)
    client = TestClient(app)
    r = client.post(f"/api/ondemand/jobs/{jid}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"

    # 现在是 queued(非终态)→ 再 retry 得 409
    r2 = client.post(f"/api/ondemand/jobs/{jid}/retry")
    assert r2.status_code == 409
    app.dependency_overrides.clear()

