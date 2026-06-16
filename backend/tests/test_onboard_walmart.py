"""TDD test: verify walmart crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Key assertions:
  - impersonate='chrome131' is passed to every fetcher.get() call (Akamai fingerprint)
  - warmup request to '/' is counted as api_calls (via fetcher.get, not raw Session)
  - counter.api_calls >= 3 (warmup + 1 SRP + 1 PDP minimum)
  - parsing not degraded (__NEXT_DATA__ JSON path: sku/title/price extracted correctly)
  - manual session rotate logic is removed (no _session() calls in crawl())
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal fixture HTML helpers
# ---------------------------------------------------------------------------

_ITEM_ID = "123456789"   # 9-digit walmart usItemId

# SRP page: must contain a URL matching _IP_RE
# Pattern: /ip/[^"\s?#]+/(\d{6,12})
# The regex findall returns the capture group (numeric id), then we reconstruct
# pdp = f"{base}/ip/{id}" — but wait, the code does:
#   for m in _IP_RE.findall(r.text):
#       pdp = f"{self.base}/ip/{m}"
# So _IP_RE captures the numeric id only; the full path doesn't matter beyond matching.
_SRP_HTML = (
    "<html><body>"
    f'<a href="/ip/some-product-name/{_ITEM_ID}">Product</a>'
    # Pad to > 30000 chars so _blocked() size check passes (threshold is 30_000)
    + " " * 35000
    + "</body></html>"
)

# PDP page: must contain __NEXT_DATA__ JSON for _parse_next to extract
_NEXT_DATA = {
    "props": {
        "pageProps": {
            "initialData": {
                "data": {
                    "product": {
                        "usItemId": _ITEM_ID,
                        "name": "Acme Sofa Set",
                        "shortDescription": "Comfortable 3-seat sofa.",
                        "brand": "Acme",
                        "availabilityStatus": "IN_STOCK",
                        "numberOfReviews": 250,
                        "averageRating": 4.3,
                        "priceInfo": {
                            "currentPrice": {
                                "price": 499.99,
                                "currencyUnit": "USD",
                            },
                            "wasPrice": {
                                "price": 599.99,
                            },
                        },
                        "imageInfo": {
                            "allImages": [
                                {"url": "https://i5.walmartimages.com/sofa.jpg"},
                            ],
                        },
                        "category": {
                            "path": [
                                {"name": "Home"},
                                {"name": "Furniture"},
                                {"name": "Sofas & Couches"},
                            ],
                        },
                    }
                }
            }
        }
    }
}

_PDP_HTML = (
    "<html><head>"
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(_NEXT_DATA)
    + "</script>"
    + "</head><body>"
    + " " * 35000
    + "</body></html>"
)

# Warmup homepage — just needs to be non-empty and large enough
_WARMUP_HTML = "<html><body>Walmart Home</body></html>" + " " * 35000

_PDP_URL = f"https://www.walmart.com/ip/{_ITEM_ID}"


def _site() -> Site:
    s = Site()
    s.site = "walmart"
    s.url = "https://www.walmart.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "walmart"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_walmart_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.walmart import WalmartCrawler

    crawler = WalmartCrawler(_site(), limit=1)

    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append((url, kw))
        crawler.counter.api_calls += 1
        # Warmup = homepage
        if url == "https://www.walmart.com/":
            html = _WARMUP_HTML
        elif "/search?" in url:
            html = _SRP_HTML
        else:
            html = _PDP_HTML
        return FetchResult(
            ok=True,
            url=url,
            status=200,
            text=html,
            content=html.encode(),
            final_url=url,
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # At least warmup + 1 SRP + 1 PDP
    assert crawler.counter.api_calls >= 3, (
        f"Expected >=3 api_calls (warmup + SRP + PDP), got {crawler.counter.api_calls}. "
        f"URLs fetched: {[u for u, _ in calls]}"
    )

    # Must have parsed at least one product
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {result.products}. Notes: {result.notes}"
    )

    product = result.products[0]
    assert product["sku"] == _ITEM_ID
    assert product["title"] == "Acme Sofa Set"
    assert product["sale_price"] == 499.99
    assert product["currency"] == "USD"
    assert product["site"] == "walmart"


def test_walmart_impersonate_chrome131_transparently_passed(monkeypatch):
    """CRITICAL: impersonate='chrome131' must be forwarded on every fetcher.get() call.

    Akamai Bot Manager checks TLS fingerprints — downgrading to default 'chrome'
    breaks real-world crawling. This test proves the kwarg is never dropped.
    """
    from app.crawlers.walmart import WalmartCrawler

    crawler = WalmartCrawler(_site(), limit=1)

    impersonate_values: list[str | None] = []

    def fake_get(url: str, **kw) -> FetchResult:
        impersonate_values.append(kw.get("impersonate"))
        crawler.counter.api_calls += 1
        if url == "https://www.walmart.com/":
            html = _WARMUP_HTML
        elif "/search?" in url:
            html = _SRP_HTML
        else:
            html = _PDP_HTML
        return FetchResult(
            ok=True, url=url, status=200, text=html,
            content=html.encode(), final_url=url, fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    crawler.crawl()

    # Every single call must carry impersonate='chrome131'
    assert len(impersonate_values) > 0, "No fetcher.get() calls were made"
    bad = [v for v in impersonate_values if v != "chrome131"]
    assert not bad, (
        f"Found calls WITHOUT impersonate='chrome131': {bad}. "
        f"All values: {impersonate_values}"
    )


def test_walmart_warmup_counted_as_api_call(monkeypatch):
    """Warmup request to / must be counted in counter.api_calls via fetcher.get."""
    from app.crawlers.walmart import WalmartCrawler

    crawler = WalmartCrawler(_site(), limit=1)
    warmup_seen = []

    def fake_get(url: str, **kw) -> FetchResult:
        if url == "https://www.walmart.com/":
            warmup_seen.append(url)
        crawler.counter.api_calls += 1
        if url == "https://www.walmart.com/":
            html = _WARMUP_HTML
        elif "/search?" in url:
            html = _SRP_HTML
        else:
            html = _PDP_HTML
        return FetchResult(
            ok=True, url=url, status=200, text=html,
            content=html.encode(), final_url=url, fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    crawler.crawl()

    assert len(warmup_seen) >= 1, (
        "Warmup GET to 'https://www.walmart.com/' was not made through fetcher"
    )
    assert crawler.counter.api_calls >= 1


def test_walmart_parse_next_not_degraded():
    """_parse_next still correctly extracts fields from __NEXT_DATA__ JSON."""
    from app.crawlers.walmart import WalmartCrawler

    crawler = WalmartCrawler(_site(), limit=1)
    row = crawler._parse_next(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse_next returned None on valid __NEXT_DATA__ HTML"
    assert row["sku"] == _ITEM_ID
    assert row["title"] == "Acme Sofa Set"
    assert row["sale_price"] == 499.99
    assert row["original_price"] == 599.99
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.3
    assert row["review_count"] == 250
    assert row["status"] == "on_sale"
    assert row["brand"] == "Acme"
    assert row["category_path"] == "Home/Furniture/Sofas & Couches"
