from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_fetch_listing_voc_tool_exists_and_shapes_output(monkeypatch):
    import app.mcp_server as mcp_server
    from app.ondemand.base import OnDemandResult

    def fake_fetch(url, *, max_items, review_limit):
        r = OnDemandResult()
        r.add_listing({"sku": "111_222", "title": "Mouse",
                       "site": "ondemand_shopee", "product_url": url,
                       "sale_price": 15.99})
        r.add_reviews([{"review_id": "555", "rating": 5, "content": "ok"}])
        r.note("done")
        return r

    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)

    result = mcp_server._call_fetch_listing_voc("https://shopee.com.my/x-i.111.222")
    assert result["listings"][0]["sku"] == "111_222"
    assert result["reviews_count"] == 1
    assert "notes" in result
