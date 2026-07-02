"""TDD test: westelm crawler 批C 收编验证。

验证两段计数：
- curl 路径：sitemap + PDP GET → make_fetcher().get() → api_calls += 1 each
- stealth 路径：_fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则：
- curl_cffi 段：make_fetcher(source="westelm").get() 替代 sess.get()，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw/profile 逻辑不动
- success 标准：westelm 原标准 — getattr(page, 'status', None) == 200 且有 html_content/body
- solve_cloudflare=False（Akamai 非 Cloudflare）原样保留
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture HTML helpers
# ---------------------------------------------------------------------------

_SPU = "andes-sofa-h12385"
_SKU_ID = "619421"
_PDP_URL = f"https://www.westelm.com/products/andes-sofa-h12385/"

# Minimal __INITIAL_STATE__ matching WestElmCrawler._parse_product expectations
_INITIAL_STATE = {
    "product": {
        "productDetails": {
            "groupId": "h12385",
            "title": "Andes Sofa",
            "isAvailable": True,
            "breadcrumbs": [
                {"label": "Home"},
                {"label": "Furniture"},
                {"label": "Sofas & Sectionals"},
            ],
            "copyBlocks": [
                {"id": "metadescription", "value": "A comfortable modern sofa."},
            ],
            "subsets": [
                {
                        "definitions": {
                            "skus": {
                                _SKU_ID: {
                                    "price": {
                                        "sellingPrice": 1499.0,
                                    "retailPrice": 1899.0,
                                    "regularPrice": 1899.0,
                                },
                                "inventory": {"availability": "IN_STOCK"},
                                "availability": {"available": True},
                                "name": "Sand",
                                    "properties": {"color": "Sand"},
                                    "flags": {"top": [{"id": "bestseller"}]},
                                },
                                "619422": {
                                    "price": {
                                        "sellingPrice": 1599.0,
                                        "retailPrice": 1999.0,
                                        "regularPrice": 1999.0,
                                    },
                                    "inventory": {"availability": "IN_STOCK"},
                                    "availability": {"available": True},
                                    "name": "Charcoal",
                                    "properties": {"color": "Charcoal"},
                                }
                            }
                        }
                    }
            ],
        }
    }
}

# Pad to > 200000 chars to pass _is_blocked_body check
_INITIAL_STATE_JSON = json.dumps(_INITIAL_STATE)
_PDP_HTML = (
    "<html><head></head><body>"
    f"<script>window.__INITIAL_STATE__ = {_INITIAL_STATE_JSON};</script>"
    + " " * (205000 - len(_INITIAL_STATE_JSON))
    + "</body></html>"
)

# Minimal sitemap_index pointing at one sub-sitemap
_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.westelm.com/netstorage/sitemaps/product-sitemap-1.xml.gz</loc></sitemap>"
    "</sitemapindex>"
)

# Minimal sub-sitemap (plain XML, not .gz — we'll test with non-gz for simplicity)
_SHARD_URL = "https://www.westelm.com/netstorage/sitemaps/product-sitemap-1.xml.gz"
_SHARD_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    f"<url><loc>{_PDP_URL}</loc></url>"
    "</urlset>"
)
# Gzip-compress the shard to simulate actual .gz response
_SHARD_GZ = gzip.compress(_SHARD_XML.encode("utf-8"))


def _site() -> Site:
    s = Site()
    s.site = "westelm"
    s.url = "https://www.westelm.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "westelm"
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

def test_westelm_curl_path_counts_api(monkeypatch):
    """curl 路径：warmup(1) + sitemap_index(1) + shard(1) + PDP(1) = api_calls >= 4。"""
    from app.crawlers.westelm import WestElmCrawler

    crawler = WestElmCrawler(_site())
    crawler.limit = 1

    url_map = {
        # warmup
        "https://www.westelm.com/": FetchResult(
            ok=True, url="https://www.westelm.com/",
            status=200, text="<html></html>",
            content=b"<html></html>", final_url="https://www.westelm.com/",
            fetcher="curl_cffi",
        ),
        # sitemap_index
        "https://www.westelm.com/netstorage/sitemaps/product-sitemap-index.xml": FetchResult(
            ok=True, url="https://www.westelm.com/netstorage/sitemaps/product-sitemap-index.xml",
            status=200, text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(), final_url="https://www.westelm.com/netstorage/sitemaps/product-sitemap-index.xml",
            fetcher="curl_cffi",
        ),
        # sub-sitemap (.gz) — returns gzip bytes
        _SHARD_URL: FetchResult(
            ok=True, url=_SHARD_URL,
            status=200, text=_SHARD_XML,
            content=_SHARD_GZ, final_url=_SHARD_URL,
            fetcher="curl_cffi",
        ),
        # PDP
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

    # warmup + sitemap_index + shard + PDP = 4 total
    assert crawler.counter.api_calls >= 4, (
        f"Expected >=4 api_calls (warmup+sitemap_index+shard+PDP), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert isinstance(result.products, list), "result.products 应为 list"
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _SKU_ID
    assert "Andes" in p["title"]
    assert p["sale_price"] == 1499.0
    assert p["site"] == "westelm"
    assert result.total_product_count >= len(result.products)


def test_westelm_product_parse_not_degraded():
    """_parse_product 直接单元测试，确认 __INITIAL_STATE__ 解析不退化。"""
    from app.crawlers.westelm import WestElmCrawler

    crawler = WestElmCrawler(_site())
    rows = crawler._parse_product(_PDP_HTML, _PDP_URL)

    assert rows, "_parse_product 在合法 PDP HTML 上不应返回空列表"
    p = rows[0]
    assert p["sku"] == _SKU_ID
    assert p["spu"] == "h12385"
    assert "Andes" in p["title"]
    assert p["sale_price"] == 1499.0
    assert p["original_price"] == 1899.0
    assert p["status"] == "on_sale"
    assert p["product_url"] == _PDP_URL
    assert p["site"] == "westelm"
    assert "Furniture" in (p["category_path"] or "")


def test_westelm_category_fallback_when_breadcrumb_missing():
    from app.crawlers.westelm import WestElmCrawler

    state = json.loads(json.dumps(_INITIAL_STATE))
    details = state["product"]["productDetails"]
    details["title"] = "Branch Desk Panels"
    details["breadcrumbs"] = []
    html = (
        "<html><body>"
        f"<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>"
        + " " * 205000
        + "</body></html>"
    )

    crawler = WestElmCrawler(_site())
    rows = crawler._parse_product(
        html,
        "https://www.westelm.com/products/branch-desk-panels-h11641/",
    )

    assert rows
    assert rows[0]["category_path"] == "Furniture/Office Furniture"


# ---------------------------------------------------------------------------
# Test: stealth path — _fetch_via_stealth goes through count_browser_fetch
# ---------------------------------------------------------------------------

def test_westelm_stealth_path_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。
    """
    from app.crawlers.westelm import WestElmCrawler

    crawler = WestElmCrawler(_site())

    # Build a fake page object matching what StealthyFetcher returns
    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    def fake_stealth_fetch(url, **kw):
        return _FakePage()

    import sys

    # Create a minimal stub for scrapling.fetchers
    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return fake_stealth_fetch(url, **kw)

    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    # Patch stealth_kwargs to avoid filesystem side effects
    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PDP_HTML, "stealth html should be html_content from FakePage"


def test_westelm_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """_fetch_via_stealth 失败时（status != 200），browser_opens 不增加。"""
    from app.crawlers.westelm import WestElmCrawler

    crawler = WestElmCrawler(_site())

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
