"""TDD test: ikea crawler 批C 收编验证。

验证两段计数：
- curl 路径 sitemap + PDP GET → make_fetcher().get() → api_calls += 1 each
- stealth 路径 _fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则：
- curl_cffi 段：fetcher.get(url, headers=...) 替代 sess.get(url)，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw 参数/profile 逻辑不动
- success 标准：ikea 原标准 — getattr(page, 'status', None) == 200 且有 html_content/body
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture HTML helpers
# ---------------------------------------------------------------------------

_SKU = "10580070"
_PDP_URL = f"https://www.ikea.com/us/en/p/kallax-shelf-unit-{_SKU}/"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "KALLAX Shelf unit",
    "sku": _SKU,
    "mpn": _SKU,
    "description": "A versatile shelf unit.",
    "image": ["https://www.ikea.com/us/en/images/products/kallax.jpg"],
    "brand": {"@type": "Brand", "name": "IKEA"},
    "offers": {
        "@type": "Offer",
        "price": "69.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.7",
        "reviewCount": "12345",
    },
}

_JSONLD_BREADCRUMB = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Home"},
        {"@type": "ListItem", "position": 2, "name": "Storage"},
        {"@type": "ListItem", "position": 3, "name": "Shelves & shelving units"},
    ],
}

# PDP page: two JSON-LD blocks + enough padding to pass _is_blocked_body size check (>30KB)
_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_BREADCRUMB)
    + "</script>"
    + "</head><body>"
    + " " * 35000  # must be > 30000 chars to pass _is_blocked_body
    + "</body></html>"
)

# Minimal sitemap_index pointing at one US shard
_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.ikea.com/sitemaps/prod-en-US_1.xml</loc></sitemap>"
    "</sitemapindex>"
)

# Minimal US shard sitemap with one PDP entry
_SHARD_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>"
    f"<url>"
    f"<loc>{_PDP_URL}</loc>"
    f"<image:image><image:loc>https://www.ikea.com/us/en/images/products/kallax.jpg</image:loc></image:image>"
    f"</url>"
    "</urlset>"
    + " " * 35000  # pad to pass internal size checks
)

_SEARCH_API_PRODUCT = {
    "searchResultPage": {
        "products": {
            "main": {
                "items": [
                    {
                        "product": {
                            "name": "KALLAX",
                            "typeName": "Shelf unit",
                            "itemMeasureReferenceText": "30 3/8x57 7/8 \"",
                            "mainImageUrl": (
                                "https://www.ikea.com/us/en/images/products/"
                                "kallax.jpg"
                            ),
                            "pipUrl": _PDP_URL,
                            "filterClass": "shelving units",
                            "allProductImage": [
                                {
                                    "url": (
                                        "https://www.ikea.com/us/en/images/"
                                        "products/kallax.jpg"
                                    )
                                }
                            ],
                            "id": _SKU,
                            "itemNo": _SKU,
                            "onlineSellable": True,
                            "ratingValue": 4.7,
                            "ratingCount": 12345,
                            "salesPrice": {
                                "currencyCode": "USD",
                                "numeral": 69.99,
                            },
                            "businessStructure": {
                                "productRangeAreaName": "Storage furniture",
                                "homeFurnishingBusinessName": "Storage",
                                "productAreaName": "Shelving units",
                            },
                        }
                    }
                ]
            }
        }
    }
}


def _site() -> Site:
    s = Site()
    s.site = "ikea"
    s.url = "https://www.ikea.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "ikea"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls."""
    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )
    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Test: curl path — sitemap + PDP GET counts api_calls
# ---------------------------------------------------------------------------

