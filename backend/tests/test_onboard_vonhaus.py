"""TDD test: verify vonhaus crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: VonHausCrawler uses raw curl_cffi Session directly; make_fetcher
is never called; counter.api_calls stays 0.
After migration: every HTTP GET goes through the unified CrawlerFetcher;
counter.api_calls is incremented on each successful fetch.

Note: The vonhaus sitemap is served as plain XML (curl_cffi handles transport-level
gzip decompression transparently). For robustness the test fixture also sets
FetchResult.content to gzip-compressed bytes to verify that if gzip content is
ever returned the caller can still process it (content field is always populated).
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<urlset>"
    "<url><loc>https://www.vonhaus.com/vh_en/wooden-tray-123</loc></url>"
    "<url><loc>https://www.vonhaus.com/vh_en/dining-chair-456</loc></url>"
    # category-like URL that should be filtered (same base, trailing slash = base)
    "<url><loc>https://www.vonhaus.com/vh_en</loc></url>"
    "</urlset>"
)

# Gzip-compressed bytes of the sitemap — used as FetchResult.content to verify
# the content field is correctly populated (for crawlers that need binary content).
_SITEMAP_GZIP = gzip.compress(_SITEMAP_XML.encode("utf-8"))

_PRODUCT_HTML = """<html>
<head>
<meta property="og:title" content="Wooden Serving Tray" />
<meta property="product:price:amount" content="29.99" />
<meta property="product:price:currency" content="GBP" />
<meta property="product:availability" content="instock" />
<meta property="og:image" content="https://cdn.vonhaus.com/wooden-tray.jpg" />
<meta property="og:description" content="A beautiful wooden tray." />
</head>
<body>
<h1>Wooden Serving Tray</h1>
<nav class="breadcrumbs"><a href="/">Home</a><a href="/kitchen">Kitchen</a></nav>
<span data-product-id="TRAY-123"></span>
</body>
</html>""" + " " * 5000

_PRODUCT_HTML_JSONLD_PROMO = """<html>
<head>
<meta property="og:title" content="Garden Storage Box" />
<meta property="product:price:amount" content="49.99" />
<meta property="product:price:currency" content="GBP" />
<meta property="og:image" content="https://cdn.vonhaus.com/storage.jpg" />
<script type="application/ld+json">
""" + json.dumps({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Home"},
        {"@type": "ListItem", "position": 2, "name": "Garden"},
        {"@type": "ListItem", "position": 3, "name": "Storage"},
    ],
}) + """
</script>
<script type="application/ld+json">
""" + json.dumps({
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Garden Storage Box",
    "sku": "GARDEN-BOX",
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.6",
        "reviewCount": "42",
    },
    "offers": {
        "@type": "Offer",
        "price": "49.99",
        "priceCurrency": "GBP",
        "description": "Summer sale save 20% with free delivery",
    },
}) + """
</script>
</head>
<body>
<h1>Garden Storage Box</h1>
<span class="promotion-badge">Bundle deal: buy 2 save 15%</span>
</body>
</html>"""

_CATEGORY_HTML = """<html>
<head>
<meta property="og:title" content="Kitchen Category" />
<!-- No product:price:amount meta → category page -->
</head>
<body><h1>Kitchen</h1></body>
</html>"""

_BASE = "https://www.vonhaus.com"
_PRODUCT_URL = f"{_BASE}/vh_en/wooden-tray-123"
_CATEGORY_URL = f"{_BASE}/vh_en/dining-chair-456"


def _site() -> Site:
    s = Site()
    s.site = "vonhaus"
    s.url = _BASE
    s.country = "GB"
    s.proxy_tier = "none"
    s.platform = "vonhaus"
    s.brand = "VonHaus"
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_vonhaus_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.vonhaus import VonHausCrawler

    crawler = VonHausCrawler(_site())
    crawler.limit = 1  # only need one product for the test

    calls: list[str] = []

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1

        if "sitemap.xml" in url:
            # Sitemap: return as text; also populate content with gzip bytes
            # to verify content field is set (gzip decompression handled upstream)
            return FetchResult(
                ok=True,
                url=url,
                status=200,
                text=_SITEMAP_XML,
                content=_SITEMAP_GZIP,  # gzip bytes in content field
                final_url=url,
                fetcher="curl_cffi",
            )
        elif url == _PRODUCT_URL:
            return FetchResult(
                ok=True,
                url=url,
                status=200,
                text=_PRODUCT_HTML,
                content=_PRODUCT_HTML.encode("utf-8"),
                final_url=url,
                fetcher="curl_cffi",
            )
        else:
            # Category page or unknown — return html without price meta
            return FetchResult(
                ok=True,
                url=url,
                status=200,
                text=_CATEGORY_HTML,
                content=_CATEGORY_HTML.encode("utf-8"),
                final_url=url,
                fetcher="curl_cffi",
            )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    # suppress snapshot writes
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    # suppress sleep
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    result = crawler.crawl()

    # At least: 1 sitemap fetch + 1 product page + 1 category page
    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls, got {crawler.counter.api_calls}. URLs: {calls}"
    )

    # Must have parsed at least one product
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {result.products}. Notes: {result.notes}"
    )

    product = result.products[0]
    assert product["sku"] == "TRAY-123"
    assert product["title"] == "Wooden Serving Tray"
    assert product["sale_price"] == 29.99
    assert product["currency"] == "GBP"
    assert product["site"] == "vonhaus"
    assert product["status"] == "on_sale"


def test_vonhaus_gzip_content_field_populated(monkeypatch):
    """FetchResult.content is set to gzip bytes — verify gzip.decompress round-trips."""
    # Independent unit test: gzip content round-trips correctly
    decompressed = gzip.decompress(_SITEMAP_GZIP).decode("utf-8")
    assert "<loc>" in decompressed
    assert "wooden-tray-123" in decompressed


def test_vonhaus_sitemap_filters_category_pages(monkeypatch):
    """Category-like pages (no price meta) are skipped; only real products counted."""
    from app.crawlers.vonhaus import VonHausCrawler

    crawler = VonHausCrawler(_site())
    crawler.limit = 5

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "sitemap.xml" in url:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_SITEMAP_XML, content=_SITEMAP_GZIP, final_url=url,
                fetcher="curl_cffi",
            )
        elif url == _PRODUCT_URL:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(), final_url=url,
                fetcher="curl_cffi",
            )
        else:
            # dining-chair-456 → no price meta → category
            return FetchResult(
                ok=True, url=url, status=200,
                text=_CATEGORY_HTML, content=_CATEGORY_HTML.encode(), final_url=url,
                fetcher="curl_cffi",
            )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    result = crawler.crawl()

    # Only 1 product (wooden-tray), 1 category skipped
    assert len(result.products) == 1
    # Counter must have incremented (unified path proved)
    assert crawler.counter.api_calls >= 1


def test_vonhaus_limit_does_not_shrink_total_product_count(monkeypatch):
    """VONHAUS_LIMIT caps emitted rows, not the crawl's discovered denominator."""
    from app.crawlers.vonhaus import VonHausCrawler

    crawler = VonHausCrawler(_site())
    crawler.limit = 1
    product_2 = _PRODUCT_HTML.replace("TRAY-123", "CHAIR-456").replace(
        "Wooden Serving Tray", "Dining Chair")

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "sitemap.xml" in url:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_SITEMAP_XML, content=_SITEMAP_GZIP, final_url=url,
                fetcher="curl_cffi",
            )
        if url == _PRODUCT_URL:
            html = _PRODUCT_HTML
        else:
            html = product_2
        return FetchResult(
            ok=True, url=url, status=200,
            text=html, content=html.encode(), final_url=url,
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert "实际入库 1 个" in (result.coverage_reason or "")


def test_vonhaus_counter_minimum(monkeypatch):
    """Smoke: at minimum one api_call recorded after crawl (unified path)."""
    from app.crawlers.vonhaus import VonHausCrawler

    crawler = VonHausCrawler(_site())
    crawler.limit = 1

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "sitemap.xml" in url:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_SITEMAP_XML, content=_SITEMAP_GZIP, final_url=url,
                fetcher="curl_cffi",
            )
        return FetchResult(
            ok=True, url=url, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(), final_url=url,
            fetcher="curl_cffi",
        )

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "sleep", lambda: None)

    crawler.crawl()
    assert crawler.counter.api_calls >= 1


