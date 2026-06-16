"""TDD test: aliexpress crawler 批C 收编验证。

验证两段计数：
- SRP curl 路径: make_fetcher().get() → api_calls += 1 each (含翻页)
- PDP stealth 路径: _fetch_via_stealth → count_browser_fetch 包裹 → browser_opens += 1

批C 收编规则（aliexpress 特殊）：
- curl 段：make_fetcher(kind=..., source="aliexpress").get(url, headers=...) 替代 sess.get()
  字段：res.status_code → res.status or 0 / resp.text → res.text
- stealth 段：StealthyFetcher.fetch 用 count_browser_fetch 包裹；kw/profile 不动
- success 标准：原 _blocked() 反面：
  * _blocked(html): 短 body(<20K) 且含 _BLOCK_MARKS → True(blocked)
  * _blocked(html): 短 body(<20K) 无 block marks → False(NOT blocked)
  * _blocked(html): 长 body(>=20K) → 永远 False(NOT blocked)
  → success = (status==200) and (not _blocked(html))
- 短 body 含 block mark → browser_opens 不增；长 body → 计数
- SRP 翻页(range(1, MAX_PAGES+1))保留
"""
from __future__ import annotations

import sys

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Constants mirrored from aliexpress.py
# ---------------------------------------------------------------------------

_BLOCK_MARKS = (
    "nc.x.alicdn.com",
    "_nc_token",
    "punish",
    "Access Denied",
    "captcha-delivery",
    "slidertest",
    "blocked",
    "behavior verification",
)

_BASE = "https://www.aliexpress.com"
_KW = "sofa cover"   # first keyword in _HOME_KW
_ITEM_ID = "12345678901"
_PDP_URL = f"{_BASE}/item/{_ITEM_ID}.html"
_SRP_URL = f"{_BASE}/w/wholesale-sofa-cover.html?page=1"

# A valid-looking SRP body: enough chars (>20K) and contains one item link
_SRP_HTML = (
    "<html><body>"
    + " " * 21000
    + f'<a href="/item/{_ITEM_ID}.html">Sofa Cover</a>'
    + "</body></html>"
)

# A valid-looking PDP body: enough chars (>20K) and contains window.runParams
import json as _json
_RUN_PARAMS_DATA = {
    "titleModule": {
        "subject": "Sofa Cover Blue",
        "feedbackRating": {"averageStar": "4.5", "totalValidNum": 200},
        "totalValidNum": 200,
    },
    "priceModule": {
        "formatedActivityPrice": "US $12.99",
        "formatedPrice": "US $15.99",
        "currencyCode": "USD",
    },
    "imageModule": {
        "imagePathList": ["https://ae01.alicdn.com/img1.jpg"],
    },
    "storeModule": {"brandName": "TestBrand", "storeID": "999"},
    "inventoryModule": {"totalAvailQuantity": 50},
    "actionModule": {"productId": _ITEM_ID},
}
_RUN_PARAMS_JS = f"window.runParams = {{\"data\": {_json.dumps(_RUN_PARAMS_DATA)}}};"
_PDP_HTML = (
    "<html><body>"
    + " " * 21000
    + "<script>"
    + _RUN_PARAMS_JS
    + "</script>"
    + "</body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "aliexpress"
    s.url = "https://www.aliexpress.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "aliexpress"
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
# Test: SRP curl path — 翻页计 api_calls
# ---------------------------------------------------------------------------

def test_aliexpress_srp_curl_counts_api(monkeypatch):
    """SRP curl 路径：page=1 GET via make_fetcher().get() → api_calls >= 1。
    至少一次 SRP fetch；找到 PDP URL 后 PDP fetch 也计入。
    _fetch_via_stealth 和 time.sleep monkeypatch，避免真实网络请求和超时等待。
    """
    import app.crawlers.aliexpress as _ali_mod
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site(), limit=1)

    url_map = {
        _SRP_URL: FetchResult(
            ok=True, url=_SRP_URL, status=200,
            text=_SRP_HTML, content=_SRP_HTML.encode(),
            final_url=_SRP_URL, fetcher="curl_cffi",
        ),
        _PDP_URL: FetchResult(
            ok=True, url=_PDP_URL, status=200,
            text=_PDP_HTML, content=_PDP_HTML.encode(),
            final_url=_PDP_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    # 避免 stealth fallback 触发真实网络请求
    monkeypatch.setattr(crawler, "_fetch_via_stealth", lambda url: None)
    # 避免 time.sleep(60) 在 curl+stealth 双失败时阻塞 (其余 kw 的 SRP 不在 url_map)
    monkeypatch.setattr(_ali_mod.time, "sleep", lambda s: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls for SRP GET, "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == _ITEM_ID
    assert p["site"] == "aliexpress"


def test_aliexpress_srp_pagination_respected(monkeypatch):
    """SRP 翻页：page=1 和 page=2 都被 GET（两次 api_calls，每页 >=5 新 item）。
    _fetch_via_stealth 和 time.sleep monkeypatch 避免真实网络请求和超时等待。
    """
    import app.crawlers.aliexpress as _ali_mod
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site(), limit=100)

    # page=1 含 5 个不同 item（触发翻页）, page=2 含 0 个新 item（停止）
    items_p1 = "".join(
        f'<a href="/item/1000000000{i}.html">Item{i}</a>'
        for i in range(5)
    )
    srp_p1_html = "<html><body>" + " " * 21000 + items_p1 + "</body></html>"
    srp_p1_url = f"{_BASE}/w/wholesale-sofa-cover.html?page=1"
    srp_p2_url = f"{_BASE}/w/wholesale-sofa-cover.html?page=2"

    url_map = {
        srp_p1_url: FetchResult(
            ok=True, url=srp_p1_url, status=200,
            text=srp_p1_html, content=srp_p1_html.encode(),
            final_url=srp_p1_url, fetcher="curl_cffi",
        ),
        srp_p2_url: FetchResult(
            ok=True, url=srp_p2_url, status=200,
            # empty page with no new items → pagination stops
            text="<html><body>" + " " * 21000 + "</body></html>",
            content=b"<html>",
            final_url=srp_p2_url, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "_fetch_via_stealth", lambda url: None)
    monkeypatch.setattr(_ali_mod.time, "sleep", lambda s: None)

    crawler.crawl()

    # Should have fetched at least page=1 and page=2 for keyword sofa cover
    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls for SRP pagination, "
        f"got {crawler.counter.api_calls}"
    )


# ---------------------------------------------------------------------------
# Test: stealth path — _fetch_via_stealth counts browser_opens
# ---------------------------------------------------------------------------

def _patch_stealth(monkeypatch, fake_page):
    """Helper: patch scrapling.fetchers.StealthyFetcher and stealth_kwargs."""
    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return fake_page

    fake_scrapling = type(sys)("scrapling")
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})


