"""TDD test: allegro crawler 批C 收编验证。

Allegro 特点：双阶段 BFS
  - 阶段1 curl 探针：首页及类目页 GET → make_fetcher().get() → api_calls += 1 each
  - 阶段2 stealth harvest：商品页 _fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则：
- curl 段：fetcher.get(url, headers=...) 替代 sess.get(url)，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 用 count_browser_fetch 包裹，kw 参数/profile 逻辑不动
- success 标准：allegro 原标准 — getattr(page, 'status', None) == 200 且 html 非空非 DataDome stub
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# 常量与 HTML 片段
# ---------------------------------------------------------------------------

_HOME = "https://allegro.pl/"
_PRODUCT_URL = "https://allegro.pl/oferta/sofa-narozna-lewa-prawa-123456789"
_PRODUCT_PATH = "/oferta/sofa-narozna-lewa-prawa-123456789"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Sofa narożna",
    "sku": "123456789",
    "description": "Wygodna sofa narożna.",
    "image": ["https://a.allegroimg.com/s720/123/sofa.jpg"],
    "offers": {
        "@type": "Offer",
        "price": "1299.00",
        "priceCurrency": "PLN",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "ratingCount": "88",
    },
}

# 有效商品页 HTML（> 10_000 chars，不含 DataDome 标识；需超过 _fetch 的 10_000 阈值）
_PRODUCT_HTML = (
    "<html><body>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    + " " * 12000  # pad beyond 10_000 to pass _fetch html size check
    + f'<a href="{_PRODUCT_PATH}">link</a>'
    + "</body></html>"
)

# 首页 HTML：包含一个 /oferta/ 链接作为种子（> 10_000 chars）
_HOME_HTML = (
    "<html><body>"
    + " " * 12000  # pad beyond 10_000 to pass _fetch html size check
    + f'<a href="{_PRODUCT_PATH}">sofa</a>'
    + "</body></html>"
)

# DataDome 被拦截页
_BLOCKED_HTML = (
    "<html><body>"
    "Please enable JS and disable any ad blocker"
    "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "allegro"
    s.url = "https://allegro.pl"
    s.country = "PL"
    s.proxy_tier = "none"
    s.platform = "allegro"
    s.brand = None
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory（curl 路径）
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls."""
    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=403,
                text=_BLOCKED_HTML, content=_BLOCKED_HTML.encode(),
                final_url=url, fetcher="curl_cffi",
            )
    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Test 1: curl 探针路径 — 首页 GET 计 api_calls
# ---------------------------------------------------------------------------

def test_allegro_curl_probe_counts_api(monkeypatch):
    """curl 探针路径：首页 GET + 商品页 GET 分别记入 api_calls。

    本测试让 curl 探针成功（返回 200 + 有效 HTML），stealth 不需要触发。
    """
    from app.crawlers.allegro import AllegroCrawler

    crawler = AllegroCrawler(_site())
    crawler.limit = 1

    url_map = {
        _HOME: FetchResult(
            ok=True, url=_HOME, status=200,
            text=_HOME_HTML, content=_HOME_HTML.encode(),
            final_url=_HOME, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # 首页(1) + 商品页(1) = 至少 2 次 api_calls
    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (home+product), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert isinstance(result.products, list)
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == "123456789"
    assert "Sofa" in p["title"]
    assert p["currency"] == "PLN"
    assert p["site"] == "allegro"


# ---------------------------------------------------------------------------
# Test 2: stealth harvest 路径 — _fetch_via_stealth 计 browser_opens
# ---------------------------------------------------------------------------

def test_allegro_stealth_path_counts_browser_opens(monkeypatch):
    """stealth harvest 路径：成功时 browser_opens += 1（不 mock count_browser_fetch）。"""
    from app.crawlers.allegro import AllegroCrawler

    crawler = AllegroCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PRODUCT_HTML
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

    html = crawler._fetch_via_stealth(_PRODUCT_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PRODUCT_HTML, "stealth html should be html_content from FakePage"


# ---------------------------------------------------------------------------
# Test 3: stealth 失败不计 browser_opens
# ---------------------------------------------------------------------------

def test_allegro_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """stealth 失败（status != 200）时 browser_opens 不增加。"""
    from app.crawlers.allegro import AllegroCrawler

    crawler = AllegroCrawler(_site())

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

    html = crawler._fetch_via_stealth(_PRODUCT_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None


# ---------------------------------------------------------------------------
# Test 4: 解析单元测试 — _parse_product 不退化
# ---------------------------------------------------------------------------

def test_allegro_parse_product_not_degraded():
    """_parse_product 在合法 JSON-LD HTML 上能正确解析字段。"""
    from app.crawlers.allegro import AllegroCrawler

    crawler = AllegroCrawler(_site())
    row = crawler._parse_product(_PRODUCT_HTML, _PRODUCT_URL)

    assert row is not None, "_parse_product 在合法 PDP HTML 上不应返回 None"
    assert row["sku"] == "123456789"
    assert "Sofa" in row["title"]
    assert row["sale_price"] == 1299.0
    assert row["currency"] == "PLN"
    assert row["status"] == "on_sale"
    assert row["product_url"] == _PRODUCT_URL
    assert row["site"] == "allegro"
