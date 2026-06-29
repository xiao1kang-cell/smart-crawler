from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.crawl_diagnostics import (
    FailureInfo,
    STAGE_JOB,
    TRACKING_PAUSED,
    record_failure,
)
from app.crawlers.base import CrawlResult
from app.db import SessionLocal, init_db
from app.models import CrawlFailure, CrawlJob, Product, Site, Workspace, WorkspaceSite
from app.runner import (
    _auto_enqueue_job_retry,
    _is_non_fatal_partial,
    claim_job,
    enqueue,
    execute_job,
)

pytestmark = pytest.mark.unit


class _ZeroCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        s = SessionLocal()
        try:
            record_failure(
                s,
                site="runner_zero_probe",
                job_id=self.job_id,
                info=FailureInfo(
                    "network_timeout",
                    STAGE_JOB,
                    "sitemap timeout",
                    True,
                    "检查代理后重跑",
                ),
            )
            s.commit()
        finally:
            s.close()
        out = CrawlResult()
        out.notes.append("sitemap timeout")
        return out


class _PartialCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        s = SessionLocal()
        try:
            record_failure(
                s,
                site="runner_partial_probe",
                job_id=self.job_id,
                info=FailureInfo(
                    "http_429",
                    "fetch",
                    "rate limited after first page",
                    True,
                    "降低并发和频率，延长冷却时间或更换代理出口",
                ),
            )
            s.commit()
        finally:
            s.close()
        out = CrawlResult()
        out.products.append({
            "sku": "PARTIAL-1",
            "title": "Partial Product",
            "site": "runner_partial_probe",
            "product_url": "https://example.com/products/partial-1",
            "sale_price": 10,
        })
        out.notes.append("rate limited after first page")
        return out


class _ProxyUnavailableCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        s = SessionLocal()
        try:
            record_failure(
                s,
                site="runner_proxy_fail_probe",
                job_id=self.job_id,
                info=FailureInfo(
                    "proxy_unavailable",
                    "fetch",
                    "no available proxy lease",
                    True,
                    "检查代理池后重跑",
                ),
            )
            s.commit()
        finally:
            s.close()
        return CrawlResult()


class _HighYieldTransientCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        s = SessionLocal()
        try:
            record_failure(
                s,
                site="runner_high_yield_probe",
                job_id=self.job_id,
                info=FailureInfo(
                    "network_timeout",
                    "fetch",
                    "one PDP timed out after enough products were parsed",
                    True,
                    "稍后重试或检查代理稳定性",
                ),
            )
            s.commit()
        finally:
            s.close()
        out = CrawlResult()
        for idx in range(60):
            out.products.append({
                "sku": f"HY-{idx}",
                "title": f"High Yield {idx}",
                "site": "runner_high_yield_probe",
                "product_url": f"https://example.com/products/{idx}",
                "sale_price": 10,
            })
        return out


