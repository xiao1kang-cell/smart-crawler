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


def _make_user_site(client):
    """借 POST 建一个 source=user 的站,返回其 site code。"""
    with patch("app.api.tracking.detect_platform",
               return_value=("shopify", "https://edit.example.com")), \
         patch("app.api.tracking.enqueue", return_value=1):
        r = client.post("/api/tracking", headers=_admin_headers(),
                        json={"url": "https://edit.example.com", "brand": "B", "country": "US"})
    return r.json()["site"]


def test_patch_edits_brand_and_review_rate():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    r = client.patch(f"/api/tracking/{code}", headers=_admin_headers(),
                     json={"brand": "Edited", "review_rate": 0.03})
    assert r.status_code == 200
    assert r.json()["brand"] == "Edited"
    assert r.json()["review_rate"] == 0.03


def test_pause_and_resume():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    assert client.post(f"/api/tracking/{code}/pause", headers=_admin_headers()).json()["track_status"] == "paused"
    assert client.post(f"/api/tracking/{code}/resume", headers=_admin_headers()).json()["track_status"] == "tracking"


def test_delete_only_user_source():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    assert client.delete(f"/api/tracking/{code}", headers=_admin_headers()).status_code == 200
    from app.db import SessionLocal
    from app.models import Site
    s = SessionLocal()
    yaml_site = s.query(Site).filter_by(source="yaml").first()
    s.close()
    if yaml_site:
        assert client.delete(f"/api/tracking/{yaml_site.site}", headers=_admin_headers()).status_code == 400


def test_patch_invalid_review_rate_400():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    r = client.patch(f"/api/tracking/{code}", headers=_admin_headers(), json={"review_rate": "abc"})
    assert r.status_code == 400


def test_paused_site_skips_enqueue():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    client.post(f"/api/tracking/{code}/pause", headers=_admin_headers())
    from app.scheduler import _product_job
    with patch("app.runner.enqueue") as enq:
        _product_job(code)
        enq.assert_not_called()
