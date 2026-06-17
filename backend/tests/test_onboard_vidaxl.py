"""TDD test: vidaxl crawler 批C 收编验证。

验证三路径计数：
- 路径1 API curl 路径: make_fetcher().get() → api_calls += 1 per page
- stealth 兜底路径 _fetch_via_stealth: count_browser_fetch 包裹 → browser_opens += 1
- stealth 失败不计 browser_opens

批C 收编规则（vidaxl 特殊）：
- API curl 段：make_fetcher(kind="api", source="vidaxl").get() 替代 sess.get()
  字段映射: res.status → resp.status_code / res.text → resp.text / res.json() → resp.json()
- storefront sitemap 段：make_fetcher(kind="sitemap", source="vidaxl").get() 替代 sess.get()
- stealth 兜底段：StealthyFetcher.fetch 用 count_browser_fetch 包裹；kw/profile 不动
- 多路径决策(API vs storefront) + proxy precheck 逻辑保留不动
- _try_fetch 中 proxy_pool 手动管理保留；成功时 self.counter.api_calls += 1
"""
from __future__ import annotations

import json
import sys

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_API_PRODUCT = {
    "sku": "VX123",
    "title": "Vidaxl Test Chair",
    "description": "A nice chair",
    "images": ["https://cdn.vidaxl.com/chair.jpg"],
    "category": "Furniture",
    "price": "99.99",
    "srp": "129.99",
    "currency": "EUR",
    "ean": "5059340100000",
    "stock": 10,
    "brand": "vidaXL",
    "url": "https://www.vidaxl.nl/e/vidaxl-chair/5059340100000.html",
}

_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.vidaxl.nl/sitemap-custom-product-1.xml</loc></sitemap>"
    "</sitemapindex>"
)

_SKU = "5059340100000"
_PDP_URL = "https://www.vidaxl.nl/e/vidaxl-chair/5059340100000.html"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "vidaXL Chair",
    "sku": _SKU,
    "mpn": _SKU,
    "description": "A comfortable chair",
    "image": ["https://cdn.vidaxl.com/chair.jpg"],
    "brand": {"@type": "Brand", "name": "vidaXL"},
    "offers": {
        "@type": "Offer",
        "price": "99.99",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.2",
        "reviewCount": "87",
    },
}

_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    + "</head><body>Product page content</body></html>"
)

_SITEMAP_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    f"<url><loc>{_PDP_URL}</loc></url>"
    "</urlset>"
)


def _site(country: str = "NL", proxy_tier: str = "none") -> Site:
    s = Site()
    s.site = "vidaxl_nl"
    s.url = "https://www.vidaxl.nl"
    s.country = country
    s.proxy_tier = proxy_tier
    s.platform = "vidaxl"
    s.brand = "vidaXL"
    return s


def _api_site() -> Site:
    s = Site()
    s.site = "vidaxl_api"
    s.url = "https://www.vidaxl.nl"
    s.country = "NL"
    s.proxy_tier = "none"
    s.platform = "vidaxl"
    s.brand = "vidaXL"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls and dispatches url_map."""
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
# Test 1: 路径1 API curl → make_fetcher().get() → api_calls per page
# ---------------------------------------------------------------------------

def test_vidaxl_api_path_counts_api_calls(monkeypatch):
    """路径1 官方 API：单页 → api_calls >= 1，解析出 product。"""
    monkeypatch.setenv("VIDAXL_API_EMAIL", "test@example.com")
    monkeypatch.setenv("VIDAXL_API_TOKEN", "testtoken")

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_api_site())

    api_response = FetchResult(
        ok=True,
        url="https://b2b.vidaxl.com/api_customer/products",
        status=200,
        text=json.dumps([_API_PRODUCT]),
        content=json.dumps([_API_PRODUCT]).encode(),
        final_url="https://b2b.vidaxl.com/api_customer/products",
        fetcher="curl_cffi",
    )
    # Override json() to return proper data
    api_response_data = [_API_PRODUCT]

    class _FakeAPIFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            res = FetchResult(
                ok=True,
                url=url,
                status=200,
                text=json.dumps(api_response_data),
                content=json.dumps(api_response_data).encode(),
                final_url=url,
                fetcher="curl_cffi",
            )
            return res

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _FakeAPIFetcher())
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 1, (
        f"Expected api_calls >= 1 for API path, got {crawler.counter.api_calls}. "
        f"Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >= 1 product, got {len(result.products)}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == "VX123"
    assert p["title"] == "Vidaxl Test Chair"
    assert p["currency"] == "EUR"
    assert p["site"] == "vidaxl_api"


def test_vidaxl_feed_path_reads_local_csv(monkeypatch, tmp_path):
    """无 API 凭据时，VIDAXL_US_FEED_URL 可直接作为 US fallback 数据源。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    feed = tmp_path / "vidaxl_us.csv"
    feed.write_text(
        "sku,title,price,srp,currency,stock,image_url,category,url\n"
        "US123,vidaXL US Patio Chair,49.99,69.99,USD,12,"
        "https://cdn.example.com/us123.jpg,Patio,"
        "https://www.vidaxl.com/e/us123.html\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDAXL_US_FEED_URL", str(feed))

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(Site(
        site="vidaxl_us",
        brand="Vidaxl",
        country="US",
        url="https://www.vidaxl.com/",
        platform="vidaxl",
        proxy_tier="residential",
    ))

    result = crawler.crawl()

    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "US123"
    assert row["title"] == "vidaXL US Patio Chair"
    assert row["currency"] == "USD"
    assert row["sale_price"] == 49.99
    assert row["original_price"] == 69.99
    assert row["inventory"] == 12
    assert row["site"] == "vidaxl_us"
    assert "官方 Feed" in " ".join(result.notes)


