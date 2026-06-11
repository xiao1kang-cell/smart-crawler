"""标杆追踪面板 API + 迁移测试。"""
from sqlalchemy import inspect

from app.db import engine, init_db


def test_site_has_tracking_columns():
    init_db()
    cols = {c["name"] for c in inspect(engine).get_columns("sites")}
    for col in ("track_status", "source", "creator", "review_rate",
                "created_at", "updated_at"):
        assert col in cols, f"sites 缺列 {col}"


from fastapi.testclient import TestClient

from app.main import app
from app.auth import make_token


def _admin_headers():
    return {"Authorization": f"Bearer {make_token('admin', '')}"}


def test_tracking_list_requires_auth():
    init_db()
    client = TestClient(app)
    assert client.get("/api/tracking").status_code == 401


def test_tracking_list_returns_items_shape():
    init_db()
    client = TestClient(app)
    r = client.get("/api/tracking", headers=_admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body
