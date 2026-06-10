from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_ondemand_fetch_endpoint(monkeypatch):
    """单条 fetch 异步:返回 queued 计数,不再同步返回 listings/reviews。"""
    from fastapi.testclient import TestClient

    import app.api.routes as routes
    import app.api.ondemand_jobs as oj
    from app.main import app
    from app.db import init_db

    init_db()
    enqueued = []
    monkeypatch.setattr(oj, "enqueue", lambda jid: enqueued.append(jid))

    app.dependency_overrides[routes.require_user] = lambda: "tester"
    monkeypatch.setattr(routes, "_current_workspace",
                        lambda user, db, x=None: type("W", (), {"id": 1})())
    monkeypatch.setattr(routes, "_current_user",
                        lambda user, db: type("U", (), {"username": "tester"})())

    client = TestClient(app)
    resp = client.post("/api/ondemand/fetch",
                       json={"url": "https://www.lazada.com.my/products/x-i1-s2.html"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 1
    assert len(enqueued) == 1
    app.dependency_overrides.clear()
