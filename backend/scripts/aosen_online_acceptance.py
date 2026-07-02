#!/usr/bin/env python3
"""Read-only Aosen production acceptance check.

The script prefers the dedicated Aosen action-plan endpoint. If production has
not been deployed yet, it falls back to /api/data-quality and reports an
approximate status using the same deferred-site rule.
"""
from __future__ import annotations

import json
import os
import sys
import argparse
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("SMARTCRAWLER_BASE_URL", "http://127.0.0.1:8077").rstrip("/")
REQUEST_TIMEOUT = int(os.environ.get("SMARTCRAWLER_ACCEPTANCE_TIMEOUT", "180"))
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
REQUIRED_TEMPLATES = {
    "product_field_fixes",
    "sku_targets",
    "promotion_signals",
    "sales_signals",
    "review_history",
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
            timeout: int | None = None) -> tuple[int, Any]:
    data = None
    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    url = BASE_URL + path
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw[:500]
        return exc.code, parsed


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


def fallback_from_data_quality(
    payload: dict[str, Any],
    *,
    exclude_deferred: bool = False,
) -> dict[str, Any]:
    items = payload.get("items") or []
    summary = payload.get("summary") or {}
    hard_issues = {
        "price_missing",
        "review_count_missing",
        "currency_missing",
        "currency_mismatch",
        "sku_deviation_high",
        "title_weak",
        "category_missing",
        "image_missing",
        "no_products",
    }
    business_issues = {
        "sales_missing",
        "revenue_missing",
        "sales_history_insufficient",
        "traffic_missing",
        "conversion_missing",
    }
    status_counts = {
        "pass": 0,
        "fail": 0,
        "needs_refresh": 0,
        "needs_business_data": 0,
    }
    issue_counts: dict[str, int] = {}
    focus_items = []
    for row in items:
        site = str(row.get("site") or "")
        if exclude_deferred and site in DEFERRED_SITES:
            continue
        issues = set(row.get("issues") or [])
        for issue in issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
        if issues & hard_issues:
            status = "fail"
        elif "promotions_missing" in issues:
            status = "needs_refresh"
        elif issues & business_issues:
            status = "needs_business_data"
        elif issues:
            status = "fail"
        else:
            status = "pass"
        status_counts[status] += 1
        if site.startswith(FOCUS_PREFIXES):
            focus_items.append({
                "site": site,
                "sku_count": row.get("sku_count"),
                "spu_count": row.get("spu_count"),
                "promotion_count": row.get("promotion_count"),
                "issues": sorted(issues),
                "approx_status": status,
            })
    return {
        "status": "fallback_data_quality",
        "note": "Dedicated Aosen endpoint is unavailable; this is an approximate old-endpoint report.",
        "base_url": BASE_URL,
        "deferred_sites": sorted(DEFERRED_SITES),
        "summary": summary,
        "approx_acceptance": {
            "status_counts": status_counts,
            "issue_counts": dict(sorted(issue_counts.items())),
        },
        "focus_items": sorted(focus_items, key=lambda item: item["site"]),
    }


def _site_matches_scope(
    site: str,
    *,
    prefixes: tuple[str, ...],
    exact_sites: set[str],
    exclude_deferred: bool = False,
) -> bool:
    if exclude_deferred and site in DEFERRED_SITES:
        return False
    if exact_sites or prefixes:
        return site in exact_sites or site.startswith(prefixes)
    return True


