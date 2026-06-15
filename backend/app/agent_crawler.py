"""Agent-first crawler service layer.

This module is the shared implementation behind `/api/v2/*` and MCP crawler
tools. It keeps the product promise simple: answer from the warehouse first,
then fall back to a light live scrape when the warehouse cannot answer.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import CrawlJob, Product, Site


MAX_LIVE_HTML_CHARS = 300_000
ADVANCED_SCRAPE_CREDITS = 3


@dataclass
class UsageInfo:
    credits_used: int = 1
    cache_hit: bool = False
    source: str = "warehouse"  # warehouse / live / queued / unsupported
    duration_ms: int = 0
    records: int = 0
    api_calls: int = 0
    browser_opens: int = 0

    def to_dict(self) -> dict:
        return {
            "credits_used": self.credits_used,
            "cache_hit": self.cache_hit,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "records": self.records,
            "api_calls": self.api_calls,
            "browser_opens": self.browser_opens,
        }


def _now_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def match_site(db: Session, url: str) -> Site | None:
    """Match a URL to a configured site by root URL."""
    if not url:
        return None
    url_lower = url.lower().rstrip("/")
    for site in db.query(Site).all():
        if site.url and url_lower.startswith(site.url.lower().rstrip("/")):
            return site
    return None


def product_to_schema(p: Product, site: Site | None = None) -> dict:
    """Stable external product schema for Agent/API responses."""
    return {
        "site": p.site,
        "site_url": site.url if site else None,
        "sku": p.sku,
        "spu": p.spu,
        "title": p.title or "",
        "description": p.description,
        "image_urls": p.image_urls or [],
        "category_path": p.category_path,
        "sale_price": p.sale_price,
        "original_price": p.original_price,
        "currency": p.currency,
        "status": p.status,
        "ratings": p.ratings,
        "review_count": p.review_count,
        "brand": p.brand,
        "product_url": p.product_url or "",
        "identifiers": {
            "mpn": p.mpn,
            "gtin": p.gtin,
            "variant_id": p.variant_id,
        },
        "flags": {
            "is_new": bool(p.is_new),
            "is_bestseller": bool(p.is_bestseller),
            "has_video": bool(p.has_video),
            "has_free_shipping": bool(p.has_free_shipping),
        },
        "crawled_at": p.updated_time.isoformat() if p.updated_time else None,
        "confidence": 1.0,
    }


def scrape_url(
    db: Session,
    url: str,
    *,
    formats: list[str] | None = None,
    wait_for_ms: int = 0,
    timeout_ms: int = 30_000,
    force_live: bool = False,
    mode: str = "standard",
) -> dict:
    """Scrape one URL with warehouse-first behavior."""
    started = time.perf_counter()
    formats = formats or ["markdown", "structured"]
    scrape_id = "scr_" + uuid.uuid4().hex[:16]
    site = match_site(db, url)
    advanced_mode = (mode or "standard").lower() == "advanced"

    if site and not force_live and not advanced_mode:
        product = (db.query(Product)
                   .filter(Product.site == site.site,
                           Product.product_url == url)
                   .first())
        if product:
            data = product_to_schema(product, site)
            usage = UsageInfo(
                credits_used=0, cache_hit=True, source="warehouse",
                duration_ms=_now_ms(started), records=1,
            )
            return {
                "success": True,
                "url": url,
                "crawl_url": url,
                "site": site.site,
                "scrape_id": scrape_id,
                "metadata": {
                    "site": site.site,
                    "brand": site.brand,
                    "platform": site.platform,
                    "country": site.country,
                },
                "data": data if "structured" in formats else None,
                "markdown": _product_markdown(data) if "markdown" in formats else None,
                "html": None,
                "links": [],
                "usage": usage.to_dict(),
                "warnings": [],
            }

    live = (
        advanced_scrape_url(url, wait_for_ms=wait_for_ms, timeout_ms=timeout_ms)
        if advanced_mode
        else live_scrape_url(url, wait_for_ms=wait_for_ms, timeout_ms=timeout_ms)
    )
    if live["success"]:
        usage = UsageInfo(
            credits_used=ADVANCED_SCRAPE_CREDITS if advanced_mode else 2,
            cache_hit=False,
            source="advanced" if advanced_mode else "live",
            duration_ms=_now_ms(started), records=1,
            api_calls=0 if advanced_mode else 1,
            browser_opens=1 if advanced_mode else 0,
        )
        return {
            "success": True,
            "url": url,
            "crawl_url": live["crawl_url"],
            "site": site.site if site else None,
            "scrape_id": scrape_id,
            "metadata": live["metadata"],
            "data": live["structured"] if "structured" in formats else None,
            "markdown": live["markdown"] if "markdown" in formats else None,
            "html": live["html"] if "html" in formats else None,
            "links": live["links"] if "links" in formats else [],
            "usage": usage.to_dict(),
            "warnings": live.get("warnings", []),
        }

    if advanced_mode:
        usage = UsageInfo(
            credits_used=0, cache_hit=False, source="advanced",
            duration_ms=_now_ms(started), records=0,
        )
        return {
            "success": False,
            "url": url,
            "crawl_url": None,
            "site": site.site if site else None,
            "scrape_id": scrape_id,
            "metadata": {"error": live.get("error") or "advanced_scrape_failed"},
            "data": None,
            "markdown": None,
            "html": None,
            "links": [],
            "usage": usage.to_dict(),
            "warnings": live.get("warnings") or [{
                "code": "advanced_scrape_failed",
                "message": "Advanced browser scrape failed before usable page content was returned.",
                "next_step": (
                    "Call query_warehouse for cached data, retry standard scrape, "
                    "or verify the browser_pool / Playwright runtime on this host."
                ),
                "cost_if_retry": ADVANCED_SCRAPE_CREDITS,
            }],
        }

    if site:
        from .runner import enqueue

        job_id = enqueue(site.site, trigger="v2_scrape")
        usage = UsageInfo(
            credits_used=1, cache_hit=False, source="queued",
            duration_ms=_now_ms(started), records=0,
        )
        return {
            "success": True,
            "url": url,
            "crawl_url": url,
            "site": site.site,
            "scrape_id": scrape_id,
            "metadata": {"queued_job": job_id, "reason": live.get("error")},
            "data": None,
            "markdown": None,
            "html": None,
            "links": [],
            "usage": usage.to_dict(),
            "warnings": [{
                "code": "queued_after_live_scrape_failed",
                "message": "Live scrape failed, but the URL belongs to a supported site. A crawl job was queued.",
                "next_step": f"Poll /api/v2/crawl/{job_id}, or query the warehouse while the crawl runs.",
                "cost_if_retry": 3,
            }],
        }

    usage = UsageInfo(
        credits_used=0, cache_hit=False, source="unsupported",
        duration_ms=_now_ms(started), records=0,
    )
    return {
        "success": False,
        "url": url,
        "crawl_url": None,
        "site": None,
        "scrape_id": scrape_id,
        "metadata": {"error": live.get("error") or "unsupported_url"},
        "data": None,
        "markdown": None,
        "html": None,
        "links": [],
        "usage": usage.to_dict(),
        "warnings": [{
            "code": "unsupported_url",
            "message": "The URL is not in the configured source list and live scrape failed.",
            "next_step": (
                "Call query_crawler_warehouse for cached alternatives. "
                "If this is a new source, add it to sites.yaml before crawling."
            ),
            "cost_if_retry": 3,
        }],
    }


def map_site(
    db: Session,
    url: str,
    *,
    limit: int = 1000,
    search: str | None = None,
) -> dict:
    started = time.perf_counter()
    site = match_site(db, url)
    if not site:
        return {
            "success": False,
            "url": url,
            "site": None,
            "links": [],
            "count": 0,
            "usage": UsageInfo(credits_used=0, source="unsupported",
                               duration_ms=_now_ms(started)).to_dict(),
            "warnings": [{
                "code": "unsupported_site",
                "message": "The URL does not match any configured source.",
                "next_step": "Add the source to sites.yaml before crawling it at scale.",
            }],
        }
    q = db.query(Product.product_url).filter(Product.site == site.site)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Product.title.ilike(like),
                         Product.product_url.ilike(like)))
    links = [row[0] for row in q.limit(max(1, min(limit, 10_000))).all() if row[0]]
    return {
        "success": True,
        "url": url,
        "site": site.site,
        "links": links,
        "count": len(links),
        "usage": UsageInfo(credits_used=0, cache_hit=True, source="warehouse",
                           duration_ms=_now_ms(started),
                           records=len(links)).to_dict(),
        "warnings": [],
    }


def crawl_site(db: Session, url: str, *, limit: int = 1000,
               dry_run: bool = True) -> dict:
    started = time.perf_counter()
    site = match_site(db, url)
    if not site:
        return {
            "success": False,
            "job_id": None,
            "status": "unsupported",
            "site": None,
            "crawl_url": url,
            "poll_url": None,
            "usage": UsageInfo(credits_used=0, source="unsupported",
                               duration_ms=_now_ms(started)).to_dict(),
            "warnings": [{
                "code": "unsupported_site",
                "message": "The URL does not match any configured source.",
                "next_step": "Add a Site entry before starting a full crawl.",
            }],
        }
    credits = max(1, min(limit, 10_000))
    if dry_run:
        return {
            "success": True,
            "job_id": None,
            "status": "dry_run",
            "site": site.site,
            "crawl_url": url,
            "total": None,
            "poll_url": None,
            "usage": UsageInfo(credits_used=0, source="dry_run",
                               duration_ms=_now_ms(started)).to_dict(),
            "warnings": [{
                "code": "dry_run_only",
                "message": "This was a dry run. No crawl job was queued.",
                "next_step": (
                    f"Call crawl_site with dry_run=false to queue a crawl. "
                    f"Estimated credits: {credits}."
                ),
            }],
        }

    from .runner import enqueue

    job_id = enqueue(site.site, trigger="v2_crawl")
    return {
        "success": True,
        "job_id": job_id,
        "status": "pending",
        "site": site.site,
        "crawl_url": url,
        "total": None,
        "poll_url": f"/api/v2/crawl/{job_id}",
        "usage": UsageInfo(credits_used=credits, source="queued",
                           duration_ms=_now_ms(started)).to_dict(),
        "warnings": [],
    }


def get_crawl_job(db: Session, job_id: int) -> dict:
    job = db.get(CrawlJob, job_id)
    if not job:
        return {
            "success": False,
            "job_id": job_id,
            "status": "not_found",
            "warnings": [{"code": "job_not_found", "message": "Job not found."}],
        }
    site = db.query(Site).filter(Site.site == job.site).first()
    data = []
    if job.status == "success":
        products = (db.query(Product)
                    .filter(Product.site == job.site)
                    .limit(100).all())
        data = [product_to_schema(p, site) for p in products]
    return {
        "success": True,
        "job_id": job_id,
        "status": job.status,
        "site": job.site,
        "crawl_url": site.url if site else "",
        "total": job.products_count,
        "products_count": job.products_count,
        "duration_sec": job.duration_sec,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "data": data,
        "usage": UsageInfo(credits_used=0, cache_hit=True, source="warehouse",
                           records=len(data)).to_dict(),
        "warnings": [],
    }


def query_warehouse(
    db: Session,
    query: str,
    *,
    site: str | None = None,
    brand: str | None = None,
    limit: int = 20,
) -> dict:
    started = time.perf_counter()
    q = db.query(Product)
    if site:
        q = q.filter(Product.site == site)
    if brand:
        q = q.filter(Product.brand.ilike(f"%{brand}%"))
    if query:
        like = f"%{query}%"
        q = q.filter(or_(Product.title.ilike(like),
                         Product.sku.ilike(like),
                         Product.category_path.ilike(like)))
    total = q.count()
    rows = q.order_by(Product.updated_time.desc()).limit(max(1, min(limit, 200))).all()
    return {
        "success": True,
        "query": query,
        "total": total,
        "returned": len(rows),
        "items": [product_to_schema(p, None) for p in rows],
        "usage": UsageInfo(credits_used=0, cache_hit=True, source="warehouse",
                           duration_ms=_now_ms(started),
                           records=len(rows)).to_dict(),
        "warnings": [],
    }


def extract_structured_data(
    db: Session,
    urls: list[str],
    schema: dict[str, Any] | None = None,
    *,
    instruction: str | None = None,
) -> dict:
    started = time.perf_counter()
    items = []
    credits = 0
    api_calls = 0
    browser_opens = 0
    for url in urls[:25]:
        scraped = scrape_url(db, url, formats=["structured", "markdown"])
        u = scraped.get("usage") or {}
        credits += int(u.get("credits_used") or 0)
        api_calls += int(u.get("api_calls") or 0)
        browser_opens += int(u.get("browser_opens") or 0)
        data = scraped.get("data") or {}
        items.append({
            "url": url,
            "success": bool(scraped.get("success")),
            "source": u.get("source"),
            "data": _shape_to_schema(data, schema or {}),
            "warnings": scraped.get("warnings", []),
        })
    return {
        "success": True,
        "items": items,
        "schema": schema or {},
        "instruction": instruction,
        "usage": UsageInfo(credits_used=credits,
                           source="mixed",
                           duration_ms=_now_ms(started),
                           records=len(items),
                           api_calls=api_calls,
                           browser_opens=browser_opens).to_dict(),
        "warnings": [],
    }


def live_scrape_url(
    url: str,
    *,
    wait_for_ms: int = 0,
    timeout_ms: int = 30_000,
) -> dict:
    if wait_for_ms > 0:
        time.sleep(min(wait_for_ms, 10_000) / 1000)
    try:
        from curl_cffi import requests as creq
        resp = creq.get(url, timeout=max(1, timeout_ms / 1000),
                        impersonate="chrome")
        status = int(resp.status_code)
        html = resp.text or ""
        final_url = str(resp.url) if getattr(resp, "url", None) else url
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    if status >= 400:
        return {"success": False, "error": f"HTTP {status}", "status_code": status}
    html = html[:MAX_LIVE_HTML_CHARS]
    metadata = extract_metadata(html, final_url)
    structured = extract_product_like_data(html, final_url, metadata)
    return {
        "success": True,
        "crawl_url": final_url,
        "status_code": status,
        "metadata": metadata,
        "structured": structured,
        "markdown": html_to_markdown(html, metadata),
        "html": html,
        "links": extract_links(html, final_url, limit=200),
        "warnings": [] if structured else [{
            "code": "no_structured_product",
            "message": "Fetched the page, but no product-like structured data was detected.",
            "next_step": "Call extract_structured_data with an explicit schema, or add a site-specific parser.",
        }],
    }


def advanced_scrape_url(
    url: str,
    *,
    wait_for_ms: int = 0,
    timeout_ms: int = 30_000,
) -> dict:
    """Fetch one page through browser_pool, offloading when inside asyncio."""
    if _has_running_asyncio_loop():
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(
                _advanced_scrape_url_browser_pool,
                url,
                wait_for_ms=wait_for_ms,
                timeout_ms=timeout_ms,
            ).result()
    return _advanced_scrape_url_browser_pool(
        url,
        wait_for_ms=wait_for_ms,
        timeout_ms=timeout_ms,
    )


def _advanced_scrape_url_browser_pool(
    url: str,
    *,
    wait_for_ms: int = 0,
    timeout_ms: int = 30_000,
) -> dict:
    """Fetch one page through browser_pool using the local Playwright vendor.

    The helper is intentionally scoped to the Agent crawler surface. It borrows
    a short-lived browser session, renders the page, extracts the same fields as
    `live_scrape_url`, and always releases the session before returning.
    """
    session_id = None
    pool = None
    context = None
    try:
        from app.browser_pool.cluster import ClusterManager
        from app.browser_pool.pool import BrowserPool
        from app.browser_pool.vendors.local_playwright import (
            LocalPlaywrightVendor,
            PlaywrightNotInstalled,
        )
        from app.browser_pool.base import PoolExhausted
    except Exception as exc:
        return _advanced_failure(
            "browser_pool_unavailable",
            f"{type(exc).__name__}: {exc}",
            "Install backend browser dependencies, then retry with mode='advanced'.",
        )

    vendor = LocalPlaywrightVendor(
        concurrent_limit=int(os.environ.get("ADVANCED_SCRAPE_BROWSER_CONCURRENCY", "2")),
        headless=os.environ.get("ADVANCED_SCRAPE_HEADLESS", "1").lower()
        not in {"0", "false", "no"},
    )
    registry = {vendor.name: vendor}
    cluster = ClusterManager(registry=registry)
    pool = BrowserPool(registry=registry, cluster=cluster)
    try:
        session = pool.borrow(
            [vendor.name],
            ttl_seconds=max(30, int(timeout_ms / 1000) + 15),
        )
        session_id = session.session_id
        browser = vendor.get_browser(session_id)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=max(1, timeout_ms),
        )
        if wait_for_ms > 0:
            page.wait_for_timeout(min(wait_for_ms, 10_000))
        try:
            page.wait_for_load_state("networkidle", timeout=min(5_000, max(1, timeout_ms)))
        except Exception:
            pass
        status = int(response.status) if response is not None else 200
        final_url = page.url or url
        html = (page.content() or "")[:MAX_LIVE_HTML_CHARS]
    except PlaywrightNotInstalled as exc:
        return _advanced_failure(
            "playwright_not_installed",
            str(exc),
            "Install Playwright and browser binaries, then retry with mode='advanced'.",
        )
    except PoolExhausted as exc:
        error = str(exc)
        if "playwright install" in error or "Executable doesn't exist" in error:
            return _advanced_failure(
                "playwright_browser_missing",
                error,
                "Run `backend/.venv/bin/python -m playwright install chromium`, then retry with mode='advanced'.",
            )
        return _advanced_failure(
            "browser_pool_exhausted",
            error,
            "Retry later or increase ADVANCED_SCRAPE_BROWSER_CONCURRENCY.",
        )
    except Exception as exc:
        return _advanced_failure(
            "advanced_scrape_failed",
            f"{type(exc).__name__}: {exc}",
            "Try query_warehouse first, retry standard scrape, or verify browser_pool on this host.",
        )
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pool is not None and session_id:
            try:
                pool.release(session_id)
            except Exception:
                pass

    if status >= 400:
        return _advanced_failure(
            "advanced_http_error",
            f"HTTP {status}",
            "Try query_warehouse first. If this source needs auth or proxy, configure browser_pool before retrying.",
            status_code=status,
        )
    metadata = extract_metadata(html, final_url)
    structured = extract_product_like_data(html, final_url, metadata)
    return {
        "success": True,
        "crawl_url": final_url,
        "status_code": status,
        "metadata": metadata,
        "structured": structured,
        "markdown": html_to_markdown(html, metadata),
        "html": html,
        "links": extract_links(html, final_url, limit=200),
        "warnings": [] if structured else [{
            "code": "no_structured_product",
            "message": "Rendered the page, but no product-like structured data was detected.",
            "next_step": "Call extract_structured_data with an explicit schema, or add a site-specific parser.",
        }],
    }


def _has_running_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _advanced_failure(
    code: str,
    error: str,
    next_step: str,
    *,
    status_code: int | None = None,
) -> dict:
    result = {
        "success": False,
        "error": error,
        "warnings": [{
            "code": code,
            "message": error,
            "next_step": next_step,
            "cost_if_retry": ADVANCED_SCRAPE_CREDITS,
        }],
    }
    if status_code is not None:
        result["status_code"] = status_code
    return result


def extract_metadata(html: str, base_url: str = "") -> dict:
    title = _first_match(html, r"<title[^>]*>(.*?)</title>")
    desc = _meta_content(html, "description")
    og_title = _meta_property(html, "og:title")
    og_desc = _meta_property(html, "og:description")
    og_image = _meta_property(html, "og:image")
    canonical = _link_href(html, "canonical")
    json_ld = extract_json_ld(html)
    return {
        "title": _clean_text(og_title or title),
        "description": _clean_text(og_desc or desc),
        "image": urljoin(base_url, og_image) if og_image else None,
        "canonical": urljoin(base_url, canonical) if canonical else None,
        "json_ld_count": len(json_ld),
        "json_ld_types": [str(x.get("@type")) for x in json_ld if isinstance(x, dict) and x.get("@type")],
    }


def extract_product_like_data(html: str, url: str, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    for obj in extract_json_ld(html):
        product = _find_jsonld_product(obj)
        if product:
            return _product_from_jsonld(product, url, metadata)
    price = _first_match(html, r"(?:\$|USD\s*)(\d{1,6}(?:[,.]\d{2})?)")
    return {
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "image_urls": [metadata["image"]] if metadata.get("image") else [],
        "sale_price": _to_float(price),
        "currency": "USD" if price else None,
        "product_url": metadata.get("canonical") or url,
        "confidence": 0.45 if price else 0.25,
    }


def extract_json_ld(html: str) -> list[Any]:
    out = []
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.I | re.S,
    )
    for raw in pattern.findall(html):
        text = unescape(raw).strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, list):
            out.extend(parsed)
        else:
            out.append(parsed)
    return out


def extract_links(html: str, base_url: str, *, limit: int = 200) -> list[str]:
    links = []
    seen = set()
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        full = urljoin(base_url, unescape(href).strip())
        parsed = urlparse(full)
        if not parsed.scheme.startswith("http"):
            continue
        if full in seen:
            continue
        seen.add(full)
        links.append(full)
        if len(links) >= limit:
            break
    return links


def html_to_markdown(html: str, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    title = metadata.get("title") or _clean_text(_first_match(html, r"<title[^>]*>(.*?)</title>"))
    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    body = re.sub(r"(?is)<br\s*/?>", "\n", body)
    body = re.sub(r"(?is)</p\s*>", "\n\n", body)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    body = _clean_text(body)
    if len(body) > 4000:
        body = body[:4000].rstrip() + "..."
    if title:
        return f"# {title}\n\n{body}"
    return body


def _product_markdown(data: dict) -> str:
    title = data.get("title") or data.get("sku") or "Product"
    price = data.get("sale_price")
    currency = data.get("currency") or ""
    lines = [f"# {title}"]
    if price is not None:
        lines.append(f"Price: {price} {currency}".strip())
    if data.get("description"):
        lines.append(str(data["description"]))
    if data.get("product_url"):
        lines.append(f"URL: {data['product_url']}")
    return "\n\n".join(lines)


def _find_jsonld_product(obj: Any) -> dict | None:
    if isinstance(obj, dict):
        typ = obj.get("@type")
        if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
            return obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _find_jsonld_product(item)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_jsonld_product(item)
            if found:
                return found
    return None


def _product_from_jsonld(obj: dict, url: str, metadata: dict) -> dict:
    offers = obj.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    image = obj.get("image") or metadata.get("image")
    images = image if isinstance(image, list) else ([image] if image else [])
    brand = obj.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    return {
        "sku": obj.get("sku") or obj.get("mpn"),
        "title": obj.get("name") or metadata.get("title"),
        "description": obj.get("description") or metadata.get("description"),
        "image_urls": images,
        "sale_price": _to_float(offers.get("price") if isinstance(offers, dict) else None),
        "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
        "availability": offers.get("availability") if isinstance(offers, dict) else None,
        "brand": brand,
        "product_url": obj.get("url") or metadata.get("canonical") or url,
        "identifiers": {"mpn": obj.get("mpn"), "gtin": obj.get("gtin13") or obj.get("gtin")},
        "confidence": 0.9,
    }


def _shape_to_schema(data: dict, schema: dict) -> dict:
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not props:
        return data
    shaped = {}
    aliases = {
        "price": "sale_price",
        "images": "image_urls",
        "url": "product_url",
    }
    for key in props:
        src = aliases.get(key, key)
        shaped[key] = data.get(src)
    return shaped


def _first_match(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text or "", re.I | re.S)
    return m.group(1) if m else None


def _meta_content(html: str, name: str) -> str | None:
    return _first_match(
        html,
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
    )


def _meta_property(html: str, prop: str) -> str | None:
    return _first_match(
        html,
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
    )


def _link_href(html: str, rel: str) -> str | None:
    return _first_match(
        html,
        rf'<link[^>]+rel=["\']{re.escape(rel)}["\'][^>]+href=["\']([^"\']+)["\']',
    )


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None