def test_vidaxl_feed_path_reads_site_crawler_config(monkeypatch, tmp_path):
    """站点 crawler_config.feed_url 可作为后台配置的 vidaXL feed 入口。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    monkeypatch.delenv("VIDAXL_US_FEED_URL", raising=False)
    monkeypatch.delenv("VIDAXL_FEED_URL", raising=False)
    feed = tmp_path / "vidaxl_us_config.csv"
    feed.write_text(
        "ean,name,price,currency,quantity,image,category\n"
        "CFG123,Configured Feed Chair,39.50,USD,7,"
        "https://cdn.example.com/cfg123.jpg,Outdoor\n",
        encoding="utf-8",
    )

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(Site(
        site="vidaxl_us",
        brand="Vidaxl",
        country="US",
        url="https://www.vidaxl.com/",
        platform="vidaxl",
        proxy_tier="residential",
        crawler_config={"feed_url": str(feed)},
    ))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.products[0]["sku"] == "CFG123"
    assert result.products[0]["inventory"] == 7
    assert result.products[0]["category_path"] == "Outdoor"


# ---------------------------------------------------------------------------
# Test 2: _map_api 直接单元测试（解析不退化）
# ---------------------------------------------------------------------------

def test_vidaxl_map_api_parse():
    """_map_api 直接解析 API product dict，确认字段不退化。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_api_site())
    row = crawler._map_api(_API_PRODUCT)

    assert row is not None
    assert row["sku"] == "VX123"
    assert row["title"] == "Vidaxl Test Chair"
    assert row["gtin"] == "5059340100000"
    assert row["inventory"] == 10
    assert row["status"] == "on_sale"
    assert row["site"] == "vidaxl_api"


# ---------------------------------------------------------------------------
# Test 3: _parse_jsonld 直接单元测试（解析不退化）
# ---------------------------------------------------------------------------

def test_vidaxl_parse_jsonld():
    """_parse_jsonld 对合法 Product JSON-LD 正确解析。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    row = crawler._parse_jsonld(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse_jsonld should not return None for valid Product JSON-LD"
    assert row["sku"] == _SKU
    assert row["title"] == "vidaXL Chair"
    assert row["sale_price"] == 99.99
    assert row["currency"] == "EUR"
    assert row["status"] == "on_sale"
    assert row["site"] == "vidaxl_nl"
    assert row["product_url"] == _PDP_URL


# ---------------------------------------------------------------------------
# Test 4: stealth 兜底 _fetch_via_stealth 成功 → browser_opens += 1
# ---------------------------------------------------------------------------

def test_vidaxl_stealth_success_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 成功(status=200)时，browser_opens 增 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    验证计数路径完整。
    """
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    def _fake_fetch(url, **kw):
        return _FakePage()

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _fake_fetch(url, **kw)

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
    assert html == _PDP_HTML


# ---------------------------------------------------------------------------
# Test 5: stealth 兜底失败(status=403) → browser_opens 不增
# ---------------------------------------------------------------------------

def test_vidaxl_stealth_failure_does_not_count(monkeypatch):
    """_fetch_via_stealth 失败(status=403)时，browser_opens 保持 0。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())

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

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None
