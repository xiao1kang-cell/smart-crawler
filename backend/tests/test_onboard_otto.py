"""TDD test: otto crawler 批C 收编验证。

验证三段计数：
- discovery curl 路径：_discover_urls 用 make_fetcher().get() → api_calls += 1 each
- warmup stealth 路径：_warm_profile 用 count_browser_fetch 包裹 StealthyFetcher.fetch
  → 成功时 browser_opens += 1
- PDP stealth 路径：_fetch_pdp 用 count_browser_fetch 包裹 StealthyFetcher.fetch
  → 成功时 browser_opens += 1（失败不计）

批C 收编规则：
- curl 段（discovery）：make_fetcher().get() 替代 creq.Session.get()
- stealth 段（warmup + PDP）：StealthyFetcher.fetch 用 count_browser_fetch 包裹
- Kasada warmup/profile/cookie 逻辑全部原样保留
- 删 proxy 自管(_session)；保留 guard / snapshot / sleep / 解析
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

_SKU = "A12345678"
_PDP_URL = f"https://www.otto.de/p/some-product-title/{_SKU}/"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Test Produkt",
    "sku": _SKU,
    "description": "Ein Testprodukt.",
    "image": ["https://www.otto.de/images/test.jpg"],
    "brand": {"@type": "Brand", "name": "TestBrand"},
    "offers": {
        "@type": "Offer",
        "price": "49.99",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "321",
    },
}

_JSONLD_BREADCRUMB = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Startseite"},
        {"@type": "ListItem", "position": 2, "name": "Mode"},
        {"@type": "ListItem", "position": 3, "name": "Damen"},
    ],
}

# PDP HTML must be > 50_000 chars to pass Kasada stub check (_fetch_pdp guard)
_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_BREADCRUMB)
    + "</script>"
    "</head><body>"
    + " " * 55000
    + "</body></html>"
)

# Minimal listing page with two /p/ product hrefs
_LISTING_HTML = (
    "<html><body>"
    f'<a href="/p/product-one/{_SKU}/">Prod One</a>'
    '<a href="/p/product-two/B98765432/">Prod Two</a>'
    "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "otto"
    s.url = "https://www.otto.de"
    s.country = "DE"
    s.proxy_tier = "none"
    s.platform = "otto"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_scrapling_module(monkeypatch, stealth_fetch_fn):
    """Inject a fake scrapling.fetchers module with custom fetch behavior."""
    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return stealth_fetch_fn(url, **kw)

    fake_scrapling = types.ModuleType("scrapling")
    fake_scrapling_fetchers = types.ModuleType("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    # Patch stealth_kwargs to avoid filesystem side effects
    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})


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
# Test 1: warmup stealth path → browser_opens counted
# ---------------------------------------------------------------------------

def test_otto_warmup_counts_browser_opens(monkeypatch):
    """_warm_profile 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。
    """
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())
    assert crawler.counter.browser_opens == 0

    class _FakePage:
        status = 200
        html_content = " " * 60000  # > 50_000 → warmup success check passes

    _fake_scrapling_module(monkeypatch, lambda url, **kw: _FakePage())

    kw = {"headless": True}
    result = crawler._warm_profile(kw)

    assert result is True, f"_warm_profile should succeed, got {result}"
    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful warmup, "
        f"got {crawler.counter.browser_opens}"
    )


def test_otto_warmup_failure_does_not_count(monkeypatch):
    """_warm_profile 失败（status != 200）时，browser_opens 不增加。"""
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())

    class _FakePageBlocked:
        status = 429
        html_content = "<html>KPSDK challenge</html>"

    _fake_scrapling_module(monkeypatch, lambda url, **kw: _FakePageBlocked())

    kw = {"headless": True}
    result = crawler._warm_profile(kw)

    assert result is False, f"_warm_profile should fail on 429, got {result}"
    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on warmup failure, "
        f"got {crawler.counter.browser_opens}"
    )


# ---------------------------------------------------------------------------
# Test 2: PDP stealth path → browser_opens counted
# ---------------------------------------------------------------------------

def test_otto_fetch_pdp_counts_browser_opens(monkeypatch):
    """_fetch_pdp 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。"""
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())
    assert crawler.counter.browser_opens == 0

    class _FakePdpPage:
        status = 200
        html_content = _PDP_HTML

    _fake_scrapling_module(monkeypatch, lambda url, **kw: _FakePdpPage())

    kw = {"headless": True}
    html = crawler._fetch_pdp(_PDP_URL, kw)

    assert html == _PDP_HTML, "should return html_content from page"
    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful PDP fetch, "
        f"got {crawler.counter.browser_opens}"
    )


def test_otto_fetch_pdp_failure_does_not_count(monkeypatch):
    """_fetch_pdp 失败（short Kasada stub）时，browser_opens 不增加。"""
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())

    class _FakeKasadaPage:
        status = 200
        html_content = "<html>KPSDK stub</html>"  # len < 50_000 → filtered out

    _fake_scrapling_module(monkeypatch, lambda url, **kw: _FakeKasadaPage())

    kw = {"headless": True}
    html = crawler._fetch_pdp(_PDP_URL, kw)

    assert html is None, "should return None for Kasada stub (too short)"
    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on Kasada stub, "
        f"got {crawler.counter.browser_opens}"
    )


# ---------------------------------------------------------------------------
# Test 3: discovery curl path → api_calls counted
# ---------------------------------------------------------------------------

def test_otto_discovery_counts_api_calls(monkeypatch):
    """_discover_urls 用 make_fetcher().get() → api_calls += 1 per seed."""
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())
    crawler.limit = 5

    listing_result = FetchResult(
        ok=True, url="https://www.otto.de/",
        status=200, text=_LISTING_HTML,
        content=_LISTING_HTML.encode(), final_url="https://www.otto.de/",
        fetcher="curl_cffi",
    )

    url_map = {
        "https://www.otto.de" + path: listing_result
        for path in [
            "/", "/mode/bekleidung/", "/mode/hosen/", "/mode/kleider/",
            "/mode/hemden/", "/mode/roecke/", "/mode/bodies/",
            "/mode/westen/", "/moebel/", "/moebel/sofas-couches/",
            "/moebel/betten/", "/moebel/tische/", "/garten/",
            "/baumarkt/", "/technik/multimedia/", "/sport/",
            "/damen/mode/", "/herren/mode/",
            "/sale/deal-des-tages/", "/sale/deals-der-woche/",
        ]
    }

    monkeypatch.setattr(
        crawler, "make_fetcher",
        lambda **kw: _make_fake_fetcher(crawler, url_map)
    )
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "guard", lambda status, where="": None)

    from app.crawlers.otto import CrawlResult
    result = CrawlResult()
    urls = crawler._discover_urls(result)

    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls from discovery, got {crawler.counter.api_calls}. "
        f"Notes: {result.notes}"
    )
    assert len(urls) >= 1, f"Expected >=1 URLs discovered, got {len(urls)}"


# ---------------------------------------------------------------------------
# Test 4: parse product (regression — no degradation)
# ---------------------------------------------------------------------------

def test_otto_parse_product_not_degraded():
    """_parse_product 直接单元测试，确认 JSON-LD 解析不退化。"""
    from app.crawlers.otto import OttoCrawler

    crawler = OttoCrawler(_site())
    row = crawler._parse_product(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse_product 在合法 PDP HTML 上不应返回 None"
    assert row["sku"] == _SKU
    assert row["title"] == "Test Produkt"
    assert row["sale_price"] == 49.99
    assert row["currency"] == "EUR"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 321
    assert row["status"] == "on_sale"
    assert row["brand"] == "TestBrand"
    assert row["product_url"] == _PDP_URL
    assert row["site"] == "otto"
    # breadcrumb: "Startseite" is filtered, "Mode" and "Damen" kept
    assert "Mode" in (row["category_path"] or "")


def test_otto_limit_uses_site_crawler_config():
    """后台站点配置可把 Otto 这种重型 crawler 限成小批量。"""
    from app.crawlers.otto import OttoCrawler

    site = _site()
    site.crawler_config = {"max_products": 25}

    crawler = OttoCrawler(site)

    assert crawler.limit == 25


def test_otto_runtime_caps_use_site_crawler_config():
    """后台配置可限制 Otto PDP 尝试数，避免挑战页拖垮 worker。"""
    from app.crawlers.otto import OttoCrawler

    site = _site()
    site.crawler_config = {
        "max_products": 25,
        "scan_cap": 500,
        "max_pdp_attempts": 12,
        "stealth_timeout_ms": 30000,
    }

    crawler = OttoCrawler(site)

    assert crawler.scan_cap == 500
    assert crawler.max_pdp_attempts == 12
    assert crawler.stealth_timeout_ms == 30000
