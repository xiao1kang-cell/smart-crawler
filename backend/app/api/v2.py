"""smart-crawler v2 API · Agent-first / Firecrawl-compatible surface.

The route layer is intentionally thin. Shared crawler behavior lives in
`app.agent_crawler`, so REST and MCP tools return the same schema.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..agent_crawler import (
    crawl_site,
    extract_structured_data,
    get_crawl_job,
    map_site as map_site_service,
    query_warehouse,
    scrape_url,
)
from ..agent_runtime import (
    agent_key_for_api_key,
    enrich_usage,
    run_with_agent_memory,
)
from ..access import (
    find_api_key,
    raw_key_from_headers,
    require_api_key_scope,
)
from ..billing import record_usage
from ..db import get_db
from ..models import ApiKey, Product, RateLimitEvent, Site
from .routes import require_user
from .. import spine


def _rate_limit_dependency(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> None:
    """Shared v2 rate limiter.

    Default backend is DB-backed so NAS / multi-worker deployments share the
    same one-minute window. Set V2_RATE_LIMIT_BACKEND=memory for throwaway local
    runs that should avoid DB writes.
    """
    key = _raw_auth_key(authorization, x_api_key) or (request.client.host if request.client else "unknown")
    path = request.url.path
    if path.endswith("/crawl") or "/batch/" in path:
        limit = int(os.environ.get("V2_CRAWL_RATE_LIMIT_PER_MIN", "10"))
    else:
        limit = int(os.environ.get("V2_RATE_LIMIT_PER_MIN", "120"))
    bucket_key = f"{key}:{path}"
    if os.environ.get("V2_RATE_LIMIT_BACKEND", "db").lower() == "memory":
        _memory_rate_limit(bucket_key, path, limit)
        return
    _db_rate_limit(db, bucket_key, path, limit)


def _memory_rate_limit(bucket_key: str, path: str, limit: int) -> None:
    now = time.time()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[bucket_key]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                429,
                {
                    "error": "rate_limited",
                    "message": f"Too many requests for {path}. Limit is {limit}/min.",
                    "next_step": "Retry after a short backoff or request a higher quota.",
                },
            )
        bucket.append(now)


def _db_rate_limit(db: Session, bucket_key: str, path: str, limit: int) -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    try:
        db.query(RateLimitEvent).filter(
            RateLimitEvent.occurred_at < cutoff - timedelta(seconds=60)
        ).delete(synchronize_session=False)
        count = db.query(RateLimitEvent).filter(
            RateLimitEvent.bucket_key == bucket_key,
            RateLimitEvent.occurred_at >= cutoff,
        ).count()
        if count >= limit:
            raise HTTPException(
                429,
                {
                    "error": "rate_limited",
                    "message": f"Too many requests for {path}. Limit is {limit}/min.",
                    "next_step": "Retry after a short backoff or request a higher quota.",
                },
            )
        db.add(RateLimitEvent(bucket_key=bucket_key, path=path))
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        _memory_rate_limit(bucket_key, path, limit)


router = APIRouter(
    prefix="/api/v2",
    dependencies=[Depends(require_user), Depends(_rate_limit_dependency)],
    tags=["v2 · Agent-first crawler API"],
)

_RATE_BUCKETS: dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()


class ScrapeRequest(BaseModel):
    url: str = Field(..., description="URL to scrape.")
    formats: list[str] = Field(
        default=["markdown", "structured"],
        description="markdown / structured / html / links",
    )
    only_main_content: bool = Field(default=True)
    wait_for: int = Field(default=0, description="Extra wait in milliseconds.")
    timeout: int = Field(default=30000, description="Timeout in milliseconds.")
    force_live: bool = Field(default=False, description="Bypass warehouse cache.")
    mode: str = Field(
        default="standard",
        description="standard / advanced (browser_pool rendered scrape).",
    )


class MapRequest(BaseModel):
    url: str = Field(..., description="Known site root URL.")
    limit: int = Field(default=1000, le=10000)
    include_subdomains: bool = Field(default=False)
    search: Optional[str] = None


class CrawlRequest(BaseModel):
    url: str = Field(..., description="Known site root URL.")
    limit: int = Field(default=1000, le=10000)
    dry_run: bool = Field(
        default=True,
        description="Default true: estimate and validate without queuing a crawl job.",
    )
    include_paths: list[str] = []
    exclude_paths: list[str] = []
    max_depth: int = 2
    poll_interval: int = 30


class ExtractRequest(BaseModel):
    urls: list[str] = Field(..., description="URLs to extract; max 25.")
    schema_: dict = Field(default_factory=dict, alias="schema")
    prompt: Optional[str] = None


class BatchScrapeRequest(BaseModel):
    urls: list[str] = Field(..., description="URLs to scrape; max 100.")
    formats: list[str] = Field(default=["markdown", "structured"])
    webhook: Optional[str] = None


class WarehouseQueryRequest(BaseModel):
    query: str = Field(..., description="Keyword/category/SKU query.")
    site: Optional[str] = None
    brand: Optional[str] = None
    limit: int = Field(default=20, le=200)


class CustomScrapeRequest(BaseModel):
    url: str
    dataset: str
    entity_type: str = "generic"
    schema_: Optional[dict] = Field(default=None, alias="schema")
    force_live: bool = False
    save_policy: str = "promote_if_valid"
    max_age_sec: Optional[int] = None

    model_config = {"populate_by_name": True}


class DatasetQueryRequest(BaseModel):
    dataset: str
    query: Optional[str] = None
    entity_type: Optional[str] = None
    include_staging: bool = False
    limit: int = 20


@router.post("/scrape")
def scrape(
    req: ScrapeRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Single URL scrape with warehouse-first behavior."""
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    key = _api_key_row(db, authorization, x_api_key)
    result = run_with_agent_memory(
        db,
        agent_key=agent_key_for_api_key(key.id if key else None),
        tool="scrape_url",
        payload={
            "url": req.url,
            "formats": req.formats,
            "wait_for": req.wait_for,
            "timeout": req.timeout,
            "mode": req.mode,
            "force_live": req.force_live,
        },
        cacheable=not req.force_live and req.mode == "standard",
        producer=lambda: scrape_url(
            db,
            req.url,
            formats=req.formats,
            wait_for_ms=req.wait_for,
            timeout_ms=req.timeout,
            force_live=req.force_live,
            mode=req.mode,
        ),
    )
    enrich_usage(db, result, api_key=key, default_cost_if_retry=3)
    _meter(db, authorization, x_api_key, "/api/v2/scrape", result)
    return result


