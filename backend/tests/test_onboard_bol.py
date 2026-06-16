"""TDD test: bol crawler 批C 收编验证。

验证三项核心：
1. curl 路径（sitemap_index + sub-sitemap GET）→ make_fetcher().get() → api_calls 计数 + 解析
2. PDP stealth 路径（_enrich_from_pdp，TRY_PDP_ENRICH=1）→ count_browser_fetch 包裹 → browser_opens 计数
3. stealth 失败（status != 200 或 body 太小）不计 browser_opens

批C 收编规则：
- curl_cffi 段：fetcher.get(url, headers=...) 替代 sess.get(url)，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw 参数/profile 逻辑不动
- TRY_PDP_ENRICH 开关（BOL_TRY_PDP 环境变量）保留
- success 标准：bol 原标准 — status == 200 且 len(html_content) >= 5000
"""
from __future__ import annotations

import json
import sys

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

_SKU = "9300000180270213"
_SLUG = "4x-zijden-kussensloop-maanlicht"
_LOCALE = "nl/nl"
_PROD_URL = f"https://www.bol.com/{_LOCALE}/p/{_SLUG}/{_SKU}/"
_LASTMOD = "2026-05-24T03:15:54.636741549+02:00"

# Minimal sitemap_index: one product sub-sitemap
_SITEMAP_INDEX_URL = "https://www.bol.com/sitemap/nl-nl/"
_SUB_SITEMAP_URL = "https://www.bol.com/sitemap/nl-nl/product-1"

_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    f"<sitemap><loc>{_SUB_SITEMAP_URL}</loc></sitemap>"
    "</sitemapindex>"
)

_SUB_SITEMAP_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    "<url>"
    f"<loc>{_PROD_URL}</loc>"
    f"<lastmod>{_LASTMOD}</lastmod>"
    "</url>"
    "</urlset>"
)

# Fake Akamai sec-if-cpt challenge body (HTTP 200 but challenge)
_CHALLENGE_BODY = (
    "<html><head><title>Access Denied</title></head>"
    "<body>"
    "<!-- Akamai sec-if-cpt challenge -->"
    "<script>window.ak_bmsc='...';document.cookie='bm_sz=...';</script>"
    "</body></html>"
)

# A rich PDP page with JSON-LD for _enrich_from_pdp
_LD_PRODUCT = {
    "@type": "Product",
    "name": "4x zijden kussensloop maanlicht",
    "offers": {
        "price": "29.99",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {"ratingValue": "4.6", "reviewCount": "88"},
    "description": "Luxe zijden kussensloop.",
    "image": ["https://media.bol.com/image/kussensloop.jpg"],
}
_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_LD_PRODUCT)
    + "</script>"
    + "</head><body>"
    + " " * 5500  # must be >= 5000 chars (bol success criterion: len(content) >= 5000)
    + "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "bol_nl"
    s.url = "https://www.bol.com"
    s.country = "NL"
    s.proxy_tier = "none"
    s.platform = "bol"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory (counts api_calls on every .get())
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
# Test 1: curl 路径计 api_calls + 解析出产品
# ---------------------------------------------------------------------------

def test_bol_curl_path_counts_api_and_parses(monkeypatch):
    """curl 路径：sitemap_index(1) + sub-sitemap(1) = api_calls >= 2，且解析出 1 个 SKU。"""
    from app.crawlers.bol import BolCrawler

    crawler = BolCrawler(_site())
    crawler.limit = 5

    url_map = {
        _SITEMAP_INDEX_URL: FetchResult(
            ok=True, url=_SITEMAP_INDEX_URL, status=200,
            text=_SITEMAP_INDEX_XML, content=_SITEMAP_INDEX_XML.encode(),
            final_url=_SITEMAP_INDEX_URL, fetcher="curl_cffi",
        ),
        _SUB_SITEMAP_URL: FetchResult(
            ok=True, url=_SUB_SITEMAP_URL, status=200,
            text=_SUB_SITEMAP_XML, content=_SUB_SITEMAP_XML.encode(),
            final_url=_SUB_SITEMAP_URL, fetcher="curl_cffi",
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
    assert "zijden kussensloop" in p["title"].lower(), (
        f"Title not decoded correctly: {p['title']}"
    )
    assert p["currency"] == "EUR"
    assert p["product_url"] == _PROD_URL
    assert p["site"] == "bol_nl"
    assert p["published_at"] is not None
    assert p["sale_price"] is None        # sitemap 无价格，PDP 才有


# ---------------------------------------------------------------------------
# Test 2: _parse_sitemap_entry 解析正确性
# ---------------------------------------------------------------------------

def test_bol_parse_sitemap_entry():
    """_parse_sitemap_entry 从 <url> 内文正确提取所有字段。"""
    from app.crawlers.bol import BolCrawler

    crawler = BolCrawler(_site())

    block = (
        f"<loc>{_PROD_URL}</loc>"
        f"<lastmod>{_LASTMOD}</lastmod>"
    )

    row = crawler._parse_sitemap_entry(block)

    assert row is not None, "_parse_sitemap_entry should return a dict on valid block"
    assert row["sku"] == _SKU
    assert row["spu"] == _SKU
    assert "zijden kussensloop" in row["title"].lower()
    assert row["currency"] == "EUR"
    assert row["status"] == "on_sale"
    assert row["product_url"] == _PROD_URL
    assert row["description"] is None        # PDP 才有
    assert row["sale_price"] is None         # 需 PDP
    assert row["image_urls"] == []           # bol sitemap 无 image 扩展
    assert row["published_at"] is not None


# ---------------------------------------------------------------------------
# Test 3: PDP stealth 路径（TRY_PDP_ENRICH=1）计 browser_opens
# ---------------------------------------------------------------------------

def test_bol_stealth_pdp_counts_browser_opens(monkeypatch):
    """_enrich_from_pdp 内 StealthyFetcher.fetch 经 count_browser_fetch 包裹后
    成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。设 TRY_PDP_ENRICH=True 启用。
    """
    import app.crawlers.bol as bol_mod

    from app.crawlers.bol import BolCrawler

    crawler = BolCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePage()

    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    monkeypatch.setattr(crawler, "sleep", lambda: None)

    rows = [{
        "product_url": _PROD_URL,
        "description": None,
        "sale_price": None,
        "original_price": None,
        "currency": "EUR",
        "status": "on_sale",
    }]

    assert crawler.counter.browser_opens == 0

    ok_count = crawler._enrich_from_pdp(rows)

    assert ok_count >= 1, f"Expected >=1 enriched rows, got {ok_count}"
    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth PDP, "
        f"got {crawler.counter.browser_opens}"
    )
    # 验证字段被丰富
    assert rows[0]["sale_price"] == 29.99
    assert rows[0]["currency"] == "EUR"


# ---------------------------------------------------------------------------
# Test 4: stealth 失败（status != 200）不计 browser_opens
# ---------------------------------------------------------------------------

def test_bol_stealth_failure_does_not_count(monkeypatch):
    """_enrich_from_pdp stealth 失败（status=403）不增加 browser_opens。"""
    from app.crawlers.bol import BolCrawler

    crawler = BolCrawler(_site())

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

    rows = [{
        "product_url": _PROD_URL,
        "description": None,
        "sale_price": None,
        "original_price": None,
        "currency": "EUR",
        "status": "on_sale",
    }]

    ok_count = crawler._enrich_from_pdp(rows)

    assert ok_count == 0, f"Expected 0 enriched on failure, got {ok_count}"
    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
