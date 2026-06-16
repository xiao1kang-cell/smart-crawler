"""TDD test: houzz crawler 批C 收编验证。

验证两段计数：
- curl 路径：Wayback CDX + PDP GET → make_fetcher().get() → api_calls 累加
- stealth 路径：_fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1
  - stealth 成功标准：page.status in (200, 404) and (html_content or body)
  - stealth 失败（403/反爬页）→ browser_opens 不增加

批C 收编规则：
- curl_cffi 段：fetcher.get(url, headers=...) 替代 sess.get(url)，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw 参数/profile 逻辑不动
- success 标准：houzz 原标准 — page.status in (200, 404) and bool(html_content or body)
"""
from __future__ import annotations

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_VR_ID = "12345678"
_PDP_SLUG = "kallax-shelving-unit-white-prvw-vr~12345678"
_PDP_URL = f"https://www.houzz.com/products/{_PDP_SLUG}"

# Sunset page：HTTP 404 + 两个 sunset 特征
_SUNSET_TITLE = "Shop Houzz - No Longer Available"
_SUNSET_CSS = "marketplaceSunset"
_SUNSET_HTML = (
    f"<html><head><title>{_SUNSET_TITLE}</title>"
    f'<link rel="stylesheet" href="{_SUNSET_CSS}_v1.bundle.css">'
    "</head><body>"
    "<p>Purchasing on Shop Houzz, operated by Cart.com, is no longer available.</p>"
    "</body></html>"
)

# Wayback CDX 返回文本：包含一条有效 PDP URL
_CDX_TEXT = (
    f"com,houzz)/products/{_PDP_SLUG} 20240101120000 "
    f"https://www.houzz.com/products/{_PDP_SLUG} text/html 404 ABC123 1234567\n"
)

# Wayback CDX URL
_CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=houzz.com/products/*&output=text&from=2023"
    f"&filter=urlkey:.*prvw-vr.*&collapse=urlkey&limit=2600"
)


def _site() -> Site:
    s = Site()
    s.site = "houzz_us"
    s.url = "https://www.houzz.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "houzz"
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
            # 规范化 CDX URL（去掉 limit 参数变化）
            if "web.archive.org/cdx" in url:
                return url_map.get("__cdx__", FetchResult(
                    ok=False, url=url, status=200,
                    text="", content=b"", final_url=url, fetcher="curl_cffi",
                ))
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )
    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Test: curl 路径 — CDX + PDP GET 计 api_calls
# ---------------------------------------------------------------------------

def test_houzz_curl_path_counts_api(monkeypatch):
    """curl 路径：CDX(1) + PDP(1) = api_calls >= 2，且解析到 sunset 商品。"""
    from app.crawlers.houzz import HouzzCrawler

    crawler = HouzzCrawler(_site(), limit=1)

    url_map = {
        "__cdx__": FetchResult(
            ok=True,
            url="https://web.archive.org/cdx/search/cdx",
            status=200,
            text=_CDX_TEXT,
            content=_CDX_TEXT.encode(),
            final_url="https://web.archive.org/cdx/search/cdx",
            fetcher="curl_cffi",
        ),
        _PDP_URL: FetchResult(
            ok=True,
            url=_PDP_URL,
            status=404,   # sunset 页返回 404 + body
            text=_SUNSET_HTML,
            content=_SUNSET_HTML.encode(),
            final_url=_PDP_URL,
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (CDX+PDP), got {crawler.counter.api_calls}. "
        f"Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == _VR_ID, f"Expected sku={_VR_ID}, got {p['sku']}"
    assert p["status"] == "discontinued", f"Expected status=discontinued, got {p['status']}"
    assert p["site"] == "houzz_us"
    assert p["currency"] == "USD"


def test_houzz_product_parse_not_degraded():
    """_parse_pdp 直接单元测试：sunset 页正确解析 sku/title/status。"""
    from app.crawlers.houzz import HouzzCrawler

    crawler = HouzzCrawler(_site())
    row = crawler._parse_pdp(_SUNSET_HTML, _PDP_URL, 404)

    assert row is not None, "_parse_pdp 在 sunset HTML 上不应返回 None"
    assert row["sku"] == _VR_ID
    assert row["status"] == "discontinued"
    assert row["sale_price"] is None
    assert row["product_url"] == _PDP_URL
    assert row["site"] == "houzz_us"
    assert row["currency"] == "USD"
    # title 从 slug 解析：去掉 -prvw-vr~id 后缀后应含 slug 词汇
    assert row["title"], "title 不应为空"


# ---------------------------------------------------------------------------
# Test: stealth 路径 — 成功时 browser_opens += 1
# ---------------------------------------------------------------------------

def test_houzz_stealth_path_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 经 count_browser_fetch 包裹后，成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。houzz stealth 成功标准：status in (200, 404) and html_content/body。
    """
    from app.crawlers.houzz import HouzzCrawler

    crawler = HouzzCrawler(_site())

    # Fake page: status=200, has html_content — stealth 成功
    class _FakePage:
        status = 200
        html_content = _SUNSET_HTML
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePage()

    import sys
    fake_scrapling = type(sys)("scrapling")
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _SUNSET_HTML, "stealth html should be html_content from FakePage"


def test_houzz_stealth_sunset_page_counts_browser_opens(monkeypatch):
    """stealth 返回 status=404 + body（sunset 页）也应计数（houzz 原标准）。"""
    from app.crawlers.houzz import HouzzCrawler

    crawler = HouzzCrawler(_site())

    class _FakePageSunset:
        status = 404   # sunset 页是 HTTP 404
        html_content = _SUNSET_HTML
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageSunset()

    import sys
    fake_scrapling = type(sys)("scrapling")
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 for sunset 404+body, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _SUNSET_HTML


def test_houzz_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """stealth 返回 403/反爬页时，browser_opens 不增加。"""
    from app.crawlers.houzz import HouzzCrawler

    crawler = HouzzCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageBlocked()

    import sys
    fake_scrapling = type(sys)("scrapling")
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None, "stealth should return None on non-(200|404) status or no body"