@router.post("/map")
def map_site(
    req: MapRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Map a configured site to known product URLs from the warehouse."""
    _require_scope(db, authorization, x_api_key, "crawler:read")
    key = _api_key_row(db, authorization, x_api_key)
    result = map_site_service(db, req.url, limit=req.limit, search=req.search)
    enrich_usage(db, result, api_key=key)
    _meter(db, authorization, x_api_key, "/api/v2/map", result)
    if not result.get("success"):
        raise HTTPException(404, result)
    return result


@router.post("/crawl")
def crawl(
    req: CrawlRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Validate or queue a full-site crawl.

    The default is dry_run=true to protect users from accidental high-cost
    crawls. Set dry_run=false explicitly to enqueue.
    """
    _require_scope(
        db, authorization, x_api_key,
        "crawler:read" if req.dry_run else "crawler:crawl",
    )
    key = _api_key_row(db, authorization, x_api_key)
    result = crawl_site(db, req.url, limit=req.limit, dry_run=req.dry_run)
    enrich_usage(db, result, api_key=key)
    _meter(db, authorization, x_api_key, "/api/v2/crawl", result)
    if not result.get("success"):
        raise HTTPException(404, result)
    return result


@router.get("/crawl/{job_id}")
def crawl_status(
    job_id: int,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Poll a queued crawl job."""
    _require_scope(db, authorization, x_api_key, "crawler:read")
    key = _api_key_row(db, authorization, x_api_key)
    result = get_crawl_job(db, job_id)
    enrich_usage(db, result, api_key=key)
    _meter(db, authorization, x_api_key, "/api/v2/crawl/{job_id}", result)
    if not result.get("success"):
        raise HTTPException(404, result)
    return result


@router.post("/batch/scrape")
def batch_scrape(
    req: BatchScrapeRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Batch scrape as a group of single URL calls.

    The MVP returns inline results for the first 100 URLs. A webhook-backed async
    batch can be layered on top of the same service later.
    """
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    key = _api_key_row(db, authorization, x_api_key)
    if len(req.urls) > 100:
        raise HTTPException(400, "Max 100 URLs per batch")
    batch_id = "batch_" + uuid.uuid4().hex[:16]
    items = [
        scrape_url(db, url, formats=req.formats)
        for url in req.urls
    ]
    result = {
        "success": True,
        "batch_id": batch_id,
        "total": len(items),
        "items": items,
        "webhook": req.webhook,
        "usage": {
            "credits_used": sum((i.get("usage") or {}).get("credits_used", 0) for i in items),
            "records": len(items),
            "source": "mixed",
        },
        "warnings": [],
    }
    enrich_usage(db, result, api_key=key, default_cost_if_retry=3)
    _meter(db, authorization, x_api_key, "/api/v2/batch/scrape", result)
    return result


@router.post("/extract")
def extract(
    req: ExtractRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Extract structured fields from URLs.

    The MVP uses warehouse/live structured data and shapes it to the requested
    JSON schema. LLM extraction can be added as a final fallback.
    """
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    key = _api_key_row(db, authorization, x_api_key)
    result = run_with_agent_memory(
        db,
        agent_key=agent_key_for_api_key(key.id if key else None),
        tool="extract_structured_data",
        payload={"urls": req.urls, "schema": req.schema_, "prompt": req.prompt},
        producer=lambda: extract_structured_data(
            db,
            req.urls,
            req.schema_,
            instruction=req.prompt,
        ),
    )
    enrich_usage(db, result, api_key=key, default_cost_if_retry=3)
    _meter(db, authorization, x_api_key, "/api/v2/extract", result)
    return result


@router.post("/query")
def query(
    req: WarehouseQueryRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Query smart-crawler's warehouse before spending live scrape credits."""
    _require_scope(db, authorization, x_api_key, "crawler:read")
    key = _api_key_row(db, authorization, x_api_key)
    result = run_with_agent_memory(
        db,
        agent_key=agent_key_for_api_key(key.id if key else None),
        tool="query_warehouse",
        payload={
            "query": req.query,
            "site": req.site,
            "brand": req.brand,
            "limit": req.limit,
        },
        producer=lambda: query_warehouse(
            db,
            req.query,
            site=req.site,
            brand=req.brand,
            limit=req.limit,
        ),
    )
    enrich_usage(db, result, api_key=key)
    _meter(db, authorization, x_api_key, "/api/v2/query", result)
    return result


@router.get("/sources")
def list_sources(
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """List all configured data sources with warehouse counts."""
    _require_scope(db, authorization, x_api_key, "crawler:read")
    key = _api_key_row(db, authorization, x_api_key)
    sku_counts = dict(db.query(Product.site, func.count(Product.id))
                        .group_by(Product.site).all())
    data = []
    for site in db.query(Site).all():
        sku_count = sku_counts.get(site.site, 0)
        data.append({
            "site": site.site,
            "crawl_url": site.url or "",
            "brand": site.brand or "",
            "country": site.country or "",
            "platform": site.platform or "",
            "sku_count": sku_count,
            "coverage_pct": 100.0 if sku_count else 0.0,
            "status": "healthy" if sku_count > 0 else "empty",
            "last_crawled": site.last_crawled.isoformat() if site.last_crawled else None,
            "proxy_tier": site.proxy_tier or "none",
            "anti_bot_level": _anti_bot_level(site.platform or "generic"),
        })
    result = {
        "success": True,
        "object": "list",
        "count": len(data),
        "data": data,
        "usage": {"credits_used": 1, "records": len(data), "source": "warehouse"},
        "warnings": [],
    }
    enrich_usage(db, result, api_key=key)
    _meter(db, authorization, x_api_key, "/api/v2/sources", result)
    return result


@router.get("/")
def v2_root():
    """v2 API index."""
    return {
        "service": "smart-crawler",
        "version": "v2.1-agent-first",
        "compatible_with": "Firecrawl-style scrape/map/crawl/extract",
        "auth": "Authorization: Bearer sck_... or X-API-Key: sck_...",
        "principles": [
            "warehouse-first",
            "stable schema",
            "natural-language errors",
            "usage transparency",
        ],
        "endpoints": {
            "POST /api/v2/scrape": "Single URL -> markdown/structured/html/links",
            "POST /api/v2/map": "Known source URL -> warehouse URL list",
            "POST /api/v2/crawl": "Known source URL -> async crawl job",
            "GET /api/v2/crawl/{id}": "Crawl job status",
            "POST /api/v2/batch/scrape": "Inline batch scrape, max 100",
            "POST /api/v2/extract": "Shape structured data to a JSON schema",
            "POST /api/v2/query": "Warehouse search before live scrape",
            "GET /api/v2/sources": "All configured sources",
        },
        "docs": "https://smartcrawler.io/d/api_v2_spec.html",
    }


def _v2_ws_id(db, authorization, x_api_key) -> int | None:
    row = _api_key_row(db, authorization, x_api_key)
    return row.workspace_id if row else None


@router.post("/custom/scrape")
def custom_scrape(req: CustomScrapeRequest,
                  authorization: str = Header(default=""),
                  x_api_key: str = Header(default="", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    """通用数据采集:任意 URL → warehouse-first 抓取 → 带 provenance 入指定 dataset。"""
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    ws = _v2_ws_id(db, authorization, x_api_key)
    ds = spine.get_or_create_dataset(db, req.dataset, workspace_id=ws,
                                     entity_type=req.entity_type)
    out = spine.resolve(db, req.url, ds, workspace_id=ws, force_live=req.force_live,
                        max_age_sec=req.max_age_sec, save_policy=req.save_policy)
    if req.schema_ and out.get("data"):
        from ..agent_crawler import _shape_to_schema
        out["data"] = _shape_to_schema(out["data"], req.schema_)
    _meter(db, authorization, x_api_key, "/api/v2/custom/scrape", out)
    return out


@router.post("/dataset/query")
def dataset_query(req: DatasetQueryRequest,
                  authorization: str = Header(default=""),
                  x_api_key: str = Header(default="", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    """查通用数据集(extracted_records)。默认只返 main;include_staging=true 带 staging。"""
    _require_scope(db, authorization, x_api_key, "crawler:read")
    ws = _v2_ws_id(db, authorization, x_api_key)
    ds = spine.get_or_create_dataset(db, req.dataset, workspace_id=ws)
    out = spine.query_dataset(db, ds, query=req.query, entity_type=req.entity_type,
                              include_staging=req.include_staging, limit=req.limit)
    _meter(db, authorization, x_api_key, "/api/v2/dataset/query", out)
    return out


def _raw_auth_key(authorization: str, x_api_key: str) -> str:
    return raw_key_from_headers(authorization, x_api_key)


def _api_key_row(db: Session, authorization: str, x_api_key: str) -> ApiKey | None:
    raw = _raw_auth_key(authorization, x_api_key)
    if not raw:
        return None
    return find_api_key(db, raw)


def _require_scope(db: Session, authorization: str, x_api_key: str,
                   required: str) -> None:
    require_api_key_scope(_api_key_row(db, authorization, x_api_key), required)


def _meter(db: Session, authorization: str, x_api_key: str,
           endpoint: str, result: dict) -> None:
    key = _api_key_row(db, authorization, x_api_key)
    if not key:
        return
    usage = result.get("usage") or {}
    record_count = int(usage.get("records") or _infer_records(result))
    duration_ms = int(usage.get("duration_ms") or 0)
    try:
        bytes_returned = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        bytes_returned = 0
    try:
        record_usage(
            api_key_id=key.id,
            endpoint=endpoint,
            record_count=record_count,
            credits_used=int(usage.get("credits_used") or record_count),
            bytes_returned=bytes_returned,
            duration_ms=duration_ms,
        )
    except Exception:
        # Metering must never break the data API.
        pass


def _infer_records(result: dict) -> int:
    if isinstance(result.get("items"), list):
        return len(result["items"])
    if isinstance(result.get("data"), list):
        return len(result["data"])
    if result.get("data"):
        return 1
    return int(result.get("count") or result.get("total") or 0)


def _anti_bot_level(platform: str) -> int:
    levels = {
        "shopify": 1, "generic": 2, "vue_spa": 2, "nuxt": 2,
        "magento": 2, "shoper": 2, "vonhaus": 2, "woltu": 2,
        "flexispot": 2, "overstock": 2, "article": 1,
        "westelm": 2, "cratebarrel": 2, "ikea": 3, "bol": 3,
        "cdiscount": 3, "otto": 3, "vidaxl": 4, "idealo": 4,
        "wayfair": 5, "allegro": 5, "ebay": 5, "houzz": 3,
    }
    return levels.get(platform, 2)
