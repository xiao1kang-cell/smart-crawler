"""Agent-first crawler service layer.

This module is the shared implementation behind `/api/v2/*` and MCP crawler
tools. It keeps the product promise simple: answer from the warehouse first,
then fall back to a light live scrape when the warehouse cannot answer.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import CrawlJob, Product, Site


MAX_LIVE_HTML_CHARS = 300_000


@dataclass
class UsageInfo:
    credits_used: int = 1
    cache_hit: bool = False
    source: str = "warehouse"  # warehouse / live / queued / unsupported
    duration_ms: int = 0
    records: int = 0

    def to_dict(self) -> dict:
        return {
            "credits_used": self.credits_used,
            "cache_hit": self.cache_hit,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "records": self.records,
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
) -> dict:
    """Scrape one URL with warehouse-first behavior."""
    started = time.perf_counter()
    formats = formats or ["markdown", "structured"]
    scrape_id = "scr_" + uuid.uuid4().hex[:16]
    site = match_site(db, url)

    if site and not force_live:
        product = (db.query(Product)
                   .filter(Product.site == site.site,
                           Product.product_url == url)
                   .first())
        if product:
            data = product_to_schema(product, site)
            usage = UsageInfo(
                credits_used=1, cache_hit=True, source="warehouse",
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

    live = live_scrape_url(url, wait_for_ms=wait_for_ms, timeout_ms=timeout_ms)
    if live["success"]:
        usage = UsageInfo(
            credits_used=2, cache_hit=False, source="live",
            duration_ms=_now_ms(started), records=1,
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
                "next_step": f"Poll /api/v2/crawl/{job_id}",
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
            "next_step": "Add the source to sites.yaml or call map_site on a supported domain.",
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
        "usage": UsageInfo(credits_used=1, cache_hit=True, source="warehouse",
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
        "usage": UsageInfo(credits_used=1, cache_hit=True, source="warehouse",
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
    for url in urls[:25]:
        scraped = scrape_url(db, url, formats=["structured", "markdown"])
        data = scraped.get("data") or {}
        items.append({
            "url": url,
            "success": bool(scraped.get("success")),
            "source": (scraped.get("usage") or {}).get("source"),
            "data": _shape_to_schema(data, schema or {}),
            "warnings": scraped.get("warnings", []),
        })
    return {
        "success": True,
        "items": items,
        "schema": schema or {},
        "instruction": instruction,
        "usage": UsageInfo(credits_used=max(1, len(items) * 2),
                           source="mixed",
                           duration_ms=_now_ms(started),
                           records=len(items)).to_dict(),
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
