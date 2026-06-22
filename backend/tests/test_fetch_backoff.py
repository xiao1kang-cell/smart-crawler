"""429/503 退避 —— Retry-After 优先,无头则指数退避封顶 60s。"""
from app.fetching import _backoff_seconds, FetchResult


def _result_with_retry_after(value):
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = value
    return r


def test_retry_after_numeric_seconds_honored():
    r = _result_with_retry_after(30.0)
    assert _backoff_seconds(r, attempt=1) == 30.0


def test_retry_after_capped_at_max():
    r = _result_with_retry_after(9999.0)
    assert _backoff_seconds(r, attempt=1) == 60.0


def test_no_header_exponential_sequence():
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = None
    # 2 * 2^(attempt-1) + jitter(0~1)，断言落在区间
    s1 = _backoff_seconds(r, attempt=1)
    s2 = _backoff_seconds(r, attempt=2)
    s3 = _backoff_seconds(r, attempt=3)
    assert 2.0 <= s1 < 3.0
    assert 4.0 <= s2 < 5.0
    assert 8.0 <= s3 < 9.0


def test_no_header_exponential_capped():
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = None
    assert _backoff_seconds(r, attempt=10) == 60.0


def test_parse_retry_after_http_date_returns_none():
    from app.fetching import _parse_retry_after
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after(None) is None


def test_large_product_page_with_bot_beacon_is_not_antibot():
    from app.fetching import _looks_like_anti_bot

    html = (
        "<html><head>"
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
        + " " * 210_000
        + '<script type="application/ld+json">'
        + '{"@context":"https://schema.org","@type":"Product","name":"Shelf"}'
        + "</script></head><body></body></html>"
    )

    assert _looks_like_anti_bot(html) is False


def test_retry_loop_uses_backoff_not_fixed_sleep(monkeypatch):
    """重试循环按 _backoff_seconds 退避,而非固定 min(2*attempt,5)。"""
    import app.fetching as fetching
    from app.fetching import CrawlerFetcher, FetchContext, FetchResult
    from app.crawl_diagnostics import FailureInfo, HTTP_429, STAGE_FETCH
    from app.models import Site

    slept = []
    monkeypatch.setattr(fetching.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(fetching, "acquire_rate", lambda *a, **k: None)

    site = Site(site="costway_it", platform="magento", proxy_tier="none",
                country="IT", url="https://www.costway.it/")
    fetcher = CrawlerFetcher(FetchContext(site=site, use_proxy=False, retries=2))

    def fake_429(method, url, **kw):
        r = FetchResult(ok=False, url=url, status=429,
                        failure=FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢"))
        r.retry_after = 30.0
        return r

    monkeypatch.setattr(fetcher, "_request_once", fake_429)
    fetcher.get("https://www.costway.it/p/1")
    # 至少有一次按 Retry-After=30 退避(而非旧的 5s 封顶)
    assert any(s == 30.0 for s in slept)
