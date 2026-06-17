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
