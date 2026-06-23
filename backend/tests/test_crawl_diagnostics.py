from __future__ import annotations

import pytest

from app.crawl_diagnostics import (
    EMPTY_SITEMAP,
    ANTI_BOT_CHALLENGE,
    HTTP_403,
    MARKET_PAUSED,
    NETWORK_TIMEOUT,
    JOB_TIMEOUT,
    HTTP_5XX,
    MANUAL_MAINTENANCE,
    QUEUE_STALLED,
    RESOURCE_EXHAUSTED,
    UNSUPPORTED_PLATFORM,
    WORKER_INTERRUPTED,
    classify_exception,
    classify_http_status,
    job_timeout_failure,
    record_url_state,
)
from app.db import SessionLocal, init_db
from app.models import CrawlUrl

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


def test_classifies_blocked_circuit_breaker_as_anti_bot():
    info = classify_exception(RuntimeError("sephora PDP 连续被拦截，熔断 ok=0"))

    assert info.code == ANTI_BOT_CHALLENGE
    assert info.retryable is True


def test_classifies_kasada_warmup_failure_as_anti_bot():
    info = classify_exception(RuntimeError("熔断：otto Kasada profile warmup failed"))

    assert info.code == ANTI_BOT_CHALLENGE
    assert info.retryable is True


def test_classifies_unknown_platform_as_configuration_issue():
    info = classify_exception(ValueError("未知平台: sephora"))

    assert info.code == UNSUPPORTED_PLATFORM
    assert info.retryable is False


def test_classifies_target_circuit_breaker_as_anti_bot():
    info = classify_exception(RuntimeError("熔断：target 连续 8 次 403/429，熔断（站点已进入冷却期）"))

    assert info.code == ANTI_BOT_CHALLENGE
    assert info.retryable is True


def test_classifies_worker_interruption_cleanup():
    info = classify_exception(RuntimeError("[Errno 32] Broken pipe Traceback ... runner.py"))

    assert info.code == WORKER_INTERRUPTED
    assert info.stage == "job"
    assert info.retryable is True


def test_classifies_resource_exhausted_thread_failure():
    info = classify_exception(RuntimeError("can't start new thread"))

    assert info.code == RESOURCE_EXHAUSTED
    assert info.retryable is True


def test_classifies_http_error_503_text():
    info = classify_exception(RuntimeError("HTTP Error 503: service unavailable"))

    assert info.code == HTTP_5XX
    assert info.http_status == 503


def test_classifies_queue_stalled_verification_message():
    info = classify_exception(RuntimeError("verification: trigger API queued job, but worker did not consume it within 4 minutes"))

    assert info.code == QUEUE_STALLED
    assert info.retryable is True


def test_classifies_manual_maintenance_retry_notes():
    info = classify_exception(RuntimeError("reschedule after fix"))

    assert info.code == MANUAL_MAINTENANCE
    assert info.retryable is True


def test_classifies_unsupported_crawler_platform_note():
    info = classify_exception(RuntimeError("manual rerun: unsupported crawler platform"))

    assert info.code == UNSUPPORTED_PLATFORM
    assert info.retryable is False


def test_successful_url_state_clears_prior_failure():
    init_db()
    s = SessionLocal()
    try:
        failure = classify_exception(TimeoutError("connection timed out"))
        record_url_state(
            s,
            site="demo_site",
            url="https://example.com/p/1",
            status="failed",
            failure=failure,
        )
        s.commit()

        record_url_state(
            s,
            site="demo_site",
            url="https://example.com/p/1",
            status="parsed",
            http_status=200,
        )
        s.commit()

        row = s.query(CrawlUrl).filter_by(
            site="demo_site",
            url="https://example.com/p/1",
        ).one()
        assert row.status == "parsed"
        assert row.failure_code is None
        assert row.failure_stage is None
        assert row.failure_detail is None
        assert row.retryable is None
    finally:
        s.close()