def test_ikea_curl_path_counts_api(monkeypatch):
    """curl 路径：sitemap_index(1) + shard(1) + warmup(1) + PDP(1) = api_calls >= 4."""
    from app.crawlers.ikea import IkeaCrawler

    crawler = IkeaCrawler(_site())
    crawler.limit = 1
    crawler.use_search_api = False

    url_map = {
        "https://www.ikea.com/sitemaps/sitemap.xml": FetchResult(
            ok=True, url="https://www.ikea.com/sitemaps/sitemap.xml",
            status=200, text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(), final_url="https://www.ikea.com/sitemaps/sitemap.xml",
            fetcher="curl_cffi",
        ),
        "https://www.ikea.com/sitemaps/prod-en-US_1.xml": FetchResult(
            ok=True, url="https://www.ikea.com/sitemaps/prod-en-US_1.xml",
            status=200, text=_SHARD_XML,
            content=_SHARD_XML.encode(), final_url="https://www.ikea.com/sitemaps/prod-en-US_1.xml",
            fetcher="curl_cffi",
        ),
        _PDP_URL: FetchResult(
            ok=True, url=_PDP_URL, status=200,
            text=_PDP_HTML, content=_PDP_HTML.encode(), final_url=_PDP_URL,
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # sitemap_index + shard + warmup + PDP = 4 total
    assert crawler.counter.api_calls >= 4, (
        f"Expected >=4 api_calls (warmup+sitemap_index+shard+PDP), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert isinstance(result.products, list), "result.products 应为 list"
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _SKU
    assert "KALLAX" in p["title"]
    assert p["sale_price"] == 69.99
    assert p["currency"] == "USD"
    assert p["site"] == "ikea"


def test_ikea_product_parse_not_degraded():
    """_parse_product 直接单元测试，确认 JSON-LD 解析不退化。"""
    from app.crawlers.ikea import IkeaCrawler

    crawler = IkeaCrawler(_site())
    row = crawler._parse_product(_PDP_HTML, _PDP_URL, [])

    assert row is not None, "_parse_product 在合法 PDP HTML 上不应返回 None"
    assert row["sku"] == _SKU
    assert row["title"] == "KALLAX Shelf unit"
    assert row["sale_price"] == 69.99
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.7
    assert row["review_count"] == 12345
    assert row["status"] == "on_sale"
    assert row["brand"] == "IKEA"
    assert row["product_url"] == _PDP_URL
    assert "Storage" in (row["category_path"] or "")


def test_ikea_search_api_path_avoids_pdp_when_product_found(monkeypatch):
    from app.crawlers.ikea import IkeaCrawler

    crawler = IkeaCrawler(_site())
    crawler.limit = 1
    search_url = crawler._search_api_url(_SKU)
    calls: list[str] = []

    url_map = {
        "https://www.ikea.com/sitemaps/sitemap.xml": FetchResult(
            ok=True, url="https://www.ikea.com/sitemaps/sitemap.xml",
            status=200, text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(),
            final_url="https://www.ikea.com/sitemaps/sitemap.xml",
            fetcher="curl_cffi",
        ),
        "https://www.ikea.com/sitemaps/prod-en-US_1.xml": FetchResult(
            ok=True, url="https://www.ikea.com/sitemaps/prod-en-US_1.xml",
            status=200, text=_SHARD_XML,
            content=_SHARD_XML.encode(),
            final_url="https://www.ikea.com/sitemaps/prod-en-US_1.xml",
            fetcher="curl_cffi",
        ),
    }

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            calls.append(url)
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "api_delay", 0)

    def fake_search_api(_fetcher, entry, session=None):
        calls.append(search_url)
        product = _SEARCH_API_PRODUCT["searchResultPage"]["products"]["main"]["items"][0]["product"]
        return crawler._row_from_search_product(product, entry["url"], entry.get("images") or [])

    monkeypatch.setattr(crawler, "_fetch_via_search_api", fake_search_api)

    result = crawler.crawl()

    assert [p["sku"] for p in result.products] == [_SKU]
    assert result.products[0]["sale_price"] == 69.99
    assert result.products[0]["currency"] == "USD"
    assert result.products[0]["product_url"] == _PDP_URL
    assert search_url in calls
    assert _PDP_URL not in calls


# ---------------------------------------------------------------------------
# Test: stealth path — _fetch_via_stealth goes through count_browser_fetch
# ---------------------------------------------------------------------------

def test_ikea_stealth_path_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。
    """
    from app.crawlers.ikea import IkeaCrawler

    crawler = IkeaCrawler(_site())

    # Build a fake page object matching what StealthyFetcher returns
    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    def fake_stealth_fetch(url, **kw):
        return _FakePage()

    # Patch StealthyFetcher inside the ikea module's _fetch_via_stealth
    import importlib
    import sys

    # Create a minimal stub for scrapling.fetchers
    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return fake_stealth_fetch(url, **kw)

    # Patch the scrapling module so the import inside _fetch_via_stealth works
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    # Also patch stealth_kwargs to avoid filesystem side effects
    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    # Verify browser_opens starts at 0
    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PDP_HTML, "stealth html should be html_content from FakePage"


def test_ikea_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """_fetch_via_stealth 失败时（status != 200），browser_opens 不增加。"""
    from app.crawlers.ikea import IkeaCrawler

    crawler = IkeaCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    def fake_stealth_fetch_blocked(url, **kw):
        return _FakePageBlocked()

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return fake_stealth_fetch_blocked(url, **kw)

    import sys
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None, "stealth should return None on non-200 status"
