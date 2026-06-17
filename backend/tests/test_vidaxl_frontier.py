from __future__ import annotations

import uuid

import pytest

from app.crawlers.vidaxl import (
    _already_crawled_urls,
    _log_fetched,
    _record_site_exception,
    _register_frontier_targets,
)
from app.db import SessionLocal, init_db
from app.models import CrawlFailure, CrawlJob, CrawlUrl, Product

pytestmark = pytest.mark.unit


def _site() -> str:
    return f"vidaxl_probe_{uuid.uuid4().hex[:8]}"


def test_vidaxl_registers_run_targets_in_frontier():
    init_db()
    site = _site()

    _register_frontier_targets(site, [
        "https://example.com/p/1.html",
        "https://example.com/p/2.html",
    ])

    s = SessionLocal()
    try:
        rows = s.query(CrawlUrl).filter(CrawlUrl.site == site).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {"pending"}
        assert {r.source for r in rows} == {"vidaxl_sitemap"}
    finally:
        s.close()


def test_vidaxl_logs_blocked_product_fetch_to_frontier_and_failures():
    init_db()
    site = _site()
    url = "https://example.com/p/403.html"

    _log_fetched(site, url, 403, job_id=None)

    s = SessionLocal()
    try:
        row = s.query(CrawlUrl).filter(CrawlUrl.site == site,
                                       CrawlUrl.url == url).first()
        failure = (s.query(CrawlFailure)
                   .filter(CrawlFailure.site == site,
                           CrawlFailure.url == url)
                   .first())
        assert row is not None
        assert row.status == "blocked"
        assert row.failure_code == "http_403"
        assert failure is not None
        assert failure.code == "http_403"
    finally:
        s.close()


def test_vidaxl_logs_parse_none_as_parse_failure():
    init_db()
    site = _site()
    url = "https://example.com/p/no-jsonld.html"

    _log_fetched(site, url, 200, parse_failed=True)

    s = SessionLocal()
    try:
        row = s.query(CrawlUrl).filter(CrawlUrl.site == site,
                                       CrawlUrl.url == url).first()
        assert row is not None
        assert row.status == "failed"
        assert row.failure_code == "parse_no_jsonld"
    finally:
        s.close()


def test_vidaxl_already_crawled_urls_uses_frontier_and_products():
    init_db()
    site = _site()
    parsed = "https://example.com/p/parsed.html"
    blocked = "https://example.com/p/blocked.html"
    failed = "https://example.com/p/failed.html"
    product = "https://example.com/p/product.html"

    _log_fetched(site, parsed, 200, parsed=True)
    _log_fetched(site, blocked, 403)
    _log_fetched(site, failed, 200, parse_failed=True)

    s = SessionLocal()
    try:
        s.add(Product(site=site, sku="SKU-1", title="Product",
                      product_url=product))
        s.commit()
    finally:
        s.close()

    assert _already_crawled_urls(site) >= {parsed, blocked, failed, product}


def test_vidaxl_records_sitemap_timeout_as_job_failure():
    init_db()
    site = _site()
    s = SessionLocal()
    try:
        job = CrawlJob(site=site, status="running")
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    url = "https://example.com/sitemap_index.xml"
    _record_site_exception(
        site,
        job_id,
        url,
        TimeoutError("Connection timed out after 30002 milliseconds"),
    )

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        row = s.query(CrawlUrl).filter(CrawlUrl.site == site,
                                       CrawlUrl.url == url).first()
        assert job.failure_code == "network_timeout"
        assert row is not None
        assert row.kind == "sitemap"
        assert row.failure_code == "network_timeout"
    finally:
        s.close()
