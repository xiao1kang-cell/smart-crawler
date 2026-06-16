"""TDD test: verify ebay crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: EbayCrawler uses raw curl_cffi Session directly; make_fetcher
is never called; counter.api_calls stays 0.
After migration: every HTTP GET goes through the unified CrawlerFetcher;
counter.api_calls is incremented on each successful fetch.

eBay-specific concerns:
  - warmup: one GET to homepage before PDP loop (counted separately)
  - gzip: sitemap .xml.gz content decompressed via res.content (bytes)
  - dual JSON-LD blocks: Product + BreadcrumbList in the same PDP HTML
  - impersonate=chrome131: preserved via headers, not dropped
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal fixture HTML helpers
# ---------------------------------------------------------------------------

_ITM_ID = "123456789012"

# SRP HTML: contains at least one /itm/<id> URL, padded to >50KB so
# _is_blocked_body() size check passes
_SRP_HTML = (
    "<html><body>"
    f'<a href="https://www.ebay.com/itm/{_ITM_ID}">Product</a>'
    + " " * 55000
    + "</body></html>"
)

# PDP HTML: two JSON-LD blocks — Product and BreadcrumbList
_PRODUCT_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Modern Sofa 3-Seater",
    "mpn": "SOFA-001",
    "description": "A comfortable modern sofa.",
    "image": [
        "https://i.ebayimg.com/images/g/sofa1.jpg",
        "https://i.ebayimg.com/images/g/sofa2.jpg",
    ],
    "brand": {"@type": "Brand", "name": "FurnitureCo"},
    "offers": {
        "@type": "Offer",
        "price": "299.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
        "priceSpecification": {
            "@type": "PriceSpecification",
            "price": "399.99",
        },
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "87",
    },
}

_BREADCRUMB_JSONLD = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {
            "@type": "ListItem",
            "position": 1,
            "name": "eBay",
            "item": {"@type": "Thing", "name": "eBay", "@id": "https://www.ebay.com"},
        },
        {
            "@type": "ListItem",
            "position": 2,
            "name": "Home & Garden",
            "item": {"@type": "Thing", "name": "Home & Garden"},
        },
        {
            "@type": "ListItem",
            "position": 3,
            "name": "Furniture",
            "item": {"@type": "Thing", "name": "Furniture"},
        },
        {
            "@type": "ListItem",
            "position": 4,
            "name": "Sofas & Armchairs",
            "item": {"@type": "Thing", "name": "Sofas & Armchairs"},
        },
    ],
}

# eBay uses unquoted type attribute: type=application/ld+json
_PDP_HTML = (
    "<html><head>"
    "<script type=application/ld+json>"
    + json.dumps(_PRODUCT_JSONLD)
    + "</script>"
    "<script type=application/ld+json>"
    + json.dumps(_BREADCRUMB_JSONLD)
    + "</script>"
    + "</head><body>"
    + " " * 55000
    + "</body></html>"
)

_PDP_URL = f"https://www.ebay.com/itm/{_ITM_ID}"

# Warmup URL: eBay homepage
_WARMUP_URL = "https://www.ebay.com/"
_WARMUP_HTML = "<html><body>eBay homepage</body></html>"


def _site() -> Site:
    s = Site()
    s.site = "ebay"
    s.url = "https://www.ebay.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "ebay"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ebay_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments.

    Verifies:
    - counter.api_calls increments (warmup + SRP + PDP ≥ 3)
    - at least one product parsed
    - product fields populated correctly
    """
    from app.crawlers.ebay import EbayCrawler

    crawler = EbayCrawler(_site(), limit=1)
    # Patch sleep to no-op so tests run in <1s instead of 5min
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    calls: list[str] = []

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1
        if url == _WARMUP_URL or url.endswith("/"):
            html = _WARMUP_HTML
            return FetchResult(
                ok=True, url=url, status=200,
                text=html, content=html.encode(), final_url=url, fetcher="curl_cffi",
            )
        if "/sch/" in url:
            html = _SRP_HTML
            return FetchResult(
                ok=True, url=url, status=200,
                text=html, content=html.encode(), final_url=url, fetcher="curl_cffi",
            )
        # PDP
        html = _PDP_HTML
        return FetchResult(
            ok=True, url=url, status=200,
            text=html, content=html.encode(), final_url=url, fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    # warmup + at least 1 SRP + 1 PDP = ≥ 3
    assert crawler.counter.api_calls >= 3, (
        f"Expected >=3 api_calls (warmup + SRP + PDP), got {crawler.counter.api_calls}. "
        f"URLs fetched: {calls}"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product parsed, got {result.products}. Notes: {result.notes}"
    )

    product = result.products[0]
    assert product["sku"] == "SOFA-001", f"sku mismatch: {product['sku']}"
    assert product["title"] == "Modern Sofa 3-Seater"
    assert product["sale_price"] == 299.99
    assert product["currency"] == "USD"
    assert product["site"] == "ebay"


def test_ebay_dual_jsonld_both_blocks_parsed(monkeypatch):
    """Product fields AND breadcrumb category_path must both be extracted.

    eBay PDP has two JSON-LD blocks:
      1. Product → name/price/brand/sku/images
      2. BreadcrumbList → category_path
    The migration must not break either block.
    """
    from app.crawlers.ebay import EbayCrawler

    crawler = EbayCrawler(_site(), limit=1)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if url == _WARMUP_URL or url.endswith("/"):
            html = _WARMUP_HTML
            return FetchResult(ok=True, url=url, status=200, text=html,
                               content=html.encode(), final_url=url)
        if "/sch/" in url:
            html = _SRP_HTML
            return FetchResult(ok=True, url=url, status=200, text=html,
                               content=html.encode(), final_url=url)
        html = _PDP_HTML
        return FetchResult(ok=True, url=url, status=200, text=html,
                           content=html.encode(), final_url=url)

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    result = crawler.crawl()

    assert len(result.products) >= 1, f"No products. Notes: {result.notes}"
    p = result.products[0]

    # --- Product block: commercial fields ---
    assert p["title"] == "Modern Sofa 3-Seater", f"title: {p['title']}"
    assert p["brand"] == "FurnitureCo", f"brand: {p['brand']}"
    assert p["sale_price"] == 299.99, f"sale_price: {p['sale_price']}"
    assert p["original_price"] == 399.99, f"original_price: {p['original_price']}"
    assert p["status"] == "on_sale", f"status: {p['status']}"
    assert len(p.get("image_urls", [])) >= 1, "image_urls missing"
    assert p.get("ratings") == 4.5, f"ratings: {p.get('ratings')}"
    assert p.get("review_count") == 87, f"review_count: {p.get('review_count')}"

    # --- BreadcrumbList block: category_path ---
    cp = p.get("category_path")
    assert cp is not None, "category_path is None — BreadcrumbList block not parsed"
    assert "Furniture" in cp or "Home" in cp, (
        f"category_path missing expected segments: {cp}"
    )
    # Should skip the eBay root node and include real categories
    assert "eBay" not in cp, f"category_path should not include 'eBay': {cp}"


def test_ebay_warmup_counts_as_api_call(monkeypatch):
    """Warmup GET to eBay homepage must be routed through fetcher (counted)."""
    from app.crawlers.ebay import EbayCrawler

    crawler = EbayCrawler(_site(), limit=1)
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    warmup_calls: list[str] = []

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if url == _WARMUP_URL or url.rstrip("/") == "https://www.ebay.com":
            warmup_calls.append(url)
        if "/sch/" in url:
            html = _SRP_HTML
        elif "/itm/" in url:
            html = _PDP_HTML
        else:
            html = _WARMUP_HTML
        return FetchResult(ok=True, url=url, status=200, text=html,
                           content=html.encode(), final_url=url)

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    crawler.crawl()

    assert crawler.counter.api_calls >= 1, "No api_calls recorded at all"
    assert len(warmup_calls) >= 1, (
        f"Warmup GET to homepage not routed through fetcher. "
        f"All calls: {warmup_calls}"
    )


def test_ebay_sitemap_gzip_uses_res_content(monkeypatch):
    """When USE_SITEMAP path is active, .xml.gz decompression must use res.content (bytes).

    Simulates the sitemap path: index.xml → one .xml.gz sub-sitemap → itm URLs.
    Verifies gzip.decompress(res.content) works (not res.text which is already decoded).
    """
    from app.crawlers.ebay import EbayCrawler
    import app.crawlers.ebay as ebay_mod

    # Enable sitemap path for this test
    monkeypatch.setattr(ebay_mod, "USE_SITEMAP", True)
    # Make SRP return nothing so sitemap kicks in
    monkeypatch.setattr(ebay_mod, "_HOME_CATEGORIES", [])

    crawler = EbayCrawler(_site(), limit=1)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    # Build a tiny sitemap index XML + one gzipped sub-sitemap
    sub_url = "https://www.ebay.com/lst/GTC-0_0.xml.gz"
    sitemap_index_xml = (
        '<?xml version="1.0"?><sitemapindex>'
        f"<sitemap><loc>{sub_url}</loc></sitemap>"
        "</sitemapindex>"
    )
    sub_xml = (
        '<?xml version="1.0"?><urlset>'
        f"<url><loc>https://www.ebay.com/itm/{_ITM_ID}</loc></url>"
        "</urlset>"
    )
    sub_gz = gzip.compress(sub_xml.encode("utf-8"))

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "VIS-0-index.xml" in url:
            txt = sitemap_index_xml
            return FetchResult(ok=True, url=url, status=200,
                               text=txt, content=txt.encode(), final_url=url)
        if url == sub_url:
            # content is gzip bytes; text is empty/garbage — only content matters
            return FetchResult(ok=True, url=url, status=200,
                               text="", content=sub_gz, final_url=url)
        if url == _WARMUP_URL or url.endswith("/"):
            html = _WARMUP_HTML
            return FetchResult(ok=True, url=url, status=200,
                               text=html, content=html.encode(), final_url=url)
        # PDP
        html = _PDP_HTML
        return FetchResult(ok=True, url=url, status=200,
                           text=html, content=html.encode(), final_url=url)

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    result = crawler.crawl()

    # Should have picked up the itm URL from the gzipped sitemap
    assert len(result.products) >= 1, (
        f"gzip sitemap path yielded no products. Notes: {result.notes}"
    )
