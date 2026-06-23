"""TDD test: verify shopify crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: ShopifyCrawler uses raw curl_cffi Session directly; make_fetcher
is never called; counter.api_calls stays 0.
After migration: every HTTP GET goes through the unified CrawlerFetcher;
counter.api_calls is incremented on each successful fetch.
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_PRODUCT = {
    "id": 1001,
    "title": "Test Chair",
    "handle": "test-chair",
    "body_html": "<p>A comfortable chair</p>",
    "product_type": "Furniture",
    "tags": "chair, comfort",
    "published_at": "2024-01-15T10:00:00-05:00",
    "options": [{"name": "Color"}, {"name": "Size"}],
    "images": [{"src": "https://example.myshopify.com/products/chair.jpg"}],
    "variants": [
        {
            "id": 2001,
            "sku": "CHAIR-RED-L",
            "price": "129.99",
            "compare_at_price": "149.99",
            "available": True,
            "inventory_quantity": 10,
            "grams": 500,
            "option1": "Red",
            "option2": "Large",
        }
    ],
}

_NEW_COLLECTION_PRODUCTS = {
    "products": [
        {
            "id": 1001,
            "handle": "test-chair",
            "title": "Test Chair",
        }
    ]
}

_EMPTY_PRODUCTS = {"products": []}

_COLLECTIONS_PAGE1 = {
    "collections": [
        {
            "id": 5001,
            "title": "Chairs",
            "handle": "chairs",
            "products_count": 10,
        }
    ]
}

_EMPTY_COLLECTIONS = {"collections": []}


def _site() -> Site:
    s = Site()
    s.site = "shopify_test"
    s.url = "https://example.myshopify.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "shopify"
    s.brand = "TestBrand"
    return s


# ---------------------------------------------------------------------------
# Helper to build FetchResult from a dict
# ---------------------------------------------------------------------------

def _ok_result(url: str, body: dict) -> FetchResult:
    text = json.dumps(body)
    return FetchResult(
        ok=True,
        url=url,
        status=200,
        text=text,
        content=text.encode(),
        final_url=url,
        fetcher="curl_cffi",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_shopify_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.shopify import ShopifyCrawler

    crawler = ShopifyCrawler(_site())

    calls: list[str] = []
    # Track per-path call counts to enable pagination termination
    path_hits: dict[str, int] = {}

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1

        # Determine which counter for this path
        path = url.split("example.myshopify.com", 1)[-1].split("?")[0]
        path_hits[path] = path_hits.get(path, 0) + 1
        hit = path_hits[path]

        # new/top-picks/best-sellers collections: return 1 product on first call, empty after
        if "/collections/new/products.json" in url:
            if hit == 1:
                return _ok_result(url, _NEW_COLLECTION_PRODUCTS)
            return _ok_result(url, _EMPTY_PRODUCTS)

        if "/collections/top-picks/products.json" in url or \
           "/collections/best-sellers/products.json" in url:
            return _ok_result(url, _EMPTY_PRODUCTS)

        # Main products.json: return 1 product on first call, empty after (terminates loop)
        if "/products.json" in url:
            if hit == 1:
                return _ok_result(url, {"products": [_PRODUCT]})
            return _ok_result(url, _EMPTY_PRODUCTS)

        # collections.json: return 1 collection on first call, empty after
        if "/collections.json" in url:
            if hit == 1:
                return _ok_result(url, _COLLECTIONS_PAGE1)
            return _ok_result(url, _EMPTY_COLLECTIONS)

        # Fallback: return empty for unknown URLs
        return _ok_result(url, {})

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # At least one API call must have gone through make_fetcher
    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls, got {crawler.counter.api_calls}. "
        f"URLs fetched: {calls}"
    )

    # Must have parsed at least one product (from _expand)
    assert isinstance(result.products, list)
    assert len(result.products) >= 1, (
        f"Expected >=1 product rows, got {result.products}. Notes: {result.notes}"
    )

    # Verify the expanded product row has key fields from _expand
    row = result.products[0]
    assert row["sku"] == "CHAIR-RED-L"
    assert row["title"] == "Test Chair"
    assert row["sale_price"] == "129.99"
    assert row["original_price"] == "149.99"
    assert row["currency"] == "USD"
    assert row["site"] == "shopify_test"
    assert row["brand"] == "TestBrand"
    assert row["status"] == "on_sale"
    # Variant attributes should be parsed from option names
    assert row["attributes"] == {"Color": "Red", "Size": "Large"}


def test_shopify_products_json_pagination_terminates(monkeypatch):
    """Pagination loop terminates when products.json returns empty page."""
    from app.crawlers.shopify import ShopifyCrawler

    crawler = ShopifyCrawler(_site())

    products_hit = {"n": 0}

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1

        if "/collections/" in url and "/products.json" in url:
            return _ok_result(url, _EMPTY_PRODUCTS)
        if "/products.json" in url:
            products_hit["n"] += 1
            # Return 1 product on first page only, then empty → terminates
            if products_hit["n"] == 1:
                return _ok_result(url, {"products": [_PRODUCT]})
            return _ok_result(url, _EMPTY_PRODUCTS)
        if "/collections.json" in url:
            return _ok_result(url, _EMPTY_COLLECTIONS)
        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    result = crawler.crawl()

    # products.json should have been called exactly 2 times: page1 (data) + page2 (empty→break)
    assert products_hit["n"] == 2, (
        f"Expected 2 products.json calls (1 data + 1 empty terminator), got {products_hit['n']}"
    )
    assert len(result.products) >= 1


def test_shopify_marks_partial_when_products_json_hits_page_cap(monkeypatch):
    """If products.json never returns an empty page before MAX_PAGES, coverage is not proven."""
    import app.crawlers.shopify as shopify_mod
    from app.crawlers.shopify import ShopifyCrawler

    monkeypatch.setattr(shopify_mod, "MAX_PAGES", 2)
    crawler = ShopifyCrawler(_site())

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1

        if "/collections/" in url and "/products.json" in url:
            return _ok_result(url, _EMPTY_PRODUCTS)
        if "/products.json" in url:
            return _ok_result(url, {"products": [_PRODUCT]})
        if "/collections.json" in url:
            return _ok_result(url, _EMPTY_COLLECTIONS)
        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    result = crawler.crawl()

    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_discovery"
    assert "MAX_PAGES=2" in result.coverage_reason
    assert result.total_product_count > len(result.products)


def test_shopify_new_handle_label(monkeypatch):
    """Products whose handle appears in new collection get is_new=True label."""
    from app.crawlers.shopify import ShopifyCrawler

    crawler = ShopifyCrawler(_site())

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1

        if "/collections/new/products.json" in url:
            # new collection contains test-chair handle
            if "page=1" in url:
                return _ok_result(url, _NEW_COLLECTION_PRODUCTS)
            return _ok_result(url, _EMPTY_PRODUCTS)

        if "/collections/top-picks/products.json" in url or \
           "/collections/best-sellers/products.json" in url:
            return _ok_result(url, _EMPTY_PRODUCTS)

        if "/products.json" in url:
            if "page=1" in url:
                return _ok_result(url, {"products": [_PRODUCT]})
            return _ok_result(url, _EMPTY_PRODUCTS)

        if "/collections.json" in url:
            return _ok_result(url, _EMPTY_COLLECTIONS)

        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    result = crawler.crawl()

    assert len(result.products) >= 1
    row = result.products[0]
    # test-chair is in the new collection
    assert row["is_new"] is True
    assert row["label"] == "NEW"
