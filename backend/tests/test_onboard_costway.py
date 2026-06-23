"""TDD test: verify costway crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: CostwayCrawler uses raw curl_cffi Session directly; make_fetcher
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
# Fixture data — mirrors real /api/products response structure
# ---------------------------------------------------------------------------

def _make_product(sku: str, name: str, entity_id: int = 1001) -> dict:
    return {
        "sku": sku,
        "entity_id": entity_id,
        "name": name,
        "price": {"price": "199.99", "special_price": "149.99"},
        "images": {
            "baseImage": f"https://cdn.costway.com/{sku}/main.jpg",
            "small_image": f"https://cdn.costway.com/{sku}/small.jpg",
        },
        "rating": {"score": 4.5, "count": 123},
        "inventory": {"qty": 50},
        "product_tag": None,
        "has_video": False,
        "request_path": f"products/{sku.lower()}.html",
        "type_id": "simple",
        "url_path": f"products/{sku.lower()}",
    }


# /api/products?category_id=10&page=1&pagesize=48 → result.data = list of products
_PRODUCT_A = _make_product("CW0001", "Outdoor Chaise Lounge", entity_id=1001)
_PRODUCT_B = _make_product("CW0002", "Folding Garden Chair", entity_id=1002)

_PRODUCTS_PAGE1 = {
    "result": {
        "data": [_PRODUCT_A, _PRODUCT_B],
        "total": 2,
    }
}
_PRODUCTS_EMPTY = {
    "result": {
        "data": [],
        "total": 0,
    }
}

# /api/category
_CATEGORY_RESP = {
    "result": [
        {
            "entity_id": 10,
            "name": "Outdoor Furniture",
            "url_path": "outdoor-furniture",
            "parent_id": None,
            "level": 2,
        }
    ]
}

# /api/home-newarrivals and /api/home-bestseller
_NEW_ARRIVALS = {"result": [{"sku": "CW0001"}]}
_BEST_SELLERS = {"result": {"product": [{"sku": "CW0002"}]}}


def _site() -> Site:
    s = Site()
    s.site = "costway_test"
    s.url = "https://www.costway.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "vue_spa"
    s.brand = "Costway"
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

def test_costway_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())

    # Track per-path hits to allow pagination termination
    path_hits: dict[str, int] = {}

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        # strip query string for path matching
        path = url.split("costway.com", 1)[-1].split("?")[0]
        path_hits[path] = path_hits.get(path, 0) + 1
        hit = path_hits[path]

        if path == "/api/home-newarrivals":
            return _ok_result(url, _NEW_ARRIVALS)
        if path == "/api/home-bestseller":
            return _ok_result(url, _BEST_SELLERS)
        if path == "/api/category":
            return _ok_result(url, _CATEGORY_RESP)
        if path == "/api/products":
            # first page has data; subsequent pages are empty → terminates
            if hit == 1:
                return _ok_result(url, _PRODUCTS_PAGE1)
            return _ok_result(url, _PRODUCTS_EMPTY)

        return _ok_result(url, {})

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # At least one API call must have gone through make_fetcher
    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls, got {crawler.counter.api_calls}. "
        f"Notes: {result.notes}"
    )

    # Must have parsed at least 2 products
    assert len(result.products) >= 2, (
        f"Expected >=2 products, got {result.products}. Notes: {result.notes}"
    )

    # Verify mapping of first product
    rows = {r["sku"]: r for r in result.products}
    assert "CW0001" in rows
    row = rows["CW0001"]
    assert row["title"] == "Outdoor Chaise Lounge"
    assert row["sale_price"] == 149.99   # special_price < price → uses special
    assert row["original_price"] == 199.99
    assert row["currency"] == "USD"
    assert row["site"] == "costway_test"
    assert row["brand"] == "Costway"
    assert row["is_new"] is True         # CW0001 appears in new_arrivals
    assert row["category_path"] == "Outdoor Furniture"


def test_costway_products_pagination_terminates(monkeypatch):
    """Pagination loop must stop when /api/products returns empty data list."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())

    products_hits: list[int] = []

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        path = url.split("costway.com", 1)[-1].split("?")[0]

        if path in ("/api/home-newarrivals", "/api/home-bestseller"):
            return _ok_result(url, {"result": []})
        if path == "/api/category":
            return _ok_result(url, _CATEGORY_RESP)
        if path == "/api/products":
            products_hits.append(1)
            n = len(products_hits)
            # page 1 → data; page 2 → empty; loop should break
            if n == 1:
                return _ok_result(url, _PRODUCTS_PAGE1)
            return _ok_result(url, _PRODUCTS_EMPTY)

        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    result = crawler.crawl()

    # Should have fetched exactly 2 pages: page 1 (data) + page 2 (empty → break)
    assert len(products_hits) == 2, (
        f"Expected 2 /api/products calls, got {len(products_hits)}. Notes: {result.notes}"
    )
    assert len(result.products) == 2


