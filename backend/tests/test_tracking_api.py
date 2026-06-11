"""标杆追踪面板 API + 迁移测试。"""
from unittest.mock import patch

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


def test_add_tracking_creates_site_and_enqueues():
    init_db()
    client = TestClient(app)
    with patch("app.api.tracking.detect_platform",
               return_value=("shopify", "https://newbrand.example.com")), \
         patch("app.api.tracking.enqueue", return_value=999) as enq:
        r = client.post("/api/tracking",
                        headers=_admin_headers(),
                        json={"url": "https://newbrand.example.com/x", "brand": "NewBrand", "country": "US"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == "shopify"
    assert body["source"] == "user"
    assert body["track_status"] == "tracking"
    enq.assert_called_once()
    assert client.get("/api/tracking", headers=_admin_headers()).json()["total"] >= 1


def test_add_tracking_400_when_undetectable():
    init_db()
    client = TestClient(app)
    with patch("app.api.tracking.detect_platform",
               return_value=(None, "https://static.example.com")):
        r = client.post("/api/tracking", headers=_admin_headers(),
                        json={"url": "https://static.example.com"})
    assert r.status_code == 400


def test_add_tracking_forbidden_for_non_admin():
    init_db()
    client = TestClient(app)
    r = client.post("/api/tracking",
                    headers={"Authorization": f"Bearer {make_token('viewer_user', '')}"},
                    json={"url": "https://x.example.com"})
    assert r.status_code in (401, 403)
