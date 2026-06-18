from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import SessionLocal, init_db
from app.models import CrawlFailure, CrawlJob, Site
from app.worker import (
    DEFAULT_JOB_TIMEOUT,
    _job_runtime_budget,
    _mark_job_timeout,
    _reclaim_stale_crawl_jobs,
    _repair_missing_failure_diagnostics,
)

pytestmark = pytest.mark.unit


def test_worker_timeout_writes_structured_failure():
    init_db()
    s = SessionLocal()
    try:
        job = CrawlJob(site="timeout_probe", status="running",
                       started_at=datetime.utcnow())
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    _mark_job_timeout(job_id, 7200)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.job_id == job_id)
                   .order_by(CrawlFailure.id.desc())
                   .first())
        assert job.status == "failed"
        assert job.failure_code == "job_timeout"
        assert failure is not None
        assert failure.code == "job_timeout"
    finally:
        s.close()


def test_job_runtime_budget_uses_site_config_and_trigger_defaults():
    init_db()
    s = SessionLocal()
    try:
        configured = Site(
            site="large_site_budget",
            brand="Large",
            country="US",
            url="https://example.com",
            platform="generic",
            crawler_config={"job_timeout_sec": 21600},
        )
        s.merge(configured)
        job = CrawlJob(site="large_site_budget", status="pending",
                       trigger="scheduled")
        fallback_job = CrawlJob(site="no_site_budget", status="pending",
                                trigger="scheduled")
        s.add_all([job, fallback_job])
        s.commit()
        job_id = job.id
        fallback_id = fallback_job.id
    finally:
        s.close()

    assert _job_runtime_budget(job_id) == 21600
    assert _job_runtime_budget(fallback_id) == DEFAULT_JOB_TIMEOUT


def test_reclaim_stale_crawl_jobs_writes_job_timeout():
    init_db()
    s = SessionLocal()
    try:
        s.query(CrawlJob).filter(CrawlJob.status == "running").delete()
        s.commit()
        job = CrawlJob(site="stale_probe", status="running",
                       started_at=datetime.utcnow() - timedelta(seconds=3600))
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert _reclaim_stale_crawl_jobs(timeout_sec=1800) == 1

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.job_id == job_id)
                   .order_by(CrawlFailure.id.desc())
                   .first())
        assert job.status == "failed"
        assert job.failure_code == "job_timeout"
        assert failure is not None
        assert failure.code == "job_timeout"
    finally:
        s.close()


def test_reclaim_stale_crawl_jobs_keeps_fresh_heartbeat_running():
    init_db()
    s = SessionLocal()
    try:
        s.query(CrawlJob).filter(CrawlJob.status == "running").delete()
        s.commit()
        job = CrawlJob(
            site="fresh_heartbeat_probe",
            status="running",
            started_at=datetime.utcnow() - timedelta(seconds=3600),
            heartbeat_at=datetime.utcnow(),
        )
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert _reclaim_stale_crawl_jobs(timeout_sec=1800) == 0

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert job.status == "running"
    finally:
        s.close()


def test_repair_missing_failure_diagnostics_backfills_old_failed_jobs():
    init_db()
    s = SessionLocal()
    try:
        job = CrawlJob(site="old_failed_probe", status="failed",
                       error="auto-canceled: stuck running >30min",
                       created_at=datetime.utcnow(),
                       finished_at=datetime.utcnow())
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert _repair_missing_failure_diagnostics(limit=20) >= 1

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert job.failure_code == "job_timeout"
        assert job.suggested_action
    finally:
        s.close()


def test_repair_failure_diagnostics_reclassifies_unknown_jobs():
    init_db()
    s = SessionLocal()
    try:
        job = CrawlJob(
            site="sephora_unknown_probe",
            status="failed",
            error="worker exception: ValueError: 未知平台: sephora",
            failure_code="unknown",
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        s.add(job)
        s.commit()
        job_id = job.id
        before = s.query(CrawlFailure).filter(CrawlFailure.job_id == job_id).count()
    finally:
        s.close()

    assert _repair_missing_failure_diagnostics(limit=20) >= 1

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        after = s.query(CrawlFailure).filter(CrawlFailure.job_id == job_id).count()
        assert job.failure_code == "unsupported_platform"
        assert job.retryable is False
        assert after == before + 1
    finally:
        s.close()


def test_repair_failure_diagnostics_reclassifies_worker_interruptions_once():
    init_db()
    s = SessionLocal()
    try:
        job = CrawlJob(
            site="still_unknown_probe",
            status="failed",
            error="manual rerun interrupted after long-running crawl",
            failure_code="unknown",
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        s.add(job)
        s.commit()
        job_id = job.id
        before = s.query(CrawlFailure).filter(CrawlFailure.job_id == job_id).count()
    finally:
        s.close()

    assert _repair_missing_failure_diagnostics(limit=20) >= 1
    assert _repair_missing_failure_diagnostics(limit=20) == 0

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        after = s.query(CrawlFailure).filter(CrawlFailure.job_id == job_id).count()
        assert job.failure_code == "worker_interrupted"
        assert job.retryable is True
        assert after == before + 1
    finally:
        s.close()
