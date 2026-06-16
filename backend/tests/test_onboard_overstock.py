"""TDD test: overstock crawler 批C 收编验证。

验证三项核心：
1. curl 路径（sitemap_index + sub sitemap GET）→ make_fetcher().get() → api_calls 计数
2. 解析逻辑：_parse_sitemap_entry 正确提取 sku/title/category_path/images/product_url
3. 假200挑战页：sitemap 返回 HTTP 200 但 body 是挑战页 → _blocked 识别，不生成产品

Overstock 无 stealth 主流程（_enrich_from_pdp 默认关，TRY_PDP_ENRICH=0），
因此 stealth 测试只测 _enrich_from_pdp 内 StealthyFetcher.fetch 通过
count_browser_fetch 包裹后计 browser_opens。
"""
from __future__ import annotations

import sys

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

_SKU = "23456789"
_SLUG = "Simple-Living-Mavis-Espresso-Writing-Desk"
_CATEGORY_SEG = "Home-Garden"
_PROD_URL = (
    f"https://www.overstock.com/{_CATEGORY_SEG}/{_SLUG}/{_SKU}/product.html"
)
_IMG_URL = "https://ak1.ostkcdn.com/images/products/is/images/direct/abc.jpg"
_LASTMOD = "2026-02-04T22:19:31Z"

# Minimal sitemap_index: one products sub-sitemap
_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://api.overstock.com/sitemaps/overstock-v3/us/products1.xml</loc></sitemap>"
    "</sitemapindex>"
)

# Minimal products sub-sitemap: one product entry with image
_SUB_SITEMAP_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset xmlns:i='http://www.google.com/schemas/sitemap-image/1.1'>"
    "<url>"
    f"<loc>{_PROD_URL}</loc>"
    f"<lastmod>{_LASTMOD}</lastmod>"
    f"<i:image><i:loc>{_IMG_URL}</i:loc></i:image>"
    "</url>"
    "</urlset>"
)

