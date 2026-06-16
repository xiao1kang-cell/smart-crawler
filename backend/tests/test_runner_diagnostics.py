from __future__ import annotations

import pytest

from app.crawl_diagnostics import FailureInfo, STAGE_JOB, record_failure
from app.crawlers.base import CrawlResult
from app.db import SessionLocal, init_db
from app.models import CrawlJob, Site
from app.runner import enqueue, execute_job

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
        assert job.status == "failed"
        assert job.failure_code == "network_timeout"
        assert job.products_count == 0
    finally:
        s.close()
