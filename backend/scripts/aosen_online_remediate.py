#!/usr/bin/env python3
"""Run the post-deploy Aosen remediation workflow against an online service.

Default mode is read-only: fetch the Aosen action plan and export CSV previews.
Use --apply to trigger promotion rebuild and analytics recompute for the groups
reported by the action plan.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("SMARTCRAWLER_BASE_URL", "http://127.0.0.1:8077").rstrip("/")
DEFAULT_USER_AGENT = os.environ.get(
    "SMARTCRAWLER_USER_AGENT",
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
)
DEFERRED_SITES = {
    "vidaxl_us",
    "vidaxl_ca",
    "sephora_fr_maquillage",
    "costway_ca",
    "costway_us",
}
FOCUS_PREFIXES = ("homary", "vidaxl", "vonhaus")
FIELD_ISSUES = {
    "title_weak",
    "category_missing",
    "image_missing",
    "price_missing",
    "review_count_missing",
    "currency_missing",
    "currency_mismatch",
    "sku_deviation_high",
    "no_products",
    "coverage_low",
}
PROMOTION_ISSUES = {"promotions_missing"}
BUSINESS_ISSUES = {
    "sales_missing",
    "revenue_missing",
    "sales_history_insufficient",
    "traffic_missing",
    "conversion_missing",
}
SITE_ONLY_FIELD_ISSUES = {"sku_deviation_high", "no_products", "coverage_low"}
FIELD_RERUN_ISSUES = {
    "no_products",
    "coverage_low",
    "title_weak",
    "price_missing",
    "review_count_missing",
}
WEAK_TITLE_VALUES = {
    "",
    "none",
    "null",
    "undefined",
    "product",
    "untitled",
    "n/a",
    "-",
}


def load_env() -> None:
    if os.environ.get("LOAD_ENV", "1") == "0":
        return
    for candidate in (Path(".env"), Path(__file__).resolve().parents[2] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def request(method: str, path: str, *, token: str = "",
            body: dict[str, Any] | None = None,
            timeout: int = 120) -> tuple[int, Any]:
    data = None
    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw[:500]
        return exc.code, parsed
    except (TimeoutError, urllib.error.URLError) as exc:
        return 0, {"error": type(exc).__name__, "detail": str(exc)}


def login() -> str:
    username = (
        os.environ.get("SMARTCRAWLER_ADMIN_USERNAME")
        or os.environ.get("ADMIN_USERNAME")
        or ""
    )
    password = (
        os.environ.get("SMARTCRAWLER_ADMIN_PASSWORD")
        or os.environ.get("ADMIN_PASSWORD")
        or ""
    )
    if not username or not password:
        raise RuntimeError("ADMIN_USERNAME/ADMIN_PASSWORD are required")
    status, body = request(
        "POST",
        "/api/auth/login",
        body={"identifier": username, "password": password},
        timeout=20,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("token"):
        raise RuntimeError(f"login failed: {status} {body}")
    return str(body["token"])


def site_list(plan: dict[str, Any], group: str) -> list[str]:
    sites = (((plan.get("groups") or {}).get(group) or {}).get("sites") or [])
    if not isinstance(sites, list):
        return []
    return sorted(
        {str(site).strip() for site in sites if str(site).strip()},
        key=site_rank,
    )


def export_templates(plan: dict[str, Any], out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    templates = plan.get("templates") or {}
    written: dict[str, str] = {}
    for key, filename in (
        ("product_field_fixes", "product_field_fixes_preview.csv"),
        ("sku_targets", "sku_targets_preview.csv"),
        ("promotion_signals", "promotion_signals_preview.csv"),
        ("sales_signals", "sales_signals_preview.csv"),
        ("review_history", "review_history_preview.csv"),
        ("site_gaps", "site_gaps.csv"),
    ):
        csv_text = str((templates.get(key) or {}).get("csv") or "")
        if not csv_text.strip():
            continue
        path = out_dir / filename
        path.write_text(csv_text, encoding="utf-8")
        written[key] = str(path)
    return written


def csv_payload(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return output.getvalue()


def product_images(row: dict[str, Any]) -> str:
    image_urls = row.get("image_urls")
    if isinstance(image_urls, list):
        return "|".join(str(item) for item in image_urls if item)
    image = row.get("image")
    return str(image or "")


def product_sku(row: dict[str, Any]) -> str:
    return str(row.get("sku") or "").strip()


def site_rank(site: Any) -> tuple[int, int, str]:
    text = str(site or "")
    for index, prefix in enumerate(FOCUS_PREFIXES):
        if text.startswith(prefix):
            return (0, index, text)
    return (1, len(FOCUS_PREFIXES), text)


def site_matches_scope(
    site: Any,
    *,
    prefixes: tuple[str, ...],
    exact_sites: set[str],
    exclude_deferred: bool = False,
) -> bool:
    text = str(site or "").strip()
    if not text or (exclude_deferred and text in DEFERRED_SITES):
        return False
    if not prefixes and not exact_sites:
        return True
    return text in exact_sites or text.startswith(prefixes)


def scoped_issue_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for issue in item.get("issues") or []:
            key = str(issue)
            if key:
                counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def filter_csv_by_scope(
    csv_text: str,
    *,
    prefixes: tuple[str, ...],
    exact_sites: set[str],
    exclude_deferred: bool = False,
) -> str:
    if not csv_text.strip() or (not prefixes and not exact_sites):
        return csv_text
    input_io = io.StringIO(csv_text)
    reader = csv.DictReader(input_io)
    if not reader.fieldnames or "site" not in reader.fieldnames:
        return csv_text
    rows = [
        row for row in reader
        if site_matches_scope(
            row.get("site"),
            prefixes=prefixes,
            exact_sites=exact_sites,
            exclude_deferred=exclude_deferred,
        )
    ]
    return csv_payload(rows, list(reader.fieldnames))


def filter_plan_scope(
    plan: dict[str, Any],
    *,
    prefixes: tuple[str, ...],
    exact_sites: set[str],
    exclude_deferred: bool = True,
) -> dict[str, Any]:
    if not prefixes and not exact_sites and not exclude_deferred:
        return plan
    scoped = copy.deepcopy(plan)
    scoped["scope"] = {
        "site_prefixes": list(prefixes),
        "sites": sorted(exact_sites),
    }
    for group in (scoped.get("groups") or {}).values():
        if not isinstance(group, dict):
            continue
        items = [
            item for item in group.get("items") or []
            if isinstance(item, dict)
            and site_matches_scope(
                item.get("site"),
                prefixes=prefixes,
                exact_sites=exact_sites,
                exclude_deferred=exclude_deferred,
            )
        ]
        group["items"] = items
        group["sites"] = sorted(
            {str(item.get("site")) for item in items if item.get("site")},
            key=site_rank,
        )
        group["count"] = len(items)
        group["issue_counts"] = scoped_issue_counts(items)
    for template in (scoped.get("templates") or {}).values():
        if not isinstance(template, dict):
            continue
        csv_text = str(template.get("csv") or "")
        filtered = filter_csv_by_scope(
            csv_text,
            prefixes=prefixes,
            exact_sites=exact_sites,
            exclude_deferred=exclude_deferred,
        )
        template["csv"] = filtered
        template["count"] = max(0, len(filtered.splitlines()) - 1) if filtered.strip() else 0
    scoped["summary"] = {
        **(scoped.get("summary") or {}),
        "scope_site_prefixes": list(prefixes),
        "scope_sites": sorted(exact_sites),
    }
    return scoped


def product_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def weak_product_title(title: Any, sku: str) -> bool:
    text = str(title or "").strip()
    lowered = text.lower()
    if lowered in WEAK_TITLE_VALUES:
        return True
    compact_title = "".join(ch for ch in lowered if ch.isalnum())
    compact_sku = "".join(ch for ch in str(sku or "").lower() if ch.isalnum())
    return bool(compact_sku and compact_title == compact_sku)


def product_field_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    sku = product_sku(row)
    if weak_product_title(row.get("title"), sku):
        issues.append("title_weak")
    if not str(row.get("category_path") or "").strip():
        issues.append("category_missing")
    if not product_images(row).strip():
        issues.append("image_missing")
    sale = product_number(row.get("sale_price"))
    original = product_number(row.get("original_price"))
    if (sale is None or sale <= 0) and (original is None or original <= 0):
        issues.append("price_missing")
    reviews = product_number(row.get("review_count"))
    if reviews is None:
        issues.append("review_count_missing")
    return issues


def product_business_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    sales = product_number(row.get("thirty_day_sales"))
    revenue = product_number(row.get("thirty_day_revenue"))
    if sales is None or sales <= 0:
        issues.append("sales_missing")
    if revenue is None or revenue <= 0:
        issues.append("revenue_missing")
    return issues


def fetch_json(token: str, path: str, *, timeout: int = 120) -> dict[str, Any] | None:
    status, body = request("GET", path, token=token, timeout=timeout)
    if status == 200 and isinstance(body, dict):
        return body
    return None


def fetch_site_products(
    token: str,
    site: str,
    limit: int,
    *,
    pages: int = 3,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = max(1, min(limit, 200))
    for page in range(1, max(1, pages) + 1):
        query = urllib.parse.urlencode({
            "site": site,
            "page": str(page),
            "page_size": str(page_size),
        })
        body = fetch_json(token, f"/api/products?{query}", timeout=60)
        if not body:
            break
        items = body.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            sku = product_sku(item)
            if not sku or sku in seen:
                continue
            seen.add(sku)
            results.append(item)
        if len(items) < page_size:
            break
        if len(results) >= limit * max(1, pages):
            break
    return results


def data_quality_fallback_plan(
    token: str,
    *,
    template_limit: int,
    products_per_site: int,
    product_pages_per_site: int,
    tenant: str = "",
    skip_product_samples: bool = False,
    exclude_deferred: bool = False,
) -> dict[str, Any]:
    quality_path = "/api/data-quality"
    if str(tenant).strip():
        quality_path += "?" + urllib.parse.urlencode({"tenant": str(tenant).strip()})
    quality = fetch_json(token, quality_path, timeout=180)
    if not quality:
        raise RuntimeError("fallback /api/data-quality unavailable")
    items = [
        row for row in quality.get("items") or []
        if (
            isinstance(row, dict)
            and not (
                exclude_deferred
                and str(row.get("site") or "") in DEFERRED_SITES
            )
        )
    ]

    def row_issues(row: dict[str, Any]) -> set[str]:
        return {str(issue) for issue in row.get("issues") or [] if str(issue)}

    def status_for(row: dict[str, Any]) -> str:
        issues = row_issues(row)
        if issues & FIELD_ISSUES:
            return "fail"
        if issues & PROMOTION_ISSUES:
            return "needs_refresh"
        if issues & BUSINESS_ISSUES:
            return "needs_business_data"
        return "pass" if not issues else "fail"

    grouped: dict[str, list[dict[str, Any]]] = {
        "field_fixes": [],
        "promotion_refresh": [],
        "business_data": [],
    }
    for row in items:
        issues = row_issues(row)
        status = status_for(row)
        payload = {
            "site": row.get("site"),
            "status": status,
            "issues": sorted(issues),
            "sku_count": row.get("sku_count"),
            "spu_count": row.get("spu_count"),
            "target_sku_count": row.get("target_sku_count"),
            "target_sku_source": row.get("target_sku_source"),
            "sku_deviation_pct": row.get("sku_deviation_pct"),
            "coverage_pct": row.get("coverage_pct"),
            "promotion_count": row.get("promotion_count"),
            "suggested_action": row.get("suggested_action"),
        }
        if issues & FIELD_ISSUES:
            grouped["field_fixes"].append(payload)
        if issues & PROMOTION_ISSUES:
            grouped["promotion_refresh"].append(payload)
        if issues & BUSINESS_ISSUES:
            grouped["business_data"].append(payload)
    for key in grouped:
        grouped[key].sort(key=lambda row: site_rank(row.get("site")))

    product_cache: dict[str, list[dict[str, Any]]] = {}

    def products_for(site: str) -> list[dict[str, Any]]:
        if skip_product_samples:
            return []
        if site not in product_cache:
            product_cache[site] = fetch_site_products(
                token,
                site,
                products_per_site,
                pages=product_pages_per_site,
            )
        return product_cache[site]

    remaining = max(1, template_limit)
    field_rows: list[dict[str, Any]] = []
    for site_row in grouped["field_fixes"]:
        site = str(site_row.get("site") or "")
        site_issues = set(site_row.get("issues") or [])
        product_rows = []
        fallback_rows = []
        for product in products_for(site):
            issues = product_field_issues(product)
            if issues:
                product_rows.append((product, issues))
            elif site_issues & SITE_ONLY_FIELD_ISSUES:
                fallback_rows.append((product, sorted(site_issues & SITE_ONLY_FIELD_ISSUES)))
        for product, issues in product_rows + fallback_rows:
            if len(field_rows) >= remaining:
                break
            field_rows.append({
                "site": site,
                "sku": product_sku(product),
                "title": product.get("title") or "",
                "currency": product.get("currency") or "",
                "category_path": product.get("category_path") or "",
                "image_urls": product_images(product),
                "sale_price": product.get("sale_price") or "",
                "original_price": product.get("original_price") or "",
                "review_count": (
                    "" if product.get("review_count") is None
                    else product.get("review_count")
                ),
                "spu": product.get("spu") or "",
                "note": "/".join(issues),
            })
        if skip_product_samples and not product_rows and not fallback_rows and len(field_rows) < remaining:
            field_rows.append({
                "site": site,
                "sku": "",
                "title": "",
                "currency": "",
                "category_path": "",
                "image_urls": "",
                "sale_price": "",
                "original_price": "",
                "review_count": "",
                "spu": "",
                "note": "/".join(sorted(site_issues)) or "fill field fixes for this site",
            })
        if len(field_rows) >= remaining:
            break

    sku_target_rows: list[dict[str, Any]] = []
    for site_row in grouped["field_fixes"]:
        issues = set(site_row.get("issues") or [])
        if not (issues & {"coverage_low", "sku_deviation_high"}):
            continue
        if len(sku_target_rows) >= remaining:
            break
        sku_target_rows.append({
            "site": site_row.get("site"),
            "workspace_id": "",
            "workspace_name": "",
            "current_target_sku_count": site_row.get("target_sku_count") or "",
            "observed_sku_count": site_row.get("sku_count") or 0,
            "observed_spu_count": site_row.get("spu_count") or 0,
            "target_sku_count": "",
            "sku_deviation_pct": site_row.get("sku_deviation_pct"),
            "coverage_pct": site_row.get("coverage_pct"),
            "note": "fill accepted target SKU count after deployment",
        })

    promo_rows: list[dict[str, Any]] = []
    for site_row in grouped["promotion_refresh"]:
        site = str(site_row.get("site") or "")
        products = products_for(site)
        if skip_product_samples and not products and len(promo_rows) < remaining:
            promo_rows.append({
                "site": site,
                "sku": "",
                "promotion_type": "",
                "promotion_name": "",
                "discount_percent": "",
                "promotion_price": "",
                "threshold": "",
                "start_time": "",
                "end_time": "",
                "note": "fill SKU plus coupon/bundle/free_shipping/external promotion",
            })
            continue
        for product in products:
            if len(promo_rows) >= remaining:
                break
            promo_rows.append({
                "site": site,
                "sku": product_sku(product),
                "promotion_type": "",
                "promotion_name": "",
                "discount_percent": "",
                "promotion_price": product.get("sale_price") or "",
                "threshold": "",
                "start_time": "",
                "end_time": "",
                "note": "fill coupon/bundle/free_shipping/external promotion",
            })
        if len(promo_rows) >= remaining:
            break

    sales_rows: list[dict[str, Any]] = []
    review_history_rows: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for site_row in grouped["business_data"]:
        site = str(site_row.get("site") or "")
        products = products_for(site)
        product_rows = [
            (product, product_business_issues(product))
            for product in products
        ]
        product_rows = [(product, issues) for product, issues in product_rows if issues]
        if not product_rows:
            product_rows = [
                (product, sorted(set(site_row.get("issues") or []) & BUSINESS_ISSUES))
                for product in products
            ]
        if skip_product_samples and not product_rows and len(sales_rows) < remaining:
            sales_rows.append({
                "site": site,
                "sku": "",
                "date": today,
                "thirty_day_sales": "",
                "thirty_day_revenue": "",
                "note": "/".join(sorted(set(site_row.get("issues") or []) & BUSINESS_ISSUES))
                or "fill external 30-day sales/revenue",
            })
            continue
        for product, issues in product_rows:
            if len(sales_rows) >= remaining:
                break
            sales_rows.append({
                "site": site,
                "sku": product_sku(product),
                "date": today,
                "thirty_day_sales": "",
                "thirty_day_revenue": "",
                "note": "/".join(issues) or "fill external 30-day sales/revenue",
            })
        if len(sales_rows) >= remaining:
            break
    for site_row in grouped["business_data"]:
        site_issues = set(site_row.get("issues") or [])
        if "sales_history_insufficient" not in site_issues:
            continue
        site = str(site_row.get("site") or "")
        products = products_for(site)
        if skip_product_samples and not products and len(review_history_rows) < remaining:
            review_history_rows.append({
                "site": site,
                "sku": "",
                "date": "",
                "review_count": "",
                "sale_price": "",
                "original_price": "",
                "current_review_count": "",
                "note": "fill SKU plus another historical review snapshot",
            })
            continue
        for product in products:
            if len(review_history_rows) >= remaining:
                break
            current_reviews = product_number(product.get("review_count"))
            if current_reviews is None or current_reviews <= 0:
                continue
            review_history_rows.append({
                "site": site,
                "sku": product_sku(product),
                "date": "",
                "review_count": "",
                "sale_price": product.get("sale_price") or "",
                "original_price": product.get("original_price") or "",
                "current_review_count": int(round(current_reviews)),
                "note": "fill another historical review snapshot date/review_count",
            })
        if len(review_history_rows) >= remaining:
            break

    site_gap_rows = []
    for key, rows in grouped.items():
        for row in rows:
            site_gap_rows.append({
                "group": key,
                "site": row.get("site"),
                "status": row.get("status"),
                "sku_count": row.get("sku_count"),
                "spu_count": row.get("spu_count"),
                "promotion_count": row.get("promotion_count"),
                "issues": "/".join(row.get("issues") or []),
                "suggested_action": row.get("suggested_action") or "",
            })

    def group_payload(key: str, action: str) -> dict[str, Any]:
        rows = grouped[key]
        issue_counts: dict[str, int] = {}
        for row in rows:
            for issue in row.get("issues") or []:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
        return {
            "status": key,
            "count": len(rows),
            "sites": [row.get("site") for row in rows if row.get("site")],
            "issue_counts": dict(sorted(issue_counts.items())),
            "action": action,
            "items": rows,
        }

    summary = quality.get("summary") or {}
    return {
        "status": "fallback_data_quality",
        "verification_source": "/api/data-quality",
        "final_acceptance_scope": "production",
        "summary": summary,
        "deferred_sites": sorted(DEFERRED_SITES),
        "groups": {
            "field_fixes": group_payload(
                "field_fixes",
                "部署新字段验收接口后导入字段修正；当前 CSV 来自旧商品列表样例。",
            ),
            "promotion_refresh": group_payload(
                "promotion_refresh",
                "部署促销解析后重算；CSV 可补外部 coupon/bundle/free_shipping 促销信号。",
            ),
            "business_data": group_payload(
                "business_data",
                "导入外部 30 日销量/营收，或补齐同 SKU 多次评论历史。",
            ),
        },
        "templates": {
            "product_field_fixes": {
                "count": len(field_rows),
                "source": "/api/products fallback",
                "csv": csv_payload(field_rows, [
                    "site", "sku", "title", "currency", "category_path",
                    "image_urls", "sale_price", "original_price",
                    "review_count", "spu", "note",
                ]),
            },
            "sku_targets": {
                "count": len(sku_target_rows),
                "source": "/api/data-quality fallback",
                "csv": csv_payload(sku_target_rows, [
                    "site", "workspace_id", "workspace_name",
                    "current_target_sku_count", "observed_sku_count",
                    "observed_spu_count", "target_sku_count",
                    "sku_deviation_pct", "coverage_pct", "note",
                ]),
            },
            "promotion_signals": {
                "count": len(promo_rows),
                "source": "/api/products fallback",
                "csv": csv_payload(promo_rows, [
                    "site", "sku", "promotion_type", "promotion_name",
                    "discount_percent", "promotion_price", "threshold",
                    "start_time", "end_time", "note",
                ]),
            },
            "sales_signals": {
                "count": len(sales_rows),
                "source": "/api/products fallback",
                "csv": csv_payload(sales_rows, [
                    "site", "sku", "date", "thirty_day_sales",
                    "thirty_day_revenue", "note",
                ]),
            },
            "review_history": {
                "count": len(review_history_rows),
                "source": "/api/products fallback",
                "csv": csv_payload(review_history_rows, [
                    "site", "sku", "date", "review_count", "sale_price",
                    "original_price", "current_review_count", "note",
                ]),
            },
            "site_gaps": {
                "count": len(site_gap_rows),
                "source": "/api/data-quality fallback",
                "csv": csv_payload(site_gap_rows, [
                    "group", "site", "status", "sku_count", "spu_count",
                    "promotion_count", "issues", "suggested_action",
                ]),
            },
        },
        "fallback_warning": (
            "Dedicated Aosen action-plan endpoint is unavailable online; "
            "templates are generated from older production endpoints and must "
            "be revalidated after deployment."
        ),
    }


def action_plan(
    token: str,
    template_limit: int,
    *,
    tenant: str = "",
    include_deferred: bool = True,
) -> dict[str, Any]:
    query_params = {
        "template_limit": str(template_limit),
        "include_deferred": "1" if include_deferred else "0",
    }
    if str(tenant).strip():
        query_params["tenant"] = str(tenant).strip()
    query = urllib.parse.urlencode(query_params)
    status, body = request(
        "GET",
        f"/api/admin/spine/acceptance/aosen/action-plan?{query}",
        token=token,
        timeout=120,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"aosen action-plan unavailable: {status} {body}")
    return body


def get_json(token: str, path: str, *, timeout: int = 120) -> dict[str, Any]:
    status, body = request("GET", path, token=token, timeout=timeout)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"{path} failed: {status} {body}")
    return body


def post_json(token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body = request("POST", path, token=token, body=payload, timeout=300)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"{path} failed: {status} {body}")
    return body


def field_rerun_sites(plan: dict[str, Any]) -> list[str]:
    rows = (((plan.get("groups") or {}).get("field_fixes") or {}).get("items") or [])
    sites: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        issues = {str(issue) for issue in row.get("issues") or []}
        if issues & FIELD_RERUN_ISSUES:
            site = str(row.get("site") or "").strip()
            if site and site not in DEFERRED_SITES:
                sites.add(site)
    return sorted(sites, key=site_rank)


def csv_import_specs(args: argparse.Namespace) -> dict[str, tuple[str, str, Path]]:
    raw = {
        "product_field_fixes": (
            "/api/admin/spine/product-field-fixes/validate",
            "/api/admin/spine/product-field-fixes/import",
            args.import_product_field_fixes,
        ),
        "sku_targets": (
            "/api/admin/spine/sku-targets/validate",
            "/api/admin/spine/sku-targets/import",
            args.import_sku_targets,
        ),
        "promotion_signals": (
            "/api/admin/spine/promotion-signals/validate",
            "/api/admin/spine/promotion-signals/import",
            args.import_promotion_signals,
        ),
        "sales_signals": (
            "/api/admin/spine/sales-signals/validate",
            "/api/admin/spine/sales-signals/import",
            args.import_sales_signals,
        ),
        "review_history": (
            "/api/admin/spine/review-history/validate",
            "/api/admin/spine/review-history/import",
            args.import_review_history,
        ),
    }
    specs: dict[str, tuple[str, str, Path]] = {}
    for key, (validate_path, import_path, value) in raw.items():
        if not value:
            continue
        path = Path(value).expanduser()
        specs[key] = (validate_path, import_path, path)
    return specs


def import_csv_files(
    token: str,
    specs: dict[str, tuple[str, str, Path]],
    *,
    apply: bool,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for key, (validate_path, import_path, path) in specs.items():
        if not path.exists():
            raise RuntimeError(f"{key} csv not found: {path}")
        csv_text = path.read_text(encoding="utf-8")
        if not csv_text.strip():
            raise RuntimeError(f"{key} csv is empty: {path}")
        validation = post_json(token, validate_path, {"csv": csv_text})
        payload: dict[str, Any] = {
            "path": str(path),
            "validation": validation,
            "applied": False,
        }
        if apply and validation.get("valid") is False:
            raise RuntimeError(f"{key} validation failed: {validation}")
        if apply:
            payload["import"] = post_json(token, import_path, {"csv": csv_text})
            payload["applied"] = True
        results[key] = payload
    return results


def main(argv: list[str]) -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description="Aosen online remediation workflow")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--tenant", default="",
                        help="numeric workspace/tenant id to scope the online remediation workflow")
    parser.add_argument("--site", action="append", default=[],
                        help="exact site key to include in the remediation scope")
    parser.add_argument("--site-prefix", action="append", default=[],
                        help="site key prefix to include in the remediation scope")
    parser.add_argument("--apply", action="store_true",
                        help="trigger online promotion rebuild and analytics recompute")
    parser.add_argument("--exclude-deferred", action="store_true",
                        help="exclude deferred sites from the remediation scope")
    parser.add_argument("--no-enqueue-field-reruns", action="store_true",
                        help="with --apply, do not enqueue crawl reruns for title/price/review gaps")
    parser.add_argument("--template-limit", type=int, default=500,
                        help="number of CSV preview rows to request")
    parser.add_argument("--fallback-products-per-site", type=int, default=10,
                        help="SKU sample rows per site when production lacks the Aosen action-plan endpoint")
    parser.add_argument("--fallback-product-pages-per-site", type=int, default=3,
                        help="number of /api/products pages to inspect per site in fallback mode")
    parser.add_argument("--skip-product-samples", action="store_true",
                        help="export site-level rows without calling /api/products for SKU samples")
    parser.add_argument("--out-dir", default="",
                        help="directory for exported CSV previews")
    parser.add_argument("--import-product-field-fixes", default="",
                        help="CSV to validate/import via product-field-fixes endpoints")
    parser.add_argument("--import-sku-targets", default="",
                        help="CSV to validate/import via sku-targets endpoints")
    parser.add_argument("--import-promotion-signals", default="",
                        help="CSV to validate/import via promotion-signals endpoints")
    parser.add_argument("--import-sales-signals", default="",
                        help="CSV to validate/import via sales-signals endpoints")
    parser.add_argument("--import-review-history", default="",
                        help="CSV to validate/import via review-history endpoints")
    args = parser.parse_args(argv)

    load_env()
    BASE_URL = str(args.base_url or os.environ.get("SMARTCRAWLER_BASE_URL") or BASE_URL).rstrip("/")
    token = login()
    tenant = str(args.tenant).strip()
    exact_sites = {str(site).strip() for site in args.site if str(site).strip()}
    site_prefixes = tuple(str(prefix).strip() for prefix in args.site_prefix if str(prefix).strip())
    source = "action_plan"
    try:
        before = action_plan(
            token,
            max(1, min(args.template_limit, 5000)),
            tenant=tenant,
            include_deferred=not bool(args.exclude_deferred),
        )
    except RuntimeError as exc:
        source = "fallback_data_quality"
        before = data_quality_fallback_plan(
            token,
            template_limit=max(1, min(args.template_limit, 5000)),
            products_per_site=max(1, min(args.fallback_products_per_site, 200)),
            product_pages_per_site=max(1, min(args.fallback_product_pages_per_site, 20)),
            tenant=tenant,
            skip_product_samples=bool(args.skip_product_samples),
            exclude_deferred=bool(args.exclude_deferred),
        )
        before["action_plan_error"] = str(exc)
    before = filter_plan_scope(
        before,
        prefixes=site_prefixes,
        exact_sites=exact_sites,
        exclude_deferred=bool(args.exclude_deferred),
    )
    if source == "action_plan" and (site_prefixes or exact_sites):
        try:
            scoped_templates = filter_plan_scope(
                data_quality_fallback_plan(
                    token,
                    template_limit=max(1, min(args.template_limit, 5000)),
                    products_per_site=max(1, min(args.fallback_products_per_site, 200)),
                    product_pages_per_site=max(1, min(args.fallback_product_pages_per_site, 20)),
                    tenant=tenant,
                    skip_product_samples=bool(args.skip_product_samples),
                    exclude_deferred=bool(args.exclude_deferred),
                ),
                prefixes=site_prefixes,
                exact_sites=exact_sites,
                exclude_deferred=bool(args.exclude_deferred),
            )
            before["groups"] = scoped_templates.get("groups") or before.get("groups")
            before["templates"] = scoped_templates.get("templates") or before.get("templates")
            before["scoped_template_source"] = "/api/data-quality + /api/products"
            source = "action_plan_scoped_templates"
        except RuntimeError as exc:
            before["scoped_template_error"] = str(exc)
    promo_sites = site_list(before, "promotion_refresh")
    business_sites = site_list(before, "business_data")
    field_sites = field_rerun_sites(before)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"data/exports/aosen_acceptance_{stamp}")
    templates = export_templates(before, out_dir)

    operations: dict[str, Any] = {
        "apply": bool(args.apply),
        "promotion_refresh_sites": promo_sites,
        "business_data_sites": business_sites,
        "field_rerun_sites": field_sites,
        "templates": templates,
    }
    try:
        operations["queue_before"] = get_json(
            token, "/api/admin/spine/jobs/stats", timeout=60)
    except RuntimeError as exc:
        operations["queue_before_error"] = str(exc)
    import_specs = csv_import_specs(args)
    if args.apply and source == "fallback_data_quality":
        operations["apply_skipped"] = (
            "production lacks the dedicated Aosen remediation endpoints; "
            "deploy first, then rerun with --apply"
        )
    elif args.apply:
        if field_sites and not args.no_enqueue_field_reruns:
            operations["field_rerun_enqueue"] = post_json(
                token,
                "/api/admin/spine/crawl/enqueue",
                {"sites": field_sites},
            )
        if promo_sites:
            operations["promotion_rebuild"] = post_json(
                token,
                "/api/admin/spine/promotions/rebuild",
                {"sites": promo_sites},
            )
        if business_sites:
            operations["analytics_recompute"] = post_json(
                token,
                "/api/admin/spine/analytics/recompute",
                {"sites": business_sites},
            )
        operations["after"] = filter_plan_scope(
            action_plan(
                token,
                max(1, min(args.template_limit, 5000)),
                tenant=tenant,
                include_deferred=not bool(args.exclude_deferred),
            ),
            prefixes=site_prefixes,
            exact_sites=exact_sites,
            exclude_deferred=bool(args.exclude_deferred),
        )
        try:
            operations["queue_after"] = get_json(
                token, "/api/admin/spine/jobs/stats", timeout=60)
        except RuntimeError as exc:
            operations["queue_after_error"] = str(exc)
    if import_specs and source == "fallback_data_quality":
        operations["csv_imports_skipped"] = (
            "production lacks the dedicated Aosen remediation endpoints; "
            "deploy first, then rerun with --apply and the CSV import paths"
        )
    elif import_specs:
        operations["csv_imports"] = import_csv_files(
            token,
            import_specs,
            apply=bool(args.apply),
        )
        if args.apply:
            operations["after_imports"] = action_plan(
                token,
                max(1, min(args.template_limit, 5000)),
                tenant=tenant,
                include_deferred=not bool(args.exclude_deferred),
            )
            operations["after_imports"] = filter_plan_scope(
                operations["after_imports"],
                prefixes=site_prefixes,
                exact_sites=exact_sites,
                exclude_deferred=bool(args.exclude_deferred),
            )

    status_label = (
        "apply_skipped" if args.apply and source == "fallback_data_quality"
        else "applied" if args.apply
        else "dry_run"
    )
    print(json.dumps({
        "status": status_label,
        "base_url": BASE_URL,
        "tenant": tenant or None,
        "scope": {
            "sites": sorted(exact_sites),
            "site_prefixes": list(site_prefixes),
            "exclude_deferred": bool(args.exclude_deferred),
        },
        "source": source,
        "summary": before.get("summary") or {},
        "fallback_warning": before.get("fallback_warning"),
        "operations": operations,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "base_url": BASE_URL,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)