def _scoped_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "sites": len(items),
        "pass": 0,
        "fail": 0,
        "needs_refresh": 0,
        "needs_business_data": 0,
        "deferred_sites": sorted(DEFERRED_SITES),
    }
    issue_key_map = {
        "no_products": "no_products",
        "coverage_low": "coverage_low",
        "title_weak": "title_weak",
        "price_missing": "price_missing",
        "review_count_missing": "review_count_missing",
        "category_missing": "category_missing",
        "image_missing": "image_missing",
        "sku_deviation_high": "sku_deviation_high",
        "promotions_missing": "promotions_missing",
    }
    currency_issues = 0
    sales_or_revenue_missing = 0
    for item in items:
        status = str(item.get("status") or "")
        if status not in {"pass", "fail", "needs_refresh", "needs_business_data"}:
            status = "pass" if not item.get("issues") else "fail"
        summary[status] = int(summary.get(status) or 0) + 1
        issues = {str(issue) for issue in item.get("issues") or []}
        for issue, key in issue_key_map.items():
            if issue in issues:
                summary[key] = int(summary.get(key) or 0) + 1
        if {"currency_missing", "currency_mismatch"} & issues:
            currency_issues += 1
        if {"sales_missing", "revenue_missing", "sales_history_insufficient"} & issues:
            sales_or_revenue_missing += 1
    summary["currency_issues"] = currency_issues
    summary["sales_or_revenue_missing"] = sales_or_revenue_missing
    for key in issue_key_map.values():
        summary.setdefault(key, 0)
    return summary


def _focus_items(
    items: list[dict[str, Any]],
    *,
    prefixes: tuple[str, ...] = (),
    exact_sites: set[str] | None = None,
    exclude_deferred: bool = False,
) -> list[dict[str, Any]]:
    exact_sites = exact_sites or set()
    return sorted(
        [
            item for item in items
            if (
                isinstance(item, dict)
                and _site_matches_scope(
                    str(item.get("site") or ""),
                    prefixes=prefixes,
                    exact_sites=exact_sites,
                    exclude_deferred=exclude_deferred,
                )
            )
        ],
        key=lambda item: str(item.get("site") or ""),
    )


def acceptance_gate(
    action_plan: dict[str, Any],
    field_quality: dict[str, Any] | None,
    *,
    site_prefixes: tuple[str, ...] = (),
    exact_sites: set[str] | None = None,
    exclude_deferred: bool = False,
) -> dict[str, Any]:
    exact_sites = exact_sites or set()
    summary = action_plan.get("summary") if isinstance(action_plan, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    templates = action_plan.get("templates") if isinstance(action_plan, dict) else {}
    templates = templates if isinstance(templates, dict) else {}
    template_keys = set(templates)
    missing_templates = sorted(REQUIRED_TEMPLATES.difference(template_keys))
    items: list[dict[str, Any]] = []
    if isinstance(field_quality, dict) and isinstance(field_quality.get("items"), list):
        items = [
            item for item in field_quality.get("items") or []
            if isinstance(item, dict)
        ]
    else:
        for group in (action_plan.get("groups") or {}).values():
            if isinstance(group, dict):
                items.extend([
                    item for item in group.get("items") or []
                    if isinstance(item, dict)
                ])
    focus_items = _focus_items(
        items,
        prefixes=site_prefixes,
        exact_sites=exact_sites,
        exclude_deferred=exclude_deferred,
    )
    if site_prefixes or exact_sites:
        summary = _scoped_summary(focus_items)
    focus_promotion_missing = [
        item.get("site")
        for item in focus_items
        if (
            int(item.get("sku_count") or 0) > 0
            and (
                int(item.get("promotion_count") or 0) <= 0
                or "promotions_missing" in set(item.get("issues") or [])
            )
        )
    ]
    blockers: list[str] = []
    if action_plan.get("status") != "ready":
        blockers.append(f"action_plan_status={action_plan.get('status')}")
    for key in ("fail", "needs_refresh", "needs_business_data"):
        if int(summary.get(key) or 0) > 0:
            blockers.append(f"{key}={summary.get(key)}")
    if missing_templates:
        blockers.append("missing_templates=" + ",".join(missing_templates))
    if focus_promotion_missing:
        blockers.append(
            "focus_promotions_missing=" + ",".join(
                str(site) for site in focus_promotion_missing if site
            )
        )
    if not focus_items:
        blockers.append("focus_sites_not_visible")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "required_templates": sorted(REQUIRED_TEMPLATES),
        "missing_templates": missing_templates,
        "focus_items": focus_items,
        "focus_promotion_missing": focus_promotion_missing,
        "summary": summary,
    }


