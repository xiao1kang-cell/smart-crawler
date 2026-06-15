"""TDD test: verify shoper crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: ShoperCrawler uses raw curl_cffi Session directly; make_fetcher
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
# Minimal fixture HTML helpers
# ---------------------------------------------------------------------------

# Homepage with a menu containing a category link (slug with hyphen -> product-looking)
# But for the category page we need it to yield product URLs with hyphens.
_CATEGORY_SLUG = "dom-i-ogrod"
_PRODUCT_SLUG = "skladana-drabina-aluminiowa"

# Shoper homepage: has a <ul class="menu"> with a category href
_HOME_HTML = (
    "<html><head></head><body>"
    '<ul class="menu">'
    f'<a href="/{_CATEGORY_SLUG}">Dom i Ogrod</a>'
    "</ul>"
    + " " * 5000
    + "</body></html>"
)

# Category page: contains a product href with hyphen slug
_CATEGORY_HTML = (
    "<html><head></head><body>"
    f'<a href="/{_PRODUCT_SLUG}">Skladana Drabina</a>'
    + " " * 5000
    + "</body></html>"
)

# Product page JSON-LD — Shoper merges multiple blocks; use a single block here
_PRODUCT_JSONLD = {
    "@context": "https://schema.org",
    "@type": "http://schema.org/Product",
    "@id": "https://example-shoper.com/product/1",
    "name": "3-stopniowa składana drabina aluminiowa",
    "sku": "SKU-DRAB-01",
    "description": "Lekka drabina aluminiowa z 3 stopniami.",
    "image": ["https://example-shoper.com/img/drabina.jpg"],
    "brand": {"@type": "Brand", "name": "Costway"},
    "offers": {
        "@type": "Offer",
        "price": "129.99",
        "priceCurrency": "PLN",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "42",
    },
}

_PRODUCT_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD)
    + "</script>"
    + "</head><body>"
    + " " * 5000
    + "</body></html>"
)

_BASE = "https://example-shoper.com"
_CATEGORY_URL = f"{_BASE}/{_CATEGORY_SLUG}"
_PRODUCT_URL = f"{_BASE}/{_PRODUCT_SLUG}"


def _site() -> Site:
    s = Site()
    s.site = "shoper"
    s.url = _BASE
    s.country = "PL"
    s.proxy_tier = "none"
    s.platform = "shoper"
    s.brand = "Costway"
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_shoper_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.shoper import ShoperCrawler

    crawler = ShoperCrawler(_site())
    crawler.limit = 1

    calls: list[str] = []

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1
        # homepage
        if url in (f"{_BASE}/", _BASE):
            html = _HOME_HTML
        # category page
        elif url == _CATEGORY_URL:
            html = _CATEGORY_HTML
        # product page
        elif url == _PRODUCT_URL:
            html = _PRODUCT_HTML
        else:
            html = ""
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

    # At least homepage + category + product page must have been fetched
    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls, got {crawler.counter.api_calls}. "
        f"URLs fetched: {calls}"
    )
    assert isinstance(result.products, list)
    assert len(result.products) >= 1, (
        f"Expected >=1 product parsed, got {result.products}. Notes: {result.notes}"
    )
    product = result.products[0]
    assert product["title"] == "3-stopniowa składana drabina aluminiowa"
    assert product["site"] == "shoper"


def test_shoper_counter_api_calls_minimum(monkeypatch):
    """Weaker smoke: at minimum one api_call is recorded (proves unified path)."""
    from app.crawlers.shoper import ShoperCrawler

    crawler = ShoperCrawler(_site())
    crawler.limit = 1

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if url in (f"{_BASE}/", _BASE):
            html = _HOME_HTML
        elif url == _CATEGORY_URL:
            html = _CATEGORY_HTML
        elif url == _PRODUCT_URL:
            html = _PRODUCT_HTML
        else:
            html = ""
        return FetchResult(
            ok=True, url=url, status=200, text=html,
            content=html.encode(), final_url=url, fetcher="curl_cffi",
        )

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    crawler.crawl()
    assert crawler.counter.api_calls >= 1