# A realistic Akamai sec-if-cpt JS challenge page body (HTTP 200, but challenge)
_CHALLENGE_BODY = (
    "<html><head><title>Access Denied</title></head>"
    "<body>"
    "<!-- Akamai sec-if-cpt challenge -->"
    "<script>window.ak_bmsc='...';document.cookie='bm_sz=...';</script>"
    "akam/11/pixel_<noscript><img src='akadns.net/...</noscript>"
    "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "overstock"
    s.url = "https://www.overstock.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "overstock"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls and returns
    preconfigured FetchResult values from url_map."""
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
# Test 1: curl 路径计 api_calls + 解析出产品
# ---------------------------------------------------------------------------

def test_overstock_curl_path_counts_api_and_parses(monkeypatch):
    """curl 路径：sitemap_index(1) + sub-sitemap(1) = api_calls >= 2，且解析出 1 个 SKU。"""
    from app.crawlers.overstock import OverstockCrawler

    crawler = OverstockCrawler(_site())
    crawler.limit = 5

    _SITEMAP_INDEX_URL = (
        "https://api.overstock.com/sitemaps/overstock-v3/us/sitemap.xml"
    )
    _SUB_URL = (
        "https://api.overstock.com/sitemaps/overstock-v3/us/products1.xml"
    )

    url_map = {
        _SITEMAP_INDEX_URL: FetchResult(
            ok=True, url=_SITEMAP_INDEX_URL, status=200,
            text=_SITEMAP_INDEX_XML, content=_SITEMAP_INDEX_XML.encode(),
            final_url=_SITEMAP_INDEX_URL, fetcher="curl_cffi",
        ),
        _SUB_URL: FetchResult(
            ok=True, url=_SUB_URL, status=200,
            text=_SUB_SITEMAP_XML, content=_SUB_SITEMAP_XML.encode(),
            final_url=_SUB_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (sitemap_index + sub-sitemap), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _SKU, f"Expected sku={_SKU}, got {p['sku']}"
    assert "Simple Living Mavis Espresso Writing Desk" in p["title"], (
        f"Title not decoded correctly: {p['title']}"
    )
    assert p["category_path"] is not None
    assert _IMG_URL in p["image_urls"], (
        f"Image URL missing: {p['image_urls']}"
    )
    assert p["product_url"] == _PROD_URL
    assert p["currency"] == "USD"
    assert p["site"] == "overstock"


# ---------------------------------------------------------------------------
# Test 2: _parse_sitemap_entry 解析正确性
# ---------------------------------------------------------------------------

def test_overstock_parse_sitemap_entry():
    """_parse_sitemap_entry 从 <url> 内文正确提取所有字段。"""
    from app.crawlers.overstock import OverstockCrawler

    crawler = OverstockCrawler(_site())

    # Simulate a <url>...</url> block inner text
    block = (
        f"<loc>{_PROD_URL}</loc>"
        f"<lastmod>{_LASTMOD}</lastmod>"
        f"<i:image><i:loc>{_IMG_URL}</i:loc></i:image>"
    )

    row = crawler._parse_sitemap_entry(block)

    assert row is not None, "_parse_sitemap_entry should return a dict on valid block"
    assert row["sku"] == _SKU
    assert row["spu"] == _SKU
    assert "Simple Living" in row["title"]
    assert row["category_path"] is not None
    assert _IMG_URL in row["image_urls"]
    assert row["product_url"] == _PROD_URL
    assert row["currency"] == "USD"
    assert row["status"] == "on_sale"
    assert row["description"] is None     # PDP 才有，sitemap 留空
    assert row["sale_price"] is None      # 需 PDP
    assert row["published_at"] is not None


# ---------------------------------------------------------------------------
# Test 3: 假200挑战页被 _blocked 识别，不生成产品
# ---------------------------------------------------------------------------

def test_overstock_fake200_challenge_page_is_blocked(monkeypatch):
    """sitemap 返回 HTTP 200 但 body 是 Akamai 挑战页 → _blocked 识别，跳过该 sitemap。

    Overstock 特殊案例：sitemap_index 本身可 200，但子 sitemap 可能返回假 200 挑战页。
    收编后的 crawler 必须保留这个判断逻辑。
    """
    from app.crawlers.overstock import OverstockCrawler

    crawler = OverstockCrawler(_site())
    crawler.limit = 5

    _SITEMAP_INDEX_URL = (
        "https://api.overstock.com/sitemaps/overstock-v3/us/sitemap.xml"
    )
    _SUB_URL = (
        "https://api.overstock.com/sitemaps/overstock-v3/us/products1.xml"
    )

    # sitemap_index returns real XML, sub-sitemap returns HTTP 200 but challenge body
    url_map = {
        _SITEMAP_INDEX_URL: FetchResult(
            ok=True, url=_SITEMAP_INDEX_URL, status=200,
            text=_SITEMAP_INDEX_XML, content=_SITEMAP_INDEX_XML.encode(),
            final_url=_SITEMAP_INDEX_URL, fetcher="curl_cffi",
        ),
        _SUB_URL: FetchResult(
            # ok=True because HTTP 200, but body is challenge page
            ok=True, url=_SUB_URL, status=200,
            text=_CHALLENGE_BODY, content=_CHALLENGE_BODY.encode(),
            final_url=_SUB_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # No products should be parsed from challenge page
    assert len(result.products) == 0, (
        f"Expected 0 products when sub-sitemap body is a challenge page, "
        f"got {len(result.products)}. Notes: {result.notes}"
    )
    # api_calls should still be >= 2 (both requests were made)
    assert crawler.counter.api_calls >= 2, (
        f"Both sitemap requests should still be counted as api_calls, "
        f"got {crawler.counter.api_calls}"
    )


# ---------------------------------------------------------------------------
# Test 4: stealth 路径（_enrich_from_pdp）计 browser_opens
# ---------------------------------------------------------------------------

def test_overstock_stealth_pdp_counts_browser_opens(monkeypatch):
    """_enrich_from_pdp 内 StealthyFetcher.fetch 经 count_browser_fetch 包裹后
    成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch。
    """
    from app.crawlers.overstock import OverstockCrawler

    crawler = OverstockCrawler(_site())

    # Build a fake page with valid PDP content (status=200, html_content present)
    import json as _json

    _LD = {
        "@type": "Product",
        "name": "Mavis Writing Desk",
        "offers": {"price": "199.99", "availability": "https://schema.org/InStock"},
        "aggregateRating": {"ratingValue": "4.5", "reviewCount": "100"},
        "description": "A great desk.",
    }
    _PDP_CONTENT = (
        '<html><head>'
        '<script type="application/ld+json">' + _json.dumps(_LD) + '</script>'
        '</head><body>' + " " * 6000 + '</body></html>'
    )

    class _FakePage:
        status = 200
        html_content = _PDP_CONTENT
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePage()

    # Patch scrapling.fetchers inside the module
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    # Patch stealth_kwargs to avoid filesystem access
    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    monkeypatch.setattr(crawler, "sleep", lambda: None)

    rows = [{"product_url": _PROD_URL, "description": None,
             "sale_price": None, "original_price": None,
             "ratings": None, "review_count": None, "status": "on_sale"}]

    assert crawler.counter.browser_opens == 0

    ok_count = crawler._enrich_from_pdp(rows)

    assert ok_count >= 1, (
        f"Expected >=1 enriched rows, got {ok_count}"
    )
    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth PDP, "
        f"got {crawler.counter.browser_opens}"
    )


# ---------------------------------------------------------------------------
# Test 5: stealth 失败（status != 200）不计 browser_opens
# ---------------------------------------------------------------------------

def test_overstock_stealth_failure_does_not_count(monkeypatch):
    """_enrich_from_pdp stealth 失败（status=403 或 body 太小）不增加 browser_opens。"""
    from app.crawlers.overstock import OverstockCrawler

    crawler = OverstockCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageBlocked()

    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    monkeypatch.setattr(crawler, "sleep", lambda: None)

    rows = [{"product_url": _PROD_URL, "description": None,
             "sale_price": None, "original_price": None,
             "ratings": None, "review_count": None, "status": "on_sale"}]

    ok_count = crawler._enrich_from_pdp(rows)

    assert ok_count == 0, f"Expected 0 enriched on failure, got {ok_count}"
    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
