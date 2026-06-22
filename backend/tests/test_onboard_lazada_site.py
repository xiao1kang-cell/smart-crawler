from __future__ import annotations

import pytest

from app.models import Site

pytestmark = pytest.mark.unit


def _site() -> Site:
    s = Site()
    s.site = "lazada_id_makeup"
    s.url = "https://www.lazada.co.id/shop-makeup/"
    s.country = "ID"
    s.proxy_tier = "none"
    s.platform = "lazada"
    s.brand = "Lazada ID"
    return s


def test_lazada_site_crawler_discovers_listing_urls_and_reuses_pdp_parser(monkeypatch):
    from app.crawlers.lazada import LazadaCrawler

    crawler = LazadaCrawler(_site(), limit=2)

    html = """
    <a href="/products/test-lipstick-i10001-s20001.html">Lipstick</a>
    <a href="//www.lazada.co.id/products/test-powder-i10002.html">Powder</a>
    """
    rows = {
        "10001": {"sku": "10001", "title": "Lipstick", "product_url": "u1"},
        "10002": {"sku": "10002", "title": "Powder", "product_url": "u2"},
    }

    monkeypatch.setattr(crawler.ondemand, "_render", lambda url, proxy=None: html)
    monkeypatch.setattr(
        crawler.ondemand,
        "fetch_listing",
        lambda item_id, url, proxy=None: {**rows[item_id], "product_url": url},
    )
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    result = crawler.crawl()

    assert [p["sku"] for p in result.products] == ["10001", "10002"]
    assert all(p["site"] == "lazada_id_makeup" for p in result.products)
    assert "发现 2 个 PDP URL" in result.notes[0]