def test_execute_job_zero_products_preserves_specific_failure(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(Site.site == "runner_zero_probe").first():
            s.add(Site(site="runner_zero_probe", brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
            s.commit()
    finally:
        s.close()

    monkeypatch.setattr("app.runner.get_crawler", lambda site: _ZeroCrawler())
    job_id = enqueue("runner_zero_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert result["status"] == "failed"
        assert result["error"] == "sitemap timeout"
        assert result["failure_code"] == "network_timeout"
        assert result["auto_retry_job_id"] is not None
        assert "已自动创建整站重跑任务" in result["suggested_action"]
        assert job.status == "failed"
        assert job.failure_code == "network_timeout"
        assert job.products_count == 0
    finally:
        s.close()


def test_execute_job_products_with_failure_becomes_partial(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(Site.site == "runner_partial_probe").first():
            s.add(Site(site="runner_partial_probe", brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
            s.commit()
    finally:
        s.close()

    monkeypatch.setattr("app.runner.get_crawler", lambda site: _PartialCrawler())
    job_id = enqueue("runner_partial_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert result["status"] == "partial"
        assert result["products"] == 1
        assert result["failure_code"] == "http_429"
        assert result["error"] == "rate limited after first page"
        assert result["suggested_action"] == "降低并发和频率，延长冷却时间或更换代理出口"
        assert job.status == "partial"
        assert job.products_count == 1
        assert job.failure_code == "http_429"
        assert job.failure_stage == "fetch"
    finally:
        s.close()


def test_execute_job_failed_result_auto_retries(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(Site.site == "runner_proxy_fail_probe").first():
            s.add(Site(site="runner_proxy_fail_probe", brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
            s.commit()
    finally:
        s.close()

    monkeypatch.setattr("app.runner.get_crawler", lambda site: _ProxyUnavailableCrawler())
    job_id = enqueue("runner_proxy_fail_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        retry_job = (s.query(CrawlJob)
                     .filter(CrawlJob.site == "runner_proxy_fail_probe",
                             CrawlJob.id != job_id)
                     .order_by(CrawlJob.id.desc())
                     .first())
        assert result["status"] == "failed"
        assert result["failure_code"] == "proxy_unavailable"
        assert job.status == "failed"
        assert job.failure_code == "proxy_unavailable"
        assert retry_job is not None
        assert retry_job.status == "pending"
        assert retry_job.trigger == "admin_retry"
    finally:
        s.close()


def test_auto_job_retry_is_idempotent_per_source_job():
    init_db()
    s = SessionLocal()
    site_name = "runner_auto_retry_idempotent"
    try:
        if not s.query(Site).filter(Site.site == site_name).first():
            s.add(Site(site=site_name, brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
        s.query(CrawlJob).filter(CrawlJob.site == site_name).delete()
        source = CrawlJob(site=site_name, status="failed", trigger="manual",
                          failure_code="job_timeout",
                          created_at=datetime(2026, 6, 25, 8, 0))
        s.add(source)
        s.flush()

        first_id = _auto_enqueue_job_retry(s, source, reason_code="job_timeout")
        second_id = _auto_enqueue_job_retry(s, source, reason_code="job_timeout")

        assert first_id == second_id
        retries = (s.query(CrawlJob)
                   .filter(CrawlJob.site == site_name,
                           CrawlJob.suggested_action.like("[auto_job_retry]%"))
                   .all())
        assert len(retries) == 1
        assert f"#{first_id}" in source.suggested_action
    finally:
        s.rollback()
        s.close()


def test_auto_job_retry_skips_when_newer_success_exists():
    init_db()
    s = SessionLocal()
    site_name = "runner_auto_retry_has_success"
    try:
        if not s.query(Site).filter(Site.site == site_name).first():
            s.add(Site(site=site_name, brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
        s.query(CrawlJob).filter(CrawlJob.site == site_name).delete()
        source = CrawlJob(site=site_name, status="failed", trigger="manual",
                          failure_code="job_timeout",
                          created_at=datetime(2026, 6, 25, 8, 0))
        success = CrawlJob(site=site_name, status="success", trigger="manual",
                           created_at=datetime(2026, 6, 25, 9, 0))
        s.add_all([source, success])
        s.flush()

        retry_id = _auto_enqueue_job_retry(s, source, reason_code="job_timeout")

        assert retry_id == success.id
        assert "已有更新成功任务" in source.suggested_action
        retries = (s.query(CrawlJob)
                   .filter(CrawlJob.site == site_name,
                           CrawlJob.suggested_action.like("[auto_job_retry]%"))
                   .count())
        assert retries == 0
    finally:
        s.rollback()
        s.close()


def test_execute_job_high_yield_transient_failure_stays_success(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(Site.site == "runner_high_yield_probe").first():
            s.add(Site(site="runner_high_yield_probe", brand="Probe", country="US",
                       url="https://example.com", platform="generic",
                       proxy_tier="none"))
            s.commit()
    finally:
        s.close()

    monkeypatch.setattr("app.runner.get_crawler", lambda site: _HighYieldTransientCrawler())
    job_id = enqueue("runner_high_yield_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        failures = s.query(CrawlFailure).filter(CrawlFailure.job_id == job_id).all()
        assert result["status"] == "success"
        assert result["products"] == 60
        assert "failure_code" not in result
        assert job.status == "success"
        assert job.failure_code is None
        assert job.products_count == 60
        assert [f.code for f in failures] == ["network_timeout"]
    finally:
        s.close()


def test_non_fatal_partial_uses_cdiscount_site_threshold():
    cdiscount = CrawlJob(site="cdiscount_fr", failure_code="anti_bot_challenge",
                         success_rate=100.0)
    generic = CrawlJob(site="other_site", failure_code="anti_bot_challenge",
                       success_rate=100.0)
    parse_noise = CrawlJob(site="vidaxl_ro", failure_code="parse_no_jsonld",
                           success_rate=100.0)
    coverage_noise = CrawlJob(site="costway_uk",
                              failure_code="incomplete_detail_parse",
                              success_rate=99.9)

    assert _is_non_fatal_partial(cdiscount, 48) is True
    assert _is_non_fatal_partial(generic, 48) is False
    assert _is_non_fatal_partial(parse_noise, 199) is True
    assert _is_non_fatal_partial(coverage_noise, 9939) is True


def test_execute_job_applies_configured_price_feed(monkeypatch, tmp_path):
    init_db()
    feed = tmp_path / "prices.csv"
    feed.write_text(
        "sku,price,regular_price,currency\n"
        "FEED-1,19.99,29.99,USD\n",
        encoding="utf-8",
    )

    class _FeedCrawler:
        job_id: int | None = None

        def crawl(self) -> CrawlResult:
            out = CrawlResult()
            out.products.append({
                "sku": "FEED-1",
                "title": "Feed Product",
                "site": "runner_feed_probe",
                "product_url": "https://example.com/products/feed-1",
            })
            return out

    s = SessionLocal()
    try:
        s.query(Product).filter(Product.site == "runner_feed_probe").delete()
        s.query(Site).filter(Site.site == "runner_feed_probe").delete()
        s.add(Site(
            site="runner_feed_probe",
            brand="Probe",
            country="US",
            url="https://example.com",
            platform="generic",
            proxy_tier="none",
            crawler_config={
                "price_source_type": "feed",
                "price_feed_url": str(feed),
            },
        ))
        s.commit()
    finally:
        s.close()

    monkeypatch.setattr("app.runner.get_crawler", lambda site: _FeedCrawler())
    job_id = enqueue("runner_feed_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        product = (s.query(Product)
                   .filter(Product.site == "runner_feed_probe",
                           Product.sku == "FEED-1")
                   .one())
        assert result["status"] == "success"
        assert result["products"] == 1
        assert product.sale_price == 19.99
        assert product.original_price == 29.99
        assert product.currency == "USD"
        assert any("configured_price_source: matched=1" in note
                   for note in result["notes"])
    finally:
        s.close()


def test_enqueue_auto_job_skips_when_required_proxy_unavailable(monkeypatch):
    init_db()
    from app import proxy_pool

    monkeypatch.setattr(proxy_pool, "has_available_proxy",
                        lambda tier, site=None: False)
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        s.query(Site).filter(Site.site == "runner_proxy_probe").delete()
        s.add(Site(site="runner_proxy_probe", brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="residential"))
        s.commit()
    finally:
        s.close()

    job_id = enqueue("runner_proxy_probe", trigger="scheduled")

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.job_id == job_id)
                   .order_by(CrawlFailure.id.desc())
                   .first())
        assert job.status == "skipped"
        assert job.failure_code == "proxy_unavailable"
        assert failure is not None
        assert failure.code == "proxy_unavailable"
    finally:
        s.close()


def test_claim_job_skips_proxy_preflight_failure_and_claims_next(monkeypatch):
    init_db()
    from app import proxy_pool

    def fake_available(tier, site=None):
        return site != "runner_proxy_blocked"

    monkeypatch.setattr(proxy_pool, "has_available_proxy", fake_available)
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        for site_name in ("runner_proxy_blocked", "runner_proxy_ready"):
            s.query(Site).filter(Site.site == site_name).delete()
        s.add(Site(site="runner_proxy_blocked", brand="Probe", country="US",
                   url="https://example.com/blocked", platform="generic",
                   proxy_tier="residential"))
        s.add(Site(site="runner_proxy_ready", brand="Probe", country="US",
                   url="https://example.com/ready", platform="generic",
                   proxy_tier="none"))
        s.flush()
        blocked = CrawlJob(site="runner_proxy_blocked", status="pending",
                           trigger="scheduled", created_at=now)
        ready = CrawlJob(site="runner_proxy_ready", status="pending",
                         trigger="scheduled",
                         created_at=now + timedelta(seconds=1))
        s.add_all([blocked, ready])
        s.commit()
        blocked_id = blocked.id
        ready_id = ready.id
    finally:
        s.close()

    assert claim_job("worker-test") == ready_id

    s = SessionLocal()
    try:
        blocked = s.get(CrawlJob, blocked_id)
        ready = s.get(CrawlJob, ready_id)
        assert blocked.status == "skipped"
        assert blocked.failure_code == "proxy_unavailable"
        assert ready.status == "running"
        assert ready.heartbeat_at is not None
    finally:
        s.close()


def test_auto_enqueue_does_not_create_job_for_paused_tracking_site(monkeypatch):
    init_db()
    from app import proxy_pool

    monkeypatch.setattr(proxy_pool, "has_available_proxy",
                        lambda tier, site=None: False)
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        s.query(Site).filter(Site.site == "runner_paused_probe").delete()
        s.add(Site(site="runner_paused_probe", brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="residential", track_status="paused"))
        s.commit()
    finally:
        s.close()

    job_id = enqueue("runner_paused_probe", trigger="daily_refresh")

    s = SessionLocal()
    try:
        job = (s.query(CrawlJob)
               .filter(CrawlJob.site == "runner_paused_probe")
               .first())
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.site == "runner_paused_probe")
                   .order_by(CrawlFailure.id.desc())
                   .first())
        assert job_id is None
        assert job is None
        assert failure is None
    finally:
        s.close()


def test_enqueue_admin_retry_restores_error_site_tracking():
    init_db()
    site_name = "runner_retry_restores_tracking"
    s = SessionLocal()
    try:
        s.query(CrawlJob).filter(CrawlJob.site == site_name).delete()
        s.query(Site).filter(Site.site == site_name).delete()
        s.add(Site(site=site_name, brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="none", track_status="error"))
        s.commit()
    finally:
        s.close()

    job_id = enqueue(site_name, trigger="admin_retry")

    s = SessionLocal()
    try:
        site = s.query(Site).filter(Site.site == site_name).one()
        job = s.get(CrawlJob, job_id)
        assert job.status == "pending"
        assert site.track_status == "tracking"
    finally:
        s.close()


def test_enqueue_failed_product_retry_does_not_restore_error_site_tracking():
    from app.runner import FAILED_PRODUCT_RETRY_TRIGGER

    init_db()
    site_name = "runner_url_retry_keeps_error"
    s = SessionLocal()
    try:
        s.query(CrawlJob).filter(CrawlJob.site == site_name).delete()
        s.query(Site).filter(Site.site == site_name).delete()
        s.add(Site(site=site_name, brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="none", track_status="error"))
        s.commit()
    finally:
        s.close()

    job_id = enqueue(site_name, trigger=FAILED_PRODUCT_RETRY_TRIGGER)

    s = SessionLocal()
    try:
        site = s.query(Site).filter(Site.site == site_name).one()
        job = s.get(CrawlJob, job_id)
        assert job.status == "pending"
        assert site.track_status == "error"
    finally:
        s.close()


def test_claim_job_skips_paused_tracking_site_and_claims_next():
    init_db()
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        for site_name in ("runner_paused_claim", "runner_ready_claim"):
            s.query(Site).filter(Site.site == site_name).delete()
        s.add(Site(site="runner_paused_claim", brand="Probe", country="US",
                   url="https://example.com/paused", platform="generic",
                   proxy_tier="none", track_status="paused"))
        s.add(Site(site="runner_ready_claim", brand="Probe", country="US",
                   url="https://example.com/ready", platform="generic",
                   proxy_tier="none"))
        s.flush()
        paused = CrawlJob(site="runner_paused_claim", status="pending",
                          trigger="daily_refresh", created_at=now)
        ready = CrawlJob(site="runner_ready_claim", status="pending",
                         trigger="daily_refresh",
                         created_at=now + timedelta(seconds=1))
        s.add_all([paused, ready])
        s.commit()
        paused_id = paused.id
        ready_id = ready.id
    finally:
        s.close()

    assert claim_job("worker-test") == ready_id

    s = SessionLocal()
    try:
        paused = s.get(CrawlJob, paused_id)
        ready = s.get(CrawlJob, ready_id)
        assert paused.status == "skipped"
        assert paused.failure_code == TRACKING_PAUSED
        assert ready.status == "running"
    finally:
        s.close()


def test_claim_job_respects_platform_running_limit(monkeypatch):
    init_db()
    monkeypatch.setenv("CRAWL_PLATFORM_RUNNING_LIMITS", "vidaxl:3")
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        for site_name in (
            "runner_vidaxl_active_a",
            "runner_vidaxl_active_b",
            "runner_vidaxl_active_c",
            "runner_vidaxl_pending",
            "runner_other_pending",
        ):
            s.query(Site).filter(Site.site == site_name).delete()
        for site_name in (
            "runner_vidaxl_active_a",
            "runner_vidaxl_active_b",
            "runner_vidaxl_active_c",
            "runner_vidaxl_pending",
        ):
            s.add(Site(site=site_name, brand="VidaXL", country="NL",
                       url="https://example.com", platform="vidaxl",
                       proxy_tier="none"))
        s.add(Site(site="runner_other_pending", brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="none"))
        s.flush()
        for site_name in (
            "runner_vidaxl_active_a",
            "runner_vidaxl_active_b",
            "runner_vidaxl_active_c",
        ):
            s.add(CrawlJob(site=site_name, status="running",
                           trigger="scheduled", created_at=now,
                           started_at=now, heartbeat_at=now))
        blocked = CrawlJob(site="runner_vidaxl_pending", status="pending",
                           trigger="scheduled", created_at=now)
        ready = CrawlJob(site="runner_other_pending", status="pending",
                         trigger="scheduled",
                         created_at=now + timedelta(seconds=1))
        s.add_all([blocked, ready])
        s.commit()
        blocked_id = blocked.id
        ready_id = ready.id
    finally:
        s.close()

    assert claim_job("worker-test") == ready_id

    s = SessionLocal()
    try:
        blocked = s.get(CrawlJob, blocked_id)
        ready = s.get(CrawlJob, ready_id)
        assert blocked.status == "pending"
        assert ready.status == "running"
    finally:
        s.close()


def test_platform_running_limit_does_not_starve_other_platforms(monkeypatch):
    from app.runner import claim_job

    init_db()
    monkeypatch.setenv("CRAWL_PLATFORM_RUNNING_LIMITS", "vidaxl:3")
    now = datetime.utcnow()
    s = SessionLocal()
    prefix = "runner_platform_starve_"
    try:
        s.query(CrawlJob).delete()
        s.query(Site).filter(Site.site.like(f"{prefix}%")).delete(
            synchronize_session=False)
        active_sites = [f"{prefix}active_{idx}" for idx in range(3)]
        blocked_sites = [f"{prefix}blocked_{idx:02d}" for idx in range(55)]
        ready_site = f"{prefix}generic"
        for site_name in active_sites + blocked_sites:
            s.add(Site(site=site_name, brand="VidaXL", country="NL",
                       url="https://example.com", platform="vidaxl",
                       proxy_tier="none"))
        s.add(Site(site=ready_site, brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="none"))
        s.flush()
        for site_name in active_sites:
            s.add(CrawlJob(site=site_name, status="running",
                           trigger="scheduled", created_at=now,
                           started_at=now, heartbeat_at=now))
        for site_name in blocked_sites:
            s.add(CrawlJob(site=site_name, status="pending",
                           trigger="scheduled", created_at=now))
        ready = CrawlJob(site=ready_site, status="pending",
                         trigger="scheduled", created_at=now)
        s.add(ready)
        s.commit()
        ready_id = ready.id
    finally:
        s.close()

    assert claim_job("worker-test") == ready_id

    s = SessionLocal()
    try:
        ready = s.get(CrawlJob, ready_id)
        blocked_count = (s.query(CrawlJob)
                         .filter(CrawlJob.site.like(f"{prefix}blocked_%"),
                                 CrawlJob.status == "pending")
                         .count())
        assert ready.status == "running"
        assert blocked_count == 55
    finally:
        s.close()


def test_enqueue_auto_job_skips_site_hidden_from_enabled_workspaces():
    init_db()
    s = SessionLocal()
    try:
        s.query(CrawlJob).delete()
        s.query(WorkspaceSite).filter(WorkspaceSite.site == "runner_hidden_probe").delete()
        s.query(Site).filter(Site.site == "runner_hidden_probe").delete()
        ws = Workspace(slug="runner-hidden-ws", name="Runner Hidden")
        s.add(ws)
        s.flush()
        s.add(Site(site="runner_hidden_probe", brand="Probe", country="US",
                   url="https://example.com", platform="generic",
                   proxy_tier="none"))
        s.add(WorkspaceSite(workspace_id=ws.id, site="runner_hidden_probe",
                            enabled=True, hidden=True))
        s.commit()
    finally:
        s.close()

    auto_job_id = enqueue("runner_hidden_probe", trigger="scheduled")
    manual_job_id = enqueue("runner_hidden_probe", trigger="admin_retry")

    s = SessionLocal()
    try:
        auto_job = s.get(CrawlJob, auto_job_id)
        manual_job = s.get(CrawlJob, manual_job_id)
        assert auto_job.status == "skipped"
        assert auto_job.failure_code == "workspace_hidden"
        assert auto_job.retryable is False
        assert manual_job.status == "pending"
        assert manual_job.failure_code is None
    finally:
        s.close()
