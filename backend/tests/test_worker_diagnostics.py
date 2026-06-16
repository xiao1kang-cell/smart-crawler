from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import SessionLocal, init_db
from app.models import CrawlFailure, CrawlJob
from app.worker import (
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

    _mark_job_timeout(job_id)

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
