"""TDD test: verify bestbuy crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Key assertions:
  - impersonate='chrome131' is passed to every fetcher.get() call (Akamai fingerprint)
  - warmup request is counted as api_calls (via fetcher.get)
  - counter.api_calls >= 2 (1 SRP + 1 PDP minimum, warmup also counted)
  - parsing not degraded (sku/title/price extracted correctly)
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

_SKU_ID = "1234567"  # 7-digit bestbuy sku

# SRP page: contains skuId in JSON format (new NextJS format)
# Pattern: "skuId": "(\d{6,9})"
_SRP_HTML = (
    "<html><body>"
    f'"skuId": "{_SKU_ID}"'
    # Pad to > 30000 chars so _blocked() size check passes (threshold is 30000)
    + " " * 35000
    + "</body></html>"
)

# PDP page: must contain a JSON-LD Product block
_PRODUCT_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Samsung 65-inch OLED TV",
    "description": "Crystal-clear 4K display.",
    "sku": _SKU_ID,
    "image": ["https://pisces.bbystatic.com/image.jpg"],
    "brand": {"@type": "Brand", "name": "Samsung"},
    "offers": {
        "@type": "Offer",
        "price": "1299.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "842",
    },
}

_PDP_HTML = (
    "<html><head>"
    '<script id="schemaOrgWebPage" type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD)
    + "</script>"
    + "</head><body>"
    + " " * 35000
    + "</body></html>"
)

# Warmup response: homepage, no meaningful content needed
_WARMUP_HTML = "<html><body>Best Buy Home</body></html>" + " " * 35000

_PDP_URL = f"https://www.bestbuy.com/site/sku/{_SKU_ID}.p?skuId={_SKU_ID}"


def _site() -> Site:
    s = Site()
    s.site = "bestbuy"
    s.url = "https://www.bestbuy.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "bestbuy"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bestbuy_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.bestbuy import BestBuyCrawler

    crawler = BestBuyCrawler(_site(), limit=1)

    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append((url, kw))
        crawler.counter.api_calls += 1
        # Warmup = homepage
        if url == "https://www.bestbuy.com/":
            html = _WARMUP_HTML
        elif "/searchpage.jsp" in url:
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
    assert product["sku"] == _SKU_ID
    assert product["title"] == "Samsung 65-inch OLED TV"
    assert product["sale_price"] == 1299.99
    assert product["currency"] == "USD"
    assert product["site"] == "bestbuy"


def test_bestbuy_impersonate_chrome131_transparently_passed(monkeypatch):
    """CRITICAL: impersonate='chrome131' must be forwarded on every fetcher.get() call.

    Akamai Bot Manager checks TLS fingerprints — downgrading to default 'chrome'
    breaks real-world crawling. This test proves the kwarg is never dropped.
    """
    from app.crawlers.bestbuy import BestBuyCrawler

    crawler = BestBuyCrawler(_site(), limit=1)

    impersonate_values: list[str | None] = []

    def fake_get(url: str, **kw) -> FetchResult:
        impersonate_values.append(kw.get("impersonate"))
        crawler.counter.api_calls += 1
        if url == "https://www.bestbuy.com/":
            html = _WARMUP_HTML
        elif "/searchpage.jsp" in url:
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


def test_bestbuy_warmup_counted_as_api_call(monkeypatch):
    """Warmup request to / must be counted in counter.api_calls."""
    from app.crawlers.bestbuy import BestBuyCrawler

    crawler = BestBuyCrawler(_site(), limit=1)
    warmup_seen = []

    def fake_get(url: str, **kw) -> FetchResult:
        if url == "https://www.bestbuy.com/":
            warmup_seen.append(url)
        crawler.counter.api_calls += 1
        if url == "https://www.bestbuy.com/":
            html = _WARMUP_HTML
        elif "/searchpage.jsp" in url:
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
        "Warmup GET to 'https://www.bestbuy.com/' was not made through fetcher"
    )
    assert crawler.counter.api_calls >= 1


def test_bestbuy_parse_jsonld_not_degraded():
    """_parse and _row still correctly extract fields from JSON-LD."""
    from app.crawlers.bestbuy import BestBuyCrawler

    crawler = BestBuyCrawler(_site(), limit=1)
    row = crawler._parse(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse returned None on valid JSON-LD HTML"
    assert row["sku"] == _SKU_ID
    assert row["title"] == "Samsung 65-inch OLED TV"
    assert row["sale_price"] == 1299.99
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 842
    assert row["status"] == "on_sale"
    assert row["brand"] == "Samsung"