def test_aliexpress_stealth_success_counts_browser_opens(monkeypatch):
    """stealth 成功（body >=20K, status=200）→ browser_opens += 1。

    不 mock count_browser_fetch；success 用原 _blocked() 反面判断。
    """
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site())

    # Long clean page: _blocked() returns False (len >= 20K)
    clean_html = "<html><body>" + "x" * 25000 + "</body></html>"

    class _FakePage:
        status = 200
        html_content = clean_html
        body = None

    _patch_stealth(monkeypatch, _FakePage())

    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == clean_html


def test_aliexpress_stealth_short_body_with_block_mark_does_not_count(monkeypatch):
    """stealth 返回短 body (<20K) 且含 _BLOCK_MARKS → _blocked()=True → browser_opens 不增。

    _blocked() 逻辑：短 body 有 mark → True(blocked); 有 mark 才返回 True。
    """
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site())

    # Short body WITH block mark (nc challenge page): _blocked() returns True
    short_blocked_html = "<html><body>nc.x.alicdn.com blocked</body></html>"

    class _FakePageShortBlocked:
        status = 200
        html_content = short_blocked_html
        body = None

    _patch_stealth(monkeypatch, _FakePageShortBlocked())

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 for short body with block mark, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None


def test_aliexpress_stealth_slider_captcha_does_not_count(monkeypatch):
    """stealth 返回短 body 含 slidertest mark → _blocked()=True → browser_opens 不增。"""
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site())

    # Short body with slider captcha mark
    blocked_html = "<html><body>slidertest captcha here</body></html>"

    class _FakePageBlocked:
        status = 200
        html_content = blocked_html
        body = None

    _patch_stealth(monkeypatch, _FakePageBlocked())

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 for short body with slider captcha, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None


def test_aliexpress_stealth_non200_does_not_count(monkeypatch):
    """stealth 返回 status != 200 → browser_opens 不增。"""
    from app.crawlers.aliexpress import AliExpressCrawler

    crawler = AliExpressCrawler(_site())

    class _FakePageFailed:
        status = 503
        html_content = "<html><body>" + "x" * 25000 + "</body></html>"
        body = None

    _patch_stealth(monkeypatch, _FakePageFailed())

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 for status=503, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None