def main(argv: list[str] | None = None) -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description="Read-only Aosen production acceptance check")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--tenant", default="",
                        help="numeric workspace/tenant id to scope the online acceptance check")
    parser.add_argument("--site", action="append", default=[],
                        help="exact site key to include in the scoped acceptance check")
    parser.add_argument("--site-prefix", action="append", default=[],
                        help="site key prefix to include in the scoped acceptance check")
    parser.add_argument("--template-limit", type=int, default=3)
    parser.add_argument("--strict", action="store_true",
                        help="fail unless production Aosen acceptance is ready")
    parser.add_argument("--exclude-deferred", action="store_true",
                        help="exclude deferred sites from the acceptance scope")
    args = parser.parse_args(argv)
    load_env()
    BASE_URL = str(args.base_url or os.environ.get("SMARTCRAWLER_BASE_URL") or BASE_URL).rstrip("/")
    token = login()
    exact_sites = {str(site).strip() for site in args.site if str(site).strip()}
    site_prefixes = tuple(str(prefix).strip() for prefix in args.site_prefix if str(prefix).strip())
    query = {
        "template_limit": str(max(1, min(args.template_limit, 5000))),
        "include_deferred": "0" if args.exclude_deferred else "1",
    }
    if str(args.tenant).strip():
        query["tenant"] = str(args.tenant).strip()
    path = "/api/admin/spine/acceptance/aosen/action-plan?" + urllib.parse.urlencode(query)
    status, body = request("GET", path, token=token, timeout=REQUEST_TIMEOUT)
    if status == 200 and isinstance(body, dict):
        field_query = {}
        if str(args.tenant).strip():
            field_query["tenant"] = str(args.tenant).strip()
        field_query["include_deferred"] = "0" if args.exclude_deferred else "1"
        field_path = "/api/admin/spine/acceptance/aosen/field-quality"
        if field_query:
            field_path += "?" + urllib.parse.urlencode(field_query)
        field_status, field_quality = request(
            "GET",
            field_path,
            token=token,
            timeout=REQUEST_TIMEOUT,
        )
        gate = acceptance_gate(
            body,
            field_quality if field_status == 200 and isinstance(field_quality, dict) else None,
            site_prefixes=site_prefixes,
            exact_sites=exact_sites,
            exclude_deferred=bool(args.exclude_deferred),
        )
        print(json.dumps({
            "status": "action_plan",
            "base_url": BASE_URL,
            "tenant": str(args.tenant).strip() or None,
            "scope": {
                "sites": sorted(exact_sites),
                "site_prefixes": list(site_prefixes),
                "exclude_deferred": bool(args.exclude_deferred),
            },
            "strict": bool(args.strict),
            "field_quality_status": field_status,
            "acceptance_gate": gate,
            "payload": body,
        }, ensure_ascii=False, indent=2))
        return 0 if (gate["ready"] or not args.strict) else 3
    quality_path = "/api/data-quality"
    if str(args.tenant).strip():
        quality_path += "?" + urllib.parse.urlencode({"tenant": str(args.tenant).strip()})
    quality_status, quality = request("GET", quality_path, token=token, timeout=120)
    if quality_status == 200 and isinstance(quality, dict):
        print(json.dumps(
            fallback_from_data_quality(
                quality,
                exclude_deferred=bool(args.exclude_deferred),
            ),
            ensure_ascii=False,
            indent=2,
        ))
        return 2
    print(json.dumps({
        "status": "failed",
        "base_url": BASE_URL,
        "action_plan_status": status,
        "action_plan_body": body,
        "data_quality_status": quality_status,
        "data_quality_body": quality,
    }, ensure_ascii=False, indent=2))
    return 1


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
