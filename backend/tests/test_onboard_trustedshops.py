"""TDD test: trustedshops crawler 批C 收编验证（评论 crawler 形态）。

验证两段计数：
- curl 路径：make_fetcher().get() → api_calls 累增
- stealth 路径：StealthyFetcher.fetch 用 count_browser_fetch 包裹 → browser_opens += 1
- stealth 失败（非 200）：browser_opens 不计

TrustedShops 是评论平台 crawler：
- 构造签名：TrustedShopsCrawler(channel, max_pages=20)  → 保持向后兼容
- crawl() 返回 list[dict]（reviews，非 CrawlResult）
- 翻页：start 从 0 递增，检查 data["remaining"] <= 0 停止
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.fetching import FetchResult

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture: 真实 API 响应结构（基于 2026-05-24 实测）
# ---------------------------------------------------------------------------

TS_ID = "X330A2E7D449741E4C6563A7B798B7E16"


def _review_raw(rid: str, mark: float = 4.5) -> dict:
    return {
        "id": rid,
        "comment": f"Super Erfahrung #{rid}",
        "mark": mark,
        "createdDate": "2026-05-23T10:00:00Z",
        "anonymousAlias": "MaxM.",
        "reply": {"comment": "Danke!", "createdDate": "2026-05-24T08:00:00Z"},
    }


_PAGE1_RESPONSE = {
    "reviews": [_review_raw("r001"), _review_raw("r002", mark=3.0)],
    "remaining": 1,
}

_PAGE2_RESPONSE = {
    "reviews": [_review_raw("r003", mark=5.0)],
    "remaining": 0,
}

_EMPTY_RESPONSE = {
    "reviews": [],
    "remaining": 0,
}


def _channel() -> dict:
    return {
        "site": "ts_test",
        "ts_id": TS_ID,
        "domain": "example.de",
        "host": "www.trustedshops.com",
        "country": "DE",
        "max_pages": 10,
    }


def _ok_result(url: str, body: dict) -> FetchResult:
    text = json.dumps(body)
    return FetchResult(
        ok=True,
        url=url,
        status=200,
        text=text,
        content=text.encode(),
        final_url=url,
        fetcher="curl_cffi",
    )


def _blocked_result(url: str, code: int = 403) -> FetchResult:
    return FetchResult(
        ok=False,
        url=url,
        status=code,
        text="",
        content=b"",
        final_url=url,
        fetcher="curl_cffi",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_trustedshops_curl_path_counts_api_calls(monkeypatch):
    """curl 路径 make_fetcher().get() 每次成功计入 api_calls；解析正确。"""
    from app.crawlers.trustedshops import TrustedShopsCrawler

    crawler = TrustedShopsCrawler(_channel())
    pages_fetched: list[int] = []

    def fake_get(url: str, **kw) -> FetchResult:
        # 推断是第几页（根据 start 参数）
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        start = int(qs.get("start", ["0"])[0])
        page = start // 50
        pages_fetched.append(page)
        crawler.counter.api_calls += 1
        if page == 0:
            return _ok_result(url, _PAGE1_RESPONSE)
        elif page == 1:
            return _ok_result(url, _PAGE2_RESPONSE)
        else:
            return _ok_result(url, _EMPTY_RESPONSE)

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    reviews = crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (2 pages), got {crawler.counter.api_calls}"
    )
    assert isinstance(reviews, list)
    assert len(reviews) == 3, (
        f"Expected 3 reviews (2 on page0 + 1 on page1), got {len(reviews)}"
    )

    # 验证 _normalize 输出字段
    first = reviews[0]
    assert first["review_id"] == "r001"
    assert first["platform"] == "trustedshops"
    assert first["site"] == "ts_test"
    assert first["reviewer_name"] == "MaxM."
    assert first["rating"] == 4.5
    assert first["content"] == "Super Erfahrung #r001"
    assert first["review_date"] == date(2026, 5, 23)
    assert first["reply_content"] == "Danke!"
    assert first["is_verified"] is True
    assert first["reviewer_country"] == "DE"


def test_trustedshops_stealth_path_counts_browser_opens(monkeypatch):
    """stealth 路径成功（status==200）→ browser_opens 计入；curl 先 403 触发 stealth。"""
    from app.crawlers.trustedshops import TrustedShopsCrawler

    crawler = TrustedShopsCrawler(_channel())

    # curl 段返回 403（触发 stealth）
    def fake_get(url: str, **kw) -> FetchResult:
        return _blocked_result(url, 403)

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    # StealthyFetcher.fetch 成功返回 status=200 的 page mock
    class _FakePage:
        status = 200

        @property
        def html_content(self):
            return json.dumps(_PAGE1_RESPONSE)

        @property
        def body(self):
            return json.dumps(_PAGE1_RESPONSE)

    stealth_calls: list[str] = []

    def fake_stealth_fetch(url, **kw):
        stealth_calls.append(url)
        return _FakePage()

    # monkeypatch StealthyFetcher.fetch（不 mock count_browser_fetch）
    try:
        from scrapling import fetchers as _sf_mod
        monkeypatch.setattr(_sf_mod.StealthyFetcher, "fetch", staticmethod(fake_stealth_fetch))
    except Exception:
        # scrapling 未安装时跳过
        pytest.skip("scrapling not installed")

    reviews = crawler.crawl()

    assert crawler.counter.browser_opens >= 1, (
        f"Expected browser_opens >= 1 after stealth success, got {crawler.counter.browser_opens}"
    )
    assert len(reviews) >= 2, (
        f"Expected at least 2 reviews from page1 stealth fallback, got {len(reviews)}"
    )


def test_trustedshops_stealth_failure_does_not_count(monkeypatch):
    """stealth 路径失败（status!=200）→ browser_opens 不累加；crawl 返回空列表。"""
    from app.crawlers.trustedshops import TrustedShopsCrawler

    crawler = TrustedShopsCrawler(_channel())

    # curl 段 403 触发 stealth
    def fake_get(url: str, **kw) -> FetchResult:
        return _blocked_result(url, 403)

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    # stealth 失败（返回 status=503 的 page）
    class _FailPage:
        status = 503
        html_content = ""
        body = ""

    def fake_stealth_fetch_fail(url, **kw):
        return _FailPage()

    try:
        from scrapling import fetchers as _sf_mod
        monkeypatch.setattr(_sf_mod.StealthyFetcher, "fetch", staticmethod(fake_stealth_fetch_fail))
    except Exception:
        pytest.skip("scrapling not installed")

    reviews = crawler.crawl()

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens == 0 after stealth failure, got {crawler.counter.browser_opens}"
    )
    assert reviews == []


def test_trustedshops_pagination_stops_on_remaining_zero(monkeypatch):
    """remaining == 0 时翻页停止。"""
    from app.crawlers.trustedshops import TrustedShopsCrawler

    crawler = TrustedShopsCrawler(_channel())
    pages_hit: list[int] = []

    def fake_get(url: str, **kw) -> FetchResult:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        page = int(qs.get("start", ["0"])[0]) // 50
        pages_hit.append(page)
        crawler.counter.api_calls += 1
        if page == 0:
            return _ok_result(url, _PAGE1_RESPONSE)  # remaining=1
        return _ok_result(url, _PAGE2_RESPONSE)       # remaining=0 → stop

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    reviews = crawler.crawl()

    assert pages_hit == [0, 1], (
        f"Expected pages [0, 1] (stops at remaining=0), got {pages_hit}"
    )
    assert len(reviews) == 3


def test_trustedshops_missing_ts_id_returns_empty(monkeypatch):
    """ts_id 缺失时 crawl 直接返回空列表，不发请求。"""
    from app.crawlers.trustedshops import TrustedShopsCrawler

    channel = {"site": "ts_test", "host": "www.trustedshops.com", "country": "DE"}
    crawler = TrustedShopsCrawler(channel)

    called = []

    class _FakeFetcher:
        def get(self, url, **kw):
            called.append(url)
            return _ok_result(url, _EMPTY_RESPONSE)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    reviews = crawler.crawl()
    assert reviews == []
    assert called == [], "Should not make any HTTP requests when ts_id is missing"
