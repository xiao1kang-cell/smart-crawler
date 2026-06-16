from __future__ import annotations

import pytest

from app.crawl_diagnostics import (
    EMPTY_SITEMAP,
    HTTP_403,
    MARKET_PAUSED,
    NETWORK_TIMEOUT,
    JOB_TIMEOUT,
    classify_exception,
    classify_http_status,
    job_timeout_failure,
)

pytestmark = pytest.mark.unit


def test_classifies_http_403_as_retryable_antibot_path():
    info = classify_http_status(403)

    assert info is not None
    assert info.code == HTTP_403
    assert info.retryable is True
    assert "住宅代理" in info.suggested_action


def test_classifies_timeout_exception():
    info = classify_exception(TimeoutError("connection timed out"))

    assert info.code == NETWORK_TIMEOUT
    assert info.retryable is True


def test_classifies_empty_sitemap_message():
    info = classify_exception(RuntimeError("sitemap_index 返回 200 但无 custom-product 子 sitemap"))

    assert info.code == EMPTY_SITEMAP
    assert info.retryable is False


def test_classifies_market_paused_message():
    info = classify_exception(RuntimeError("market paused: pausing orders until further notice"))

    assert info.code == MARKET_PAUSED
    assert info.retryable is False


def test_job_timeout_failure_is_retryable_with_action():
    info = job_timeout_failure("demo_us", 1800)

    assert info.code == JOB_TIMEOUT
    assert info.stage == "job"
    assert info.retryable is True
    assert "重跑" in info.suggested_action


def test_classifies_auto_canceled_as_job_timeout():
    info = classify_exception(RuntimeError("auto-canceled: stuck running >30min"))

    assert info.code == JOB_TIMEOUT
    assert info.stage == "job"
