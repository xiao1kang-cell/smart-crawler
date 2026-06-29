from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.crawl_diagnostics import hash_url
from app.db import Base
from app.models import (CrawlFailure, CrawlJob, CrawlUrl, Site, User,
                        Workspace, WorkspaceMember, WorkspaceSite)


pytestmark = pytest.mark.unit


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def _seed(db):
    ws = Workspace(name="Internal", slug="internal", type="internal", status="active")
    db.add(ws)
    db.flush()
    user = User(
        username="admin",
        email="admin@example.com",
        password_hash=hash_password("Password1"),
        role="admin",
        global_role="super_admin",
        status="active",
        default_workspace_id=ws.id,
    )
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                           role="owner", status="active"))
    site = Site(site="costway_us", brand="Costway", country="US",
                url="https://www.costway.com", platform="vue_spa",
                proxy_tier="residential")
    db.add(site)
    db.flush()
    db.add(WorkspaceSite(workspace_id=ws.id, site=site.site, enabled=True,
                         hidden=False, sort_order=0))
    job = CrawlJob(site=site.site, status="failed", trigger="manual",
                   requested_by_workspace_id=ws.id, created_at=datetime.utcnow())
    db.add(job)
    db.flush()
    failed_url = "https://www.costway.com/api/products?category_id=1&page=1&pagesize=48"
    ok_url = "https://www.costway.com/api/products?category_id=2&page=1&pagesize=48"
    db.add(CrawlUrl(
        site=site.site,
        url_hash=hash_url(failed_url),
        url=failed_url,
        kind="product",
        source="costway",
        status="failed",
        failure_code="network_timeout",
        failure_stage="fetch",
        failure_detail="timed out",
        retryable=True,
        attempts=2,
        last_fetched_at=datetime.utcnow(),
    ))
    db.add(CrawlUrl(
        site=site.site,
        url_hash=hash_url(ok_url),
        url=ok_url,
        kind="product",
        source="costway",
        status="parsed",
        attempts=1,
    ))
    db.add(CrawlFailure(
        site=site.site,
        job_id=job.id,
        url=failed_url,
        stage="fetch",
        code="network_timeout",
        detail="timed out",
        retryable=True,
        occurred_at=datetime.utcnow(),
    ))
    db.commit()
    return ws, job, failed_url, ok_url


def test_list_failed_products_by_job_includes_url_detail():
    from app.api.routes import list_failed_products

    db = _session()
    ws, job, failed_url, _ok_url = _seed(db)

    payload = list_failed_products(
        site="costway_us",
        job_id=job.id,
        user="admin",
        x_workspace_id=str(ws.id),
        db=db,
    )

    assert payload["total"] == 1
    assert payload["items"][0]["url"] == failed_url
    assert payload["items"][0]["failure_code"] == "network_timeout"
    assert payload["items"][0]["attempts"] == 2


def test_list_failed_products_excludes_successful_fetched_urls():
    from app.api.routes import list_failed_products

    db = _session()
    ws, _job, failed_url, _ok_url = _seed(db)
    fetched_url = "https://www.costway.com/api/products?category_id=3&page=1&pagesize=48"
    db.add(CrawlUrl(
        site="costway_us",
        url_hash=hash_url(fetched_url),
        url=fetched_url,
        kind="product",
        source="costway",
        status="fetched",
        attempts=1,
        last_fetched_at=datetime.utcnow(),
    ))
    db.commit()

    payload = list_failed_products(
        site="costway_us",
        user="admin",
        x_workspace_id=str(ws.id),
        db=db,
    )

    assert payload["total"] == 1
    assert [item["url"] for item in payload["items"]] == [failed_url]


def test_retry_failed_products_resets_only_selected_urls(monkeypatch):
    from app.api import routes
    from app.runner import FAILED_PRODUCT_RETRY_TRIGGER

    db = _session()
    ws, job, failed_url, ok_url = _seed(db)
    enqueued: dict[str, object] = {}

    def fake_enqueue(site_name: str, trigger: str = "manual", **kwargs):
        enqueued.update({"site": site_name, "trigger": trigger, **kwargs})
        return 9001

    monkeypatch.setattr(routes, "enqueue", fake_enqueue)

    payload = routes.retry_failed_products(
        payload={"site": "costway_us", "job_id": job.id, "urls": [failed_url]},
        user="admin",
        x_workspace_id=str(ws.id),
        db=db,
    )

    failed_row = db.query(CrawlUrl).filter(CrawlUrl.url_hash == hash_url(failed_url)).one()
    ok_row = db.query(CrawlUrl).filter(CrawlUrl.url_hash == hash_url(ok_url)).one()
    assert payload["job_id"] == 9001
    assert payload["selected_count"] == 1
    assert enqueued["trigger"] == FAILED_PRODUCT_RETRY_TRIGGER
    assert failed_row.status == "pending"
    assert failed_row.failure_code is None
    assert failed_row.attempts == 0
    assert ok_row.status == "parsed"
