from __future__ import annotations

import pytest

from app.antiban import BlockedError
from app.fetching import CrawlCounter, CrawlerFetcher, FetchContext, FetchResult
from app.db import SessionLocal, init_db
from app.models import CrawlFailure, CrawlJob, Site

pytestmark = pytest.mark.unit


def _site() -> Site:
    return Site(site="t", url="https://example.com", country="US",
                proxy_tier="none", platform="generic")


def _ctx(counter, retries=0):
    return FetchContext(site=_site(), counter=counter, use_proxy=False,
                        retries=retries)


def _fail_fast_ctx(counter=None):
    return FetchContext(site=_site(), counter=counter, use_proxy=False,
                        retries=0, fail_fast_blocked=True)


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


def test_fail_fast_blocked_raises_on_anti_bot_challenge(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "<html>Cloudflare verify you are human</html>"
        content = text.encode()
        url = "https://example.com/p/1"

    class FakeSession:
        def __init__(self, **kwargs):
            self.headers = {}
            self.proxies = {}

        def request(self, method, url, timeout=30, **kwargs):
            return FakeResponse()

    fetcher = CrawlerFetcher(_fail_fast_ctx(), middlewares=[])
    monkeypatch.setattr("app.fetching.creq.Session", FakeSession)
    monkeypatch.setattr("app.fetching._record_fetch", lambda *args, **kwargs: None)

    with pytest.raises(BlockedError) as exc:
        fetcher.get("https://example.com/p/1")

    assert "anti_bot_challenge" in str(exc.value)


def test_required_proxy_missing_does_not_direct_connect(monkeypatch):
    class FakeSession:
        def __init__(self, **kwargs):
            self.headers = {}
            self.proxies = {}

        def request(self, method, url, timeout=30, **kwargs):
            raise AssertionError("request should not be sent without required proxy")

    site = Site(site="needs_proxy", url="https://example.com", country="US",
                proxy_tier="residential", platform="generic")
    ctx = FetchContext(site=site, retries=2, use_proxy=True)
    fetcher = CrawlerFetcher(ctx)
    monkeypatch.setattr("app.fetching.proxy_pool.get_proxy",
                        lambda tier, site=None: None)
    monkeypatch.setattr("app.fetching.proxy_pool.lease_proxy",
                        lambda *a, **k: None)
    monkeypatch.setattr("app.fetching.creq.Session", FakeSession)
    monkeypatch.setattr("app.fetching._record_fetch", lambda *args, **kwargs: None)

    result = fetcher.get("https://example.com/p/1")

    assert not result.ok
    assert result.failure is not None
    assert result.failure.code == "proxy_unavailable"
    assert result.attempt == 1


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


def test_post_retry_to_success_counts_once(monkeypatch):
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
    fetcher.post("https://example.com/api", data="{}")
    assert c.api_calls == 1


def test_retry_failure_event_does_not_mark_job_when_later_success(monkeypatch):
    init_db()
    site = Site(site="retry_success_site", url="https://example.com",
                country="US", proxy_tier="none", platform="generic")
    s = SessionLocal()
    try:
        job = CrawlJob(site=site.site, status="running")
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    class OkResponse:
        status_code = 200
        text = "ok"
        content = b"ok"
        url = "https://example.com/p/1"
        headers = {}

    class FlakySession:
        calls = 0

        def __init__(self, **kwargs):
            self.headers = {}
            self.proxies = {}

        def request(self, method, url, timeout=30, **kwargs):
            FlakySession.calls += 1
            if FlakySession.calls == 1:
                raise TimeoutError("Connection timed out after 30002 milliseconds")
            return OkResponse()

    ctx = FetchContext(site=site, job_id=job_id, use_proxy=False, retries=1)
    fetcher = CrawlerFetcher(ctx, middlewares=[])
    monkeypatch.setattr("app.fetching.creq.Session", FlakySession)
    monkeypatch.setattr("app.fetching.time.sleep", lambda *_: None)

    result = fetcher.get("https://example.com/p/1")

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        failures = (s.query(CrawlFailure)
                    .filter(CrawlFailure.job_id == job_id)
                    .all())
        assert result.ok is True
        assert job.failure_code is None
        assert [row.code for row in failures] == ["network_timeout"]
    finally:
        s.close()


def test_terminal_retry_failure_does_not_mark_running_job(monkeypatch):
    init_db()
    site = Site(site="retry_failed_site", url="https://example.com",
                country="US", proxy_tier="none", platform="generic")
    s = SessionLocal()
    try:
        job = CrawlJob(site=site.site, status="running")
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    class FailingSession:
        def __init__(self, **kwargs):
            self.headers = {}
            self.proxies = {}

        def request(self, method, url, timeout=30, **kwargs):
            raise TimeoutError("Connection timed out after 30002 milliseconds")

    ctx = FetchContext(site=site, job_id=job_id, use_proxy=False, retries=1)
    fetcher = CrawlerFetcher(ctx, middlewares=[])
    monkeypatch.setattr("app.fetching.creq.Session", FailingSession)
    monkeypatch.setattr("app.fetching.time.sleep", lambda *_: None)

    result = fetcher.get("https://example.com/p/1")

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert result.ok is False
        assert result.failure and result.failure.code == "network_timeout"
        assert job.failure_code is None
    finally:
        s.close()
