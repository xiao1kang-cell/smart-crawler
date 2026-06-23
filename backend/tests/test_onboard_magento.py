"""TDD test: verify magento crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Key cases:
  - sitemap discovery via robots.txt (text response)
  - sitemap .xml.gz (gzip content → res.content must be used)
  - product page with JSON-LD → product parsed out
  - counter.api_calls accumulated across all fetches
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture HTML / XML helpers
# ---------------------------------------------------------------------------

_PRODUCT_URL = "https://www.example.com/products/widget-pro.html"
_SITEMAP_URL = "https://www.example.com/sitemap.xml"
_SITEMAP_GZ_URL = "https://www.example.com/sitemap.xml.gz"

_ROBOTS_TXT = f"User-agent: *\nDisallow: /private\nSitemap: {_SITEMAP_URL}\n"

# Sitemap XML listing a single product URL
_SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_PRODUCT_URL}</loc></url>
</urlset>
"""

# Gzip-compressed sitemap for the .gz variant test
_SITEMAP_XML_GZ = gzip.compress(_SITEMAP_XML.encode("utf-8"))

_PRODUCT_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Widget Pro",
    "description": "A high-quality widget.",
    "image": ["https://www.example.com/images/widget-pro.jpg"],
    "brand": {"@type": "Brand", "name": "WidgetCo"},
    "offers": {
        "@type": "Offer",
        "price": "49.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "sku": "WP-001",
}

_PRODUCT_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD)
    + "</script>"
    "</head><body><h1>Widget Pro</h1></body></html>"
)

_PRODUCT_JSONLD_2 = {
    **_PRODUCT_JSONLD,
    "name": "Widget Mini",
    "sku": "WM-002",
    "offers": {
        "@type": "Offer",
        "price": "29.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
}
_PRODUCT_HTML_2 = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD_2)
    + "</script>"
    "</head><body><h1>Widget Mini</h1></body></html>"
)
_CATEGORY_HTML = (
    "<html><head><title>Category</title></head>"
    "<body><h1>Category</h1></body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "example_magento"
    s.url = "https://www.example.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "magento"
    s.brand = "WidgetCo"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict[str, FetchResult]):
    """Return a fake fetcher whose .get() looks up url_map and increments counter."""

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            # Match by exact URL or prefix
            if url in url_map:
                return url_map[url]
            # Default: 404
            return FetchResult(ok=False, url=url, status=404,
                               text="", content=b"", final_url=url, fetcher="curl_cffi")

    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_magento_routes_through_make_fetcher_plain_sitemap(monkeypatch):
    """Sitemap via text/XML: counter increments and product is parsed."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    # Provide sitemap_hint to skip _discover_sitemap (simplifies fixture)
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    # Manually init since __init__ calls get_sites() which needs DB
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML,
            content=_SITEMAP_XML.encode("utf-8"),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML,
            content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    calls_before = crawler.counter.api_calls
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert crawler.counter.api_calls > calls_before, (
        f"api_calls did not increase (still {crawler.counter.api_calls})"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {result.products}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["title"] == "Widget Pro"
    assert p["sku"] == "WP-001"
    assert p["sale_price"] == 49.99
    assert p["site"] == "example_magento"


def test_magento_gzip_sitemap_uses_res_content(monkeypatch):
    """Gzip sitemap: _sitemap_locs must use res.content (not res.text) to decompress."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_GZ_URL   # .gz url triggers gzip path
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_GZ_URL: FetchResult(
            ok=True, url=_SITEMAP_GZ_URL, status=200,
            text="",                          # text is empty / garbage for gzip
            content=_SITEMAP_XML_GZ,          # real gzip bytes in .content
            final_url=_SITEMAP_GZ_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML,
            content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    # Gzip decompression must have worked → product found
    assert len(result.products) >= 1, (
        f"Gzip sitemap not decompressed correctly. Notes: {result.notes}"
    )
    assert result.products[0]["title"] == "Widget Pro"


def test_magento_expands_all_sitemap_index_children(monkeypatch):
    """Sitemap index expansion must not stop at the first 12 child sitemaps."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 20
    crawler.scan_cap = 100

    child_urls = [f"https://www.example.com/sitemap-{i}.xml" for i in range(1, 14)]
    index_xml = "<sitemapindex>" + "".join(
        f"<sitemap><loc>{url}</loc></sitemap>" for url in child_urls
    ) + "</sitemapindex>"
    product_13 = "https://www.example.com/products/from-child-13.html"

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=index_xml, content=index_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        product_13: FetchResult(
            ok=True, url=product_13, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=product_13, fetcher="curl_cffi",
        ),
    }
    for child in child_urls[:-1]:
        url_map[child] = FetchResult(
            ok=True, url=child, status=200,
            text="<urlset></urlset>", content=b"<urlset></urlset>",
            final_url=child, fetcher="curl_cffi",
        )
    url_map[child_urls[-1]] = FetchResult(
        ok=True, url=child_urls[-1], status=200,
        text=f"<urlset><url><loc>{product_13}</loc></url></urlset>",
        content=f"<urlset><url><loc>{product_13}</loc></url></urlset>".encode(),
        final_url=child_urls[-1], fetcher="curl_cffi",
    )

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.products[0]["title"] == "Widget Pro"


def test_magento_counter_accumulates_across_sitemap_and_products(monkeypatch):
    """Smoke: counter.api_calls >= 2 (sitemap fetch + product fetch)."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML, content=_SITEMAP_XML.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (sitemap + product), got {crawler.counter.api_calls}"
    )


