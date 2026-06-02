from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent_crawler import (
    crawl_site,
    extract_links,
    extract_metadata,
    extract_product_like_data,
    html_to_markdown,
    scrape_url,
)
from app.agent_runtime import enrich_usage, run_with_agent_memory
from app.db import Base
from app.models import ApiKey, Product, Site, Usage


pytestmark = pytest.mark.unit


FIXTURE = Path(__file__).parent / "fixtures" / "agent_product.html"


def test_extract_product_from_jsonld_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    metadata = extract_metadata(html, "https://example.com/products/patio-chair")
    data = extract_product_like_data(
        html, "https://example.com/products/patio-chair", metadata)

    assert metadata["title"] == "Acme Patio Chair - Black"
    assert metadata["image"] == "https://example.com/images/chair.jpg"
    assert data["sku"] == "CHAIR-001"
    assert data["sale_price"] == 49.99
    assert data["currency"] == "USD"
    assert data["brand"] == "Acme"


def test_html_to_markdown_and_links_are_stable():
    html = FIXTURE.read_text(encoding="utf-8")
    metadata = extract_metadata(html, "https://example.com/products/patio-chair")
    markdown = html_to_markdown(html, metadata)
    links = extract_links(html, "https://example.com/products/patio-chair")

    assert markdown.startswith("# Acme Patio Chair - Black")
    assert "Comfortable chair" in markdown
    assert links == ["https://example.com/products/table"]


def test_scrape_url_hits_warehouse_before_live_fetch(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Site(site="example_us", brand="Example", country="US",
                url="https://example.com/", platform="generic",
                proxy_tier="none"))
    db.add(Product(site="example_us", brand="Example", sku="CHAIR-001",
                   title="Warehouse Chair",
                   product_url="https://example.com/products/patio-chair",
                   sale_price=42.0, currency="USD",
                   updated_time=datetime.utcnow()))
    db.commit()

    def fail_live(*_, **__):
        raise AssertionError("live scrape should not run on warehouse hit")

    monkeypatch.setattr("app.agent_crawler.live_scrape_url", fail_live)
    result = scrape_url(db, "https://example.com/products/patio-chair")

    assert result["success"] is True
    assert result["usage"]["source"] == "warehouse"
    assert result["usage"]["credits_used"] == 0
    assert result["usage"]["cache_hit"] is True
    assert result["data"]["title"] == "Warehouse Chair"


def test_agent_memory_returns_zero_credit_cache_hit():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {
            "success": True,
            "data": {"title": "Cached Chair"},
            "usage": {
                "credits_used": 2,
                "cache_hit": False,
                "source": "live",
                "records": 1,
                "duration_ms": 1,
            },
            "warnings": [],
        }

    first = run_with_agent_memory(
        db, agent_key="apikey:1", tool="scrape_url",
        payload={"url": "https://example.com/products/chair"},
        producer=producer,
    )
    second = run_with_agent_memory(
        db, agent_key="apikey:1", tool="scrape_url",
        payload={"url": "https://example.com/products/chair"},
        producer=producer,
    )

    assert calls["n"] == 1
    assert first["usage"]["credits_used"] == 2
    assert second["usage"]["credits_used"] == 0
    assert second["usage"]["cache_hit"] is True
    assert second["usage"]["source"] == "agent_memory"


def test_crawl_site_defaults_to_dry_run_without_enqueue():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Site(site="example_us", brand="Example", country="US",
                url="https://example.com/", platform="generic",
                proxy_tier="none"))
    db.commit()

    result = crawl_site(db, "https://example.com/", limit=500)

    assert result["success"] is True
    assert result["status"] == "dry_run"
    assert result["job_id"] is None
    assert result["usage"]["credits_used"] == 0
    assert result["warnings"][0]["code"] == "dry_run_only"


def test_query_warehouse_is_free_and_errors_have_next_step():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()

    from app.agent_crawler import query_warehouse

    result = query_warehouse(db, "missing product", limit=5)
    assert result["usage"]["source"] == "warehouse"
    assert result["usage"]["credits_used"] == 0

    failed = {
        "success": False,
        "metadata": {"error": "HTTP 403"},
        "usage": {"credits_used": 0, "records": 0},
        "warnings": [],
    }
    enrich_usage(db, failed, default_cost_if_retry=3)
    assert failed["usage"]["cost_if_retry"] == 3
    assert failed["warnings"][0]["next_step"]


def test_zero_monthly_credit_quota_is_not_defaulted():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    key = ApiKey(id=7, name="zero", key_prefix="sck_x",
                 key_hash="hash", active=True, monthly_credit_quota=0)
    db.add(key)
    db.add(Usage(api_key_id=7, endpoint="/api/v2/scrape",
                 credits_used=1, record_count=1))
    db.commit()

    result = {"success": True, "usage": {"credits_used": 0}}
    enrich_usage(db, result, api_key=key)

    assert result["usage"]["balance"] == 0
