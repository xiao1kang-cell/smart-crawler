from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.crawlers.base import CrawlResult
from app.db import SessionLocal, init_db
from app.models import CrawlJob, CrawlUrl, Site
from app.runner import (
    _crawl_failed_product_retry,
    _crawl_total_from_result,
    enqueue,
    execute_job,
)

pytestmark = pytest.mark.unit


class _IncompleteCoverageCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        out = CrawlResult()
        out.products.append({
            "sku": "INC-1",
            "title": "Incomplete Product",
            "site": "runner_incomplete_coverage_probe",
            "product_url": "https://example.com/products/inc-1",
            "sale_price": 10,
        })
        out.total_product_count = 1
        out.coverage_complete = False
        out.coverage_code = "incomplete_discovery"
        out.coverage_stage = "discovery"
        out.coverage_reason = "only homepage pool was discovered"
        out.coverage_suggested_action = "configure a full feed"
        return out


class _UnchangedProductsCrawler:
    job_id: int | None = None

    def crawl(self) -> CrawlResult:
        out = CrawlResult()
        for sku in ("UNCHANGED-1", "UNCHANGED-2"):
            out.products.append({
                "sku": sku,
                "title": sku,
                "site": "runner_unchanged_products_probe",
                "product_url": f"https://example.com/products/{sku.lower()}",
                "sale_price": 10,
            })
        out.total_product_count = 2
        return out


class _RetryCaptureCrawler:
    job_id: int | None = None
    site = SimpleNamespace(crawler_config={})

    def __init__(self):
        self.urls: list[str] = []

    def crawl_failed_products(self, urls: list[str]) -> CrawlResult:
        self.urls = list(urls)
        out = CrawlResult()
        out.total_product_count = len(urls)
        return out


def test_execute_job_incomplete_coverage_becomes_partial(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(
            Site.site == "runner_incomplete_coverage_probe",
        ).first():
            s.add(Site(
                site="runner_incomplete_coverage_probe",
                brand="Probe",
                country="US",
                url="https://example.com",
                platform="generic",
                proxy_tier="none",
            ))
            s.commit()
    finally:
        s.close()
    monkeypatch.setattr(
        "app.runner.get_crawler",
        lambda site: _IncompleteCoverageCrawler(),
    )
    job_id = enqueue("runner_incomplete_coverage_probe")

    result = execute_job(job_id)

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert result["status"] == "partial"
        assert result["products"] == 1
        assert result["failure_code"] == "incomplete_discovery"
        assert result["error"] == "only homepage pool was discovered"
        assert result["suggested_action"] == "configure a full feed"
        assert job.status == "partial"
        assert job.products_count == 1
        assert job.total_product_count == 2
        assert job.success_rate == 50.0
        assert job.failure_stage == "discovery"
    finally:
        s.close()


def test_execute_job_counts_parsed_products_when_rows_unchanged(monkeypatch):
    init_db()
    s = SessionLocal()
    try:
        if not s.query(Site).filter(
            Site.site == "runner_unchanged_products_probe",
        ).first():
            s.add(Site(
                site="runner_unchanged_products_probe",
                brand="Probe",
                country="US",
                url="https://example.com",
                platform="generic",
                proxy_tier="none",
            ))
            s.commit()
    finally:
        s.close()
    monkeypatch.setattr(
        "app.runner.get_crawler",
        lambda site: _UnchangedProductsCrawler(),
    )

    first_job_id = enqueue("runner_unchanged_products_probe")
    first = execute_job(first_job_id)
    second_job_id = enqueue("runner_unchanged_products_probe")
    second = execute_job(second_job_id)

    s = SessionLocal()
    try:
        first_job = s.get(CrawlJob, first_job_id)
        second_job = s.get(CrawlJob, second_job_id)
        assert first["status"] == "success"
        assert second["status"] == "success"
        assert first_job.products_count == 2
        assert second_job.products_count == 2
        assert second["products"] == 2
        assert second_job.total_product_count == 2
        assert second_job.success_rate == 100.0
    finally:
        s.close()


def test_crawl_total_from_result_preserves_explicit_zero():
    result = CrawlResult()
    result.total_product_count = 0

    assert _crawl_total_from_result(result, fallback_count=9) == 0


def test_crawl_total_from_result_falls_back_only_when_missing():
    result = CrawlResult()
    result.total_product_count = None

    assert _crawl_total_from_result(result, fallback_count=9) == 9


def test_failed_product_retry_claims_only_failed_or_selected_pending():
    init_db()
    s = SessionLocal()
    try:
        rows = [
            CrawlUrl(
                site="retry_scope_probe",
                url_hash="failed",
                url="https://example.com/failed",
                kind="product",
                status="failed",
                attempts=1,
                priority=40,
            ),
            CrawlUrl(
                site="retry_scope_probe",
                url_hash="selected",
                url="https://example.com/selected",
                kind="product",
                status="pending",
                attempts=1,
                priority=10,
            ),
            CrawlUrl(
                site="retry_scope_probe",
                url_hash="fetched",
                url="https://example.com/fetched",
                kind="product",
                status="fetched",
                attempts=1,
                priority=5,
            ),
            CrawlUrl(
                site="retry_scope_probe",
                url_hash="unselected",
                url="https://example.com/unselected",
                kind="product",
                status="pending",
                attempts=1,
                priority=40,
            ),
        ]
        s.add_all(rows)
        s.commit()
    finally:
        s.close()

    crawler = _RetryCaptureCrawler()

    _crawl_failed_product_retry(crawler, 123, "retry_scope_probe")

    assert crawler.urls == [
        "https://example.com/selected",
        "https://example.com/failed",
    ]