def test_costway_bestseller_label_set(monkeypatch):
    """Products whose SKU appears in home-bestseller get is_bestseller=True."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        path = url.split("costway.com", 1)[-1].split("?")[0]

        if path == "/api/home-newarrivals":
            return _ok_result(url, {"result": []})
        if path == "/api/home-bestseller":
            # CW0002 is bestseller
            return _ok_result(url, {"result": {"product": [{"sku": "CW0002"}]}})
        if path == "/api/category":
            return _ok_result(url, _CATEGORY_RESP)
        if path == "/api/products":
            # first call has data; second call empty
            if not hasattr(fake_get, "_hit"):
                fake_get._hit = 0
            fake_get._hit += 1
            if fake_get._hit == 1:
                return _ok_result(url, _PRODUCTS_PAGE1)
            return _ok_result(url, _PRODUCTS_EMPTY)

        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    result = crawler.crawl()

    rows = {r["sku"]: r for r in result.products}
    assert "CW0002" in rows
    assert rows["CW0002"]["is_bestseller"] is True
    # CW0001 is not in bestseller list
    assert rows["CW0001"]["is_bestseller"] is False


def test_costway_total_counts_discovered_items_not_only_parsed_rows(monkeypatch):
    """A malformed API item still belongs in the run total if it has a stable URL."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())
    malformed = dict(_PRODUCT_B)
    malformed.pop("sku")

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        path = url.split("costway.com", 1)[-1].split("?")[0]

        if path in ("/api/home-newarrivals", "/api/home-bestseller"):
            return _ok_result(url, {"result": []})
        if path == "/api/category":
            return _ok_result(url, _CATEGORY_RESP)
        if path == "/api/products":
            if not hasattr(fake_get, "_hit"):
                fake_get._hit = 0
            fake_get._hit += 1
            if fake_get._hit == 1:
                return _ok_result(url, {
                    "result": {"data": [_PRODUCT_A, malformed], "total": 2}
                })
            return _ok_result(url, _PRODUCTS_EMPTY)

        return _ok_result(url, {})

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2


def test_costway_concurrency_stays_serial_without_proxy_lease(monkeypatch):
    """Costway must not parallelize from the server IP without proxy leases."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())
    crawler.site.crawler_config = {"detail_concurrency": 8, "listing_concurrency": 8}
    monkeypatch.setenv("COSTWAY_CONCURRENCY", "8")

    assert crawler._listing_concurrency() == 1
    assert crawler._detail_concurrency() == 1
    assert crawler._proxy_lease_ttl_sec(default=0) == 0


def test_costway_concurrency_enabled_by_proxy_lease_config(monkeypatch):
    """Configured proxy lease TTL is the switch that allows Costway concurrency."""
    from app.crawlers.costway import CostwayCrawler

    crawler = CostwayCrawler(_site())
    crawler.site.crawler_config = {
        "proxy_lease_ttl_sec": 300,
        "detail_concurrency": 6,
        "listing_concurrency": 10,
    }
    monkeypatch.delenv("COSTWAY_CONCURRENCY", raising=False)

    assert crawler._listing_concurrency() == 10
    assert crawler._detail_concurrency() == 6
