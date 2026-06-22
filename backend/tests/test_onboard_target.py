"""TDD test: verify target crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: TargetCrawler uses raw curl_cffi Session directly (_session()); make_fetcher
is never called; counter.api_calls stays 0.
After migration: every HTTP GET goes through the unified CrawlerFetcher;
counter.api_calls is incremented on each successful fetch.

Target uses RedSky JSON API:
  - SRP: redsky.target.com/redsky_aggregations/v1/web/plp_search_v2
  - PDP: redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture data: RedSky plp_search_v2 SRP response
# ---------------------------------------------------------------------------

_TCIN = "87654321"

_SRP_PAGE1 = {
    "data": {
        "search": {
            "products": [
                {"tcin": _TCIN, "item": {"product_description": {"title": "Test Desk"}}},
            ]
        }
    }
}

_SRP_EMPTY = {
    "data": {
        "search": {
            "products": []
        }
    }
}

# ---------------------------------------------------------------------------
# Fixture data: RedSky pdp_client_v1 PDP response
# ---------------------------------------------------------------------------

_PDP_RESPONSE = {
    "data": {
        "product": {
            "item": {
                "product_description": {
                    "title": "Test Desk",
                    "downstream_description": "A solid office desk.",
                    "soft_bullets": {"bullets": ["Sturdy frame", "Easy assembly"]},
                },
                "enrichment": {
                    "images": {
                        "primary_image_url": "https://target.scene7.com/is/image/Target/desk.jpg",
                        "alternate_image_urls": [],
                    }
                },
                "primary_brand": {"name": "Threshold"},
                "fulfillment": {"is_out_of_stock_in_all_store_locations": False},
                "product_classification": {
                    "item_type": {"name": "Desks"},
                },
            },
            "price": {
                "current_retail": 129.99,
                "reg_retail": 159.99,
            },
            "ratings_and_reviews": {
                "statistics": {
                    "rating": {"average": 4.5, "count": 312},
                }
            },
        }
    }
}


def _site() -> Site:
    s = Site()
    s.site = "target"
    s.url = "https://www.target.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "target"
    s.brand = "Target"
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

def test_target_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.target import TargetCrawler, _HOME_KW

    monkeypatch.setenv("TARGET_USE_REDSKY", "1")
    crawler = TargetCrawler(_site(), limit=1)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    calls: list[str] = []
    srp_hits: dict[str, int] = {}

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1

        if "plp_search_v2" in url:
            kw_param = kw.get("params", {}).get("keyword", "")
            key = f"srp:{kw_param}"
            srp_hits[key] = srp_hits.get(key, 0) + 1
            # First keyword first page returns 1 product; everything else empty → terminates
            if kw_param == _HOME_KW[0] and srp_hits[key] == 1:
                return _ok_result(url, _SRP_PAGE1)
            return _ok_result(url, _SRP_EMPTY)

        if "pdp_client_v1" in url:
            return _ok_result(url, _PDP_RESPONSE)

        return _ok_result(url, {})

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # At least SRP + PDP calls must have gone through make_fetcher
    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (SRP + PDP), got {crawler.counter.api_calls}. "
        f"URLs fetched: {calls}"
    )

    # Must have parsed at least one product
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {result.products}. Notes: {result.notes}"
    )

    row = result.products[0]
    assert row["sku"] == _TCIN
    assert row["title"] == "Test Desk"
    assert row["sale_price"] == 129.99
    assert row["original_price"] == 159.99
    assert row["currency"] == "USD"
    assert row["brand"] == "Threshold"
    assert row["site"] == "target"
    assert row["status"] == "on_sale"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 312


def test_target_srp_pagination_terminates(monkeypatch):
    """SRP pagination loop terminates when plp_search_v2 returns empty products."""
    from app.crawlers.target import TargetCrawler, _HOME_KW

    monkeypatch.setenv("TARGET_USE_REDSKY", "1")
    crawler = TargetCrawler(_site(), limit=1)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    srp_calls: list[str] = []

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1

        if "plp_search_v2" in url:
            kw_param = kw.get("params", {}).get("keyword", "")
            srp_calls.append(kw_param)
            # First page of first keyword returns data; all others empty → break inner loop
            if kw_param == _HOME_KW[0] and len(srp_calls) == 1:
                return _ok_result(url, _SRP_PAGE1)
            return _ok_result(url, _SRP_EMPTY)

        if "pdp_client_v1" in url:
            return _ok_result(url, _PDP_RESPONSE)

        return _ok_result(url, {})

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # Should have terminated: SRP returns empty after first page → break
    # At minimum: 1 SRP (data) + 1 SRP (empty→break for kw[0])
    # Then subsequent keywords also return empty → break their inner loops immediately
    assert len(srp_calls) >= 2, (
        f"Expected >=2 SRP calls (1 data + 1 empty terminator per kw), got {srp_calls}"
    )

    # Should have parsed at least 1 product from the non-empty first page
    assert len(result.products) >= 1, (
        f"Expected >=1 product after pagination, got {result.products}. "
        f"Notes: {result.notes}"
    )


def test_target_map_pdp_not_degraded():
    """_map_pdp still correctly extracts all fields from PDP JSON."""
    from app.crawlers.target import TargetCrawler

    crawler = TargetCrawler(_site(), limit=1)
    url = f"https://www.target.com/p/-/A-{_TCIN}"
    row = crawler._map_pdp(_PDP_RESPONSE, _TCIN, url)

    assert row is not None, "_map_pdp returned None on valid PDP JSON"
    assert row["sku"] == _TCIN
    assert row["spu"] == _TCIN
    assert row["title"] == "Test Desk"
    assert row["description"] == "A solid office desk."
    assert row["sale_price"] == 129.99
    assert row["original_price"] == 159.99
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 312
    assert row["status"] == "on_sale"
    assert row["brand"] == "Threshold"
    assert row["category_path"] == "Desks"
    assert "https://target.scene7.com/is/image/Target/desk.jpg" in row["image_urls"]
    assert row["product_url"] == url
    assert row["site"] == "target"


def test_target_default_crawl_uses_sitemap_only(monkeypatch):
    from app.crawlers.target import SITEMAP_INDEX, TargetCrawler

    crawler = TargetCrawler(_site(), limit=1)
    shard = "https://www.target.com/pdp/sitemap_00-0001.xml.gz"
    url = (
        "https://www.target.com/p/"
        "creative-products-trick-or-treat-cat-16x16-indoor-outdoor-pillow"
        "/-/A-1000008858"
    )
    index = f"<sitemapindex><sitemap><loc>{shard}</loc></sitemap></sitemapindex>"
    sitemap = (
        "<urlset><url>"
        f"<loc>{url}</loc>"
        "<image:image><image:loc>https://target.scene7.com/is/image/Target/x</image:loc></image:image>"
        "</url></urlset>"
    )
    calls: list[str] = []

    class _FakeFetcher:
        def get(self, u: str, **kw) -> FetchResult:
            calls.append(u)
            crawler.counter.api_calls += 1
            text = index if u == SITEMAP_INDEX else sitemap
            return FetchResult(
                ok=True,
                url=u,
                status=200,
                text=text,
                content=text.encode(),
                final_url=u,
                fetcher="curl_cffi",
            )

    monkeypatch.delenv("TARGET_USE_REDSKY", raising=False)
    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    assert calls == [SITEMAP_INDEX, shard]
    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "1000008858"
    assert "Indoor Outdoor Pillow" in row["title"]
    assert row["image_urls"] == ["https://target.scene7.com/is/image/Target/x"]
    assert row["attributes"]["source"] == "sitemap"