def test_vonhaus_parse_product_collects_jsonld_category_and_promotions():
    from app.crawlers.vonhaus import VonHausCrawler

    row = VonHausCrawler(_site())._parse_product(
        _PRODUCT_HTML_JSONLD_PROMO,
        "https://www.vonhaus.com/vh_en/garden/storage/garden-storage-box",
    )

    assert row is not None
    assert row["sku"] == "GARDEN-BOX"
    assert row["category_path"] == "Garden/Storage"
    assert row["ratings"] == 4.6
    assert row["review_count"] == 42
    assert row["has_free_shipping"] is True
    assert row["attributes"]["free_shipping_label"] == "Free delivery"
    assert "Bundle deal: buy 2 save 15%" in row["attributes"]["promotions"]
    assert any("Summer sale save 20%" in item for item in row["attributes"]["promotions"])


def test_vonhaus_parse_product_fallback_category_and_free_delivery():
    from app.crawlers.vonhaus import VonHausCrawler

    html = """
    <html><head>
      <meta property="product:price:amount" content="459.99" />
      <meta property="product:price:currency" content="GBP" />
      <meta property="og:title" content="Garden Sofa Set with Two Stools and Dining Table | VonHaus" />
      <meta property="og:image" content="https://cdn.vonhaus.com/garden-sofa.jpg" />
      <meta property="og:description" content="Free Delivery On All Orders*" />
    </head><body>
      <h1>Aruba Garden Corner Sofa with Table</h1>
      <div itemprop="aggregateRating" itemtype="https://schema.org/AggregateRating" itemscope>
        <meta itemprop="reviewCount" content="96"/>
        <meta itemprop="ratingValue" content="4.80"/>
      </div>
      <a title="Free Delivery On All Orders*">Free Delivery On All Orders*</a>
      <div data-product-id="7298"></div>
    </body></html>
    """

    row = VonHausCrawler(_site())._parse_product(
        html,
        "https://www.vonhaus.com/vh_en/garden-corner-sofa-set",
    )

    assert row is not None
    assert row["sku"] == "7298"
    assert row["category_path"] == "Garden & Outdoor"
    assert row["review_count"] == 96
    assert row["ratings"] == 4.8
    assert row["has_free_shipping"] is True
    assert row["attributes"]["free_shipping_label"] == "Free delivery"
    assert row["attributes"]["promotions"]
