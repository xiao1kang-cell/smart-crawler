"""TDD test: idealo crawler 批C 收编验证。

验证两段计数：
- curl 路径 首页 + 商品 GET → make_fetcher().get() → api_calls += 1 each
- stealth 路径 _fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则：
- curl_cffi 段：fetcher.get(url, headers=...) 替代 sess.get(url)；
  res.status/res.text/res.content 对齐 FetchResult 字段
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，
  kw 参数/persist_profile/profile 目录逻辑不动
- success 标准：idealo 原标准 — getattr(page, 'status', None) == 200
  且有 html_content/body
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

_PID = "12345678"
_TAIL = "_-some-product-name.html"
_PROD_URL = f"https://www.idealo.de/preisvergleich/OffersOfProduct/{_PID}{_TAIL}"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Test Product Idealo",
    "sku": _PID,
    "description": "A test product from idealo.",
    "image": ["https://cdn.idealo.de/folder/Product/test.jpg"],
    "brand": {"@type": "Brand", "name": "TestBrand"},
    "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "29.99",
        "highPrice": "49.99",
        "offerCount": 5,
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "ratingCount": "321",
    },
}

_JSONLD_BREADCRUMB = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Startseite"},
        {"@type": "ListItem", "position": 2, "name": "Elektronik"},
        {"@type": "ListItem", "position": 3, "name": "Smartphones"},
    ],
}

# Product page HTML: two JSON-LD blocks + > 10KB padding (Akamai check needs >10KB)
_PROD_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_BREADCRUMB)
    + "</script>"
    + "</head><body>"
    + " " * 15000   # > 10_000 chars so Akamai check passes
    + "</body></html>"
)

# Home page HTML: contains at least one OffersOfProduct link seed
_HOME_HTML = (
    "<html><body>"
    f'<a href="/preisvergleich/OffersOfProduct/{_PID}{_TAIL}">Product Link</a>'
    + " " * 15000
    + "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "idealo"
    s.url = "https://www.idealo.de"
    s.country = "DE"
    s.proxy_tier = "none"
    s.platform = "idealo"
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
# Test: curl path — home seed + product GET counts api_calls + parse succeeds
# ---------------------------------------------------------------------------

def test_idealo_curl_path_counts_api(monkeypatch):
    """curl 路径：首页种子(1) + 商品页(1) = api_calls >= 2；解析出 product。"""
    from app.crawlers.idealo import IdealoCrawler, _HOME

    crawler = IdealoCrawler(_site())
    crawler.limit = 1

    url_map = {
        _HOME: FetchResult(
            ok=True, url=_HOME, status=200,
            text=_HOME_HTML, content=_HOME_HTML.encode(),
            final_url=_HOME, fetcher="curl_cffi",
        ),
        _PROD_URL: FetchResult(
            ok=True, url=_PROD_URL, status=200,
            text=_PROD_HTML, content=_PROD_HTML.encode(),
            final_url=_PROD_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (home+product), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _PID
    assert "Test Product Idealo" in p["title"]
    assert p["sale_price"] == 29.99      # lowPrice
    assert p["price_low"] == 29.99
    assert p["price_high"] == 49.99
    assert p["currency"] == "EUR"
    assert p["site"] == "idealo"


def test_idealo_product_parse_not_degraded():
    """_parse_product 直接单元测试，确认 JSON-LD 解析不退化。"""
    from app.crawlers.idealo import IdealoCrawler

    crawler = IdealoCrawler(_site())
    row = crawler._parse_product(_PROD_HTML, _PROD_URL)

    assert row is not None, "_parse_product 在合法 PDP HTML 上不应返回 None"
    assert row["sku"] == _PID
    assert row["title"] == "Test Product Idealo"
    assert row["sale_price"] == 29.99
    assert row["price_low"] == 29.99
    assert row["price_high"] == 49.99
    assert row["offer_count"] == 5
    assert row["currency"] == "EUR"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 321
    assert row["status"] == "on_sale"
    assert row["brand"] == "TestBrand"
    assert row["product_url"] == _PROD_URL
    assert "Elektronik" in (row["category_path"] or "")


# ---------------------------------------------------------------------------
# Test: stealth path — _fetch_via_stealth goes through count_browser_fetch
# ---------------------------------------------------------------------------

def test_idealo_stealth_path_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。
    """
    from app.crawlers.idealo import IdealoCrawler

    crawler = IdealoCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PROD_HTML
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePage()

    import sys
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PROD_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PROD_HTML, "stealth html should be html_content from FakePage"


def test_idealo_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """_fetch_via_stealth 失败时（status != 200），browser_opens 不增加。"""
    from app.crawlers.idealo import IdealoCrawler

    crawler = IdealoCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageBlocked()

    import sys
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    html = crawler._fetch_via_stealth(_PROD_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None, "stealth should return None on non-200 status"
