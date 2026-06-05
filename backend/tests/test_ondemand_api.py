from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_ondemand_fetch_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.routes as routes
    from app.main import app
    from app.ondemand.base import OnDemandResult

    def fake_fetch(url, *, max_items, review_limit):
        r = OnDemandResult()
        r.add_listing({"sku": "X", "title": "t", "site": "ondemand_lazada",
                       "product_url": url, "sale_price": 9.9})
        r.add_reviews([{"review_id": "rv", "rating": 4, "content": "ok"}])
        r.note("done")
        return r

    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)
    # 绕过登录依赖
    app.dependency_overrides[routes.require_user] = lambda: "tester"

    client = TestClient(app)
    resp = client.post("/api/ondemand/fetch",
                       json={"url": "https://www.lazada.com.my/products/x-i1-s2.html"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["listings_count"] == 1
    assert body["reviews_count"] == 1
    assert body["listings"][0]["sku"] == "X"
    app.dependency_overrides.clear()
