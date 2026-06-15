from __future__ import annotations

import pytest

from app.fetching import CrawlCounter, CrawlerFetcher, FetchContext, FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


def _site() -> Site:
    return Site(site="t", url="https://example.com", country="US",
                proxy_tier="none", platform="generic")


def _ctx(counter, retries=0):
    return FetchContext(site=_site(), counter=counter, use_proxy=False,
                        retries=retries)


def test_counter_pages_is_sum():
    c = CrawlCounter(api_calls=3, browser_opens=2)
    assert c.pages_fetched == 5


def test_success_curl_increments_api_calls(monkeypatch):
    c = CrawlCounter()
    fetcher = CrawlerFetcher(_ctx(c), middlewares=[])

    def fake_once(method, url, *, attempt=1, **kw):
        return FetchResult(ok=True, url=url, status=200, text="ok",
                           fetcher="curl_cffi", attempt=attempt)

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    fetcher.get("https://example.com/p/1")
    assert c.api_calls == 1
    assert c.browser_opens == 0


def test_failure_does_not_count(monkeypatch):
    c = CrawlCounter()
    fetcher = CrawlerFetcher(_ctx(c), middlewares=[])

    def fake_once(method, url, *, attempt=1, **kw):
        return FetchResult(ok=False, url=url, status=503,
                           fetcher="curl_cffi", attempt=attempt)

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    fetcher.get("https://example.com/p/1")
    assert c.api_calls == 0


def test_retry_to_success_counts_once(monkeypatch):
    c = CrawlCounter()
    fetcher = CrawlerFetcher(_ctx(c, retries=2), middlewares=[])
    calls = {"n": 0}

    def fake_once(method, url, *, attempt=1, **kw):
        calls["n"] += 1
        ok = calls["n"] >= 2
        return FetchResult(ok=ok, url=url, status=200 if ok else 503,
                           fetcher="curl_cffi", attempt=attempt)

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    monkeypatch.setattr("app.fetching.time.sleep", lambda *_: None)
    fetcher.get("https://example.com/p/1")
    assert c.api_calls == 1


def test_counter_none_is_noop(monkeypatch):
    fetcher = CrawlerFetcher(_ctx(None), middlewares=[])

    def fake_once(method, url, *, attempt=1, **kw):
        return FetchResult(ok=True, url=url, status=200, fetcher="curl_cffi")

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    result = fetcher.get("https://example.com/p/1")
    assert result.ok


def test_success_stealth_increments_browser_opens(monkeypatch):
    c = CrawlCounter()
    fetcher = CrawlerFetcher(_ctx(c), middlewares=[])

    def fake_once(method, url, *, attempt=1, **kw):
        return FetchResult(ok=True, url=url, status=200,
                           fetcher="scrapling", attempt=attempt)

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    fetcher.get("https://example.com/p/1")
    assert c.browser_opens == 1
    assert c.api_calls == 0


def test_post_counts_as_api_call(monkeypatch):
    c = CrawlCounter()
    fetcher = CrawlerFetcher(_ctx(c), middlewares=[])

    def fake_once(method, url, *, attempt=1, **kw):
        assert method == "POST"
        return FetchResult(ok=True, url=url, status=200,
                           text='{"data": 1}', fetcher="curl_cffi")

    monkeypatch.setattr(fetcher, "_request_once", fake_once)
    res = fetcher.post("https://example.com/api", data="{}")
    assert c.api_calls == 1
    assert res.json() == {"data": 1}


def test_fetchresult_json_invalid_returns_none():
    res = FetchResult(ok=True, url="x", status=200, text="not json")
    assert res.json() is None