def test_magento_prioritizes_costway_product_urls():
    from app.crawlers.magento import _candidate_priority

    urls = [
        "https://www.costway.de/garten.html",
        "https://www.costway.de/garten/gartenmobel.html",
        "https://www.costway.de/costway-kunstpflanze-22-x-88-cm-grun.html",
    ]

    ordered = sorted(urls, key=_candidate_priority)

    assert ordered[0].endswith("costway-kunstpflanze-22-x-88-cm-grun.html")


def test_magento_costway_sitemap_only_rows(monkeypatch):
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    site.site = "costway_de"
    site.url = "https://www.costway.de"
    site.country = "DE"
    site.brand = "Costway"
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100
    crawler._sitemap_meta = {}

    product_url = "https://www.costway.de/costway-klappstuhl-rot.html"
    product_url_2 = "https://www.costway.de/costway-tisch-blau.html"
    sitemap_xml = f"""
    <urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
      <url>
        <loc>{product_url}</loc>
        <lastmod>2026-06-17T13:23:52+00:00</lastmod>
        <image:image>
          <image:loc>https://www.costway.de/media/chair.jpg</image:loc>
          <image:title>Klappstuhl Rot</image:title>
        </image:image>
      </url>
      <url>
        <loc>{product_url_2}</loc>
        <lastmod>2026-06-17T13:23:52+00:00</lastmod>
        <image:image>
          <image:loc>https://www.costway.de/media/table.jpg</image:loc>
          <image:title>Tisch Blau</image:title>
        </image:image>
      </url>
    </urlset>
    """

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "costway-klappstuhl-rot"
    assert row["title"] == "Klappstuhl Rot"
    assert row["image_urls"] == ["https://www.costway.de/media/chair.jpg"]
    assert row["currency"] == "EUR"
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_detail_parse"


def test_magento_total_counts_products_not_candidate_pages(monkeypatch):
    """Mixed sitemaps contain category URLs; total_product_count must count products."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 10
    crawler.scan_cap = 100

    category_url = "https://www.example.com/category/chairs.html"
    product_2 = "https://www.example.com/products/widget-mini.html"
    sitemap_xml = f"""
    <urlset>
      <url><loc>{_PRODUCT_URL}</loc></url>
      <url><loc>{category_url}</loc></url>
      <url><loc>{product_2}</loc></url>
    </urlset>
    """
    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
        product_2: FetchResult(
            ok=True, url=product_2, status=200,
            text=_PRODUCT_HTML_2, content=_PRODUCT_HTML_2.encode(),
            final_url=product_2, fetcher="curl_cffi",
        ),
        category_url: FetchResult(
            ok=True, url=category_url, status=200,
            text=_CATEGORY_HTML, content=_CATEGORY_HTML.encode(),
            final_url=category_url, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 2
    assert result.total_product_count == 2
    assert result.coverage_complete is True


def test_magento_limit_does_not_shrink_total_product_count(monkeypatch):
    """MAGENTO_LIMIT caps emitted rows, not the discovered product denominator."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    product_2 = "https://www.example.com/products/widget-mini.html"
    sitemap_xml = f"""
    <urlset>
      <url><loc>{_PRODUCT_URL}</loc></url>
      <url><loc>{product_2}</loc></url>
    </urlset>
    """
    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
        product_2: FetchResult(
            ok=True, url=product_2, status=200,
            text=_PRODUCT_HTML_2, content=_PRODUCT_HTML_2.encode(),
            final_url=product_2, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert "实际入库 1 个" in (result.coverage_reason or "")


def test_magento_robots_txt_discovery(monkeypatch):
    """Without sitemap_hint, crawler discovers sitemap from robots.txt."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = None   # force auto-discovery
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    robots_url = site.url.rstrip("/") + "/robots.txt"

    url_map = {
        robots_url: FetchResult(
            ok=True, url=robots_url, status=200,
            text=_ROBOTS_TXT, content=_ROBOTS_TXT.encode(),
            final_url=robots_url, fetcher="curl_cffi",
        ),
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML, content=_SITEMAP_XML.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) >= 1, (
        f"robots.txt discovery path failed. Notes: {result.notes}"
    )
