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


def test_add_tracking_falls_back_to_generic_when_undetectable():
    init_db()
    client = TestClient(app)
    with patch("app.api.tracking.detect_platform",
               return_value=(None, "https://static.example.com")), \
         patch("app.api.tracking.enqueue", return_value=1001) as enq:
        r = client.post("/api/tracking", headers=_admin_headers(),
                        json={"url": "https://static.example.com"})
    assert r.status_code == 200
    assert r.json()["platform"] == "generic"
    enq.assert_called_once()


def test_add_tracking_forbidden_for_non_admin():
    init_db()
    client = TestClient(app)
    r = client.post("/api/tracking",
                    headers={"Authorization": f"Bearer {make_token('viewer_user', '')}"},
                    json={"url": "https://x.example.com"})
    assert r.status_code in (401, 403)


def test_trigger_reuses_active_job_for_same_site():
    init_db()
    client = TestClient(app)
    code = _make_user_site(client)

    first = client.post(f"/api/jobs/trigger?site={code}", headers=_admin_headers())
    assert first.status_code == 200, first.text
    first_job = first.json()["jobs"][0]

    second = client.post(f"/api/jobs/trigger?site={code}", headers=_admin_headers())
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "already_running"
    assert body["jobs"] == [first_job]
    assert body["existing_jobs"] == [first_job]


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


def test_delete_user_site_and_remove_seed_site_from_workspace():
    init_db(); client = TestClient(app)
    code = _make_user_site(client)
    deleted = client.delete(f"/api/tracking/{code}", headers=_admin_headers())
    assert deleted.status_code == 200
    assert deleted.json()["deleted_site"] is True
    from app.db import SessionLocal
    from app.models import Site
    s = SessionLocal()
    yaml_site = s.query(Site).filter_by(source="yaml").first()
    yaml_code = yaml_site.site if yaml_site else None
    s.close()
    if yaml_code:
        removed = client.delete(f"/api/tracking/{yaml_code}", headers=_admin_headers())
        assert removed.status_code == 200
        assert removed.json()["deleted_site"] is False
        s = SessionLocal()
        try:
            assert s.query(Site).filter_by(site=yaml_code).first() is not None
        finally:
            s.close()
        listed = client.get("/api/tracking", headers=_admin_headers()).json()["items"]
        assert yaml_code not in {item["site"] for item in listed}


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


def test_export_returns_xlsx():
    init_db(); client = TestClient(app)
    _make_user_site(client)
    from app.auth import make_token
    from openpyxl import load_workbook
    import io
    r = client.get(f"/api/tracking/export?token={make_token('admin', '')}")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers.get("content-type", "")
    assert r.content[:2] == b"PK"  # xlsx = zip
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    headers = [cell.value for cell in next(wb.active.iter_rows(max_row=1))]
    assert "Created Time" in headers
    assert "Creator" in headers
    assert "30-Day Revenue" in headers
    assert client.get("/api/tracking/export?token=bogus").status_code == 401
