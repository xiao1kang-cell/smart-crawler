"""TDD test: wayfair crawler 批C 收编验证。

验证两段计数：
- curl 路径 sitemap + PDP GET → make_fetcher().get() → api_calls += 1 each
- stealth 路径 _fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则：
- curl_cffi 段：fetcher.get(url, headers=...) 替代 sess.get(url)，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw 参数/profile 逻辑不动
- success 标准：wayfair 原标准 — getattr(page, 'status', None) == 200 且有 html_content/body
"""
from __future__ import annotations

import re

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture HTML helpers
# ---------------------------------------------------------------------------

_SKU = "W003077221"
_PDP_URL = f"https://www.wayfair.com/furniture/pdp/some-chair-{_SKU.lower()}.html"

# Wayfair PDP HTML: must be > 50_000 chars to pass _is_blocked_body
_PDP_HTML = (
    "<html><head>"
    '<meta property="og:title" content="Some Chair | Wayfair"/>'
    '<meta property="og:description" content="A comfortable chair."/>'
    '<meta property="og:image" content="https://assets.wfcdn.com/images/chair.jpg"/>'
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
    '{"@type":"ListItem","position":1,"item":{"name":"Home"}},'
    '{"@type":"ListItem","position":2,"item":{"name":"Furniture"}},'
    '{"@type":"ListItem","position":3,"item":{"name":"Chairs"}}'
    "]}</script>"
    "</head><body>"
    f'<h1>Some Chair</h1>'
    f'<input name="sku" value="{_SKU}"/>'
    # Sale price
    '<span data-test-id="StandardPricingPrice-PRIMARY">$399.99</span>'
    # Original price
    '<span data-test-id="StandardPricingPrice-PREVIOUS"><s data-test-id="PriceDisplay">$478.79</s></span>'
    # Rating
    "<p>Rated 4.4 out of 5 stars.</p>"
    # Review count
    "<span>361 Reviews</span>"
    # Image
    '<img src="https://assets.wfcdn.com/images/chair.jpg"/>'
    # Pad to > 50_000 chars
    + " " * 52000
    + "</body></html>"
)

# Sitemap index with one PDP sub-sitemap
_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.wayfair.com/seo-pdp-sitemap~1.xml</loc></sitemap>"
    "</sitemapindex>"
)

# Sub-sitemap with one PDP URL
_SHARD_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    f"<url><loc>{_PDP_URL}</loc></url>"
    "</urlset>"
)

_HOME_URL = "https://www.wayfair.com/"


def _site() -> Site:
    s = Site()
    s.site = "wayfair"
    s.url = "https://www.wayfair.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "wayfair"
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

def test_wayfair_default_crawl_uses_sitemap_only(monkeypatch):
    """默认非住宅路径：warmup + sitemap_index + sub-sitemap，不打 PDP。"""
    from app.crawlers.wayfair import WayfairCrawler

    crawler = WayfairCrawler(_site())
    crawler.limit = 1

    url_map = {
        _HOME_URL: FetchResult(
            ok=True, url=_HOME_URL, status=200,
            text="<html></html>", content=b"<html></html>",
            final_url=_HOME_URL, fetcher="curl_cffi",
        ),
        "https://www.wayfair.com/seo-pdp-index.xml": FetchResult(
            ok=True, url="https://www.wayfair.com/seo-pdp-index.xml",
            status=200, text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(),
            final_url="https://www.wayfair.com/seo-pdp-index.xml",
            fetcher="curl_cffi",
        ),
        "https://www.wayfair.com/seo-pdp-sitemap~1.xml": FetchResult(
            ok=True, url="https://www.wayfair.com/seo-pdp-sitemap~1.xml",
            status=200, text=_SHARD_XML,
            content=_SHARD_XML.encode(),
            final_url="https://www.wayfair.com/seo-pdp-sitemap~1.xml",
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # warmup + sitemap_index + sub-sitemap = at least 3
    assert crawler.counter.api_calls >= 3, (
        f"Expected >=3 api_calls (warmup+sitemap_index+sub-sitemap), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert isinstance(result.products, list), "result.products 应为 list"
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _SKU
    assert "Some Chair" in p["title"]
    assert p["currency"] == "USD"
    assert p["site"] == "wayfair"


def test_wayfair_product_parse_not_degraded():
    """_parse_product 直接单元测试，确认 data-test-id 解析不退化。"""
    from app.crawlers.wayfair import WayfairCrawler

    crawler = WayfairCrawler(_site())
    row = crawler._parse_product(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse_product 在合法 PDP HTML 上不应返回 None"
    assert row["sku"] == _SKU
    assert row["title"] == "Some Chair"
    assert row["sale_price"] == 399.99
    assert row["original_price"] == 478.79
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.4
    assert row["review_count"] == 361
    assert row["status"] == "on_sale"
    assert row["product_url"] == _PDP_URL
    assert "Furniture" in (row["category_path"] or "")


# ---------------------------------------------------------------------------
# Test: stealth path — _fetch_via_stealth goes through count_browser_fetch
# ---------------------------------------------------------------------------

def test_wayfair_stealth_path_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。
    """
    from app.crawlers.wayfair import WayfairCrawler

    crawler = WayfairCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PDP_HTML
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

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PDP_HTML, "stealth html should be html_content from FakePage"


def test_wayfair_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """_fetch_via_stealth 失败时（status != 200），browser_opens 不增加。"""
    from app.crawlers.wayfair import WayfairCrawler

    crawler = WayfairCrawler(_site())

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

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None, "stealth should return None on non-200 status"
