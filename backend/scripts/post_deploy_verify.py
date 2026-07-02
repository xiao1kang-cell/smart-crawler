#!/usr/bin/env python3
"""HTTP-level deployment smoke test for smart-crawler."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


BASE_URL = os.environ.get("SMARTCRAWLER_BASE_URL", "http://127.0.0.1:8077").rstrip("/")
DEFAULT_USER_AGENT = os.environ.get(
    "SMARTCRAWLER_USER_AGENT",
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
)
API_KEY = os.environ.get("SMARTCRAWLER_API_KEY") or os.environ.get("API_KEY") or ""
ADMIN_USERNAME = os.environ.get("SMARTCRAWLER_ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME") or ""
ADMIN_PASSWORD = os.environ.get("SMARTCRAWLER_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or ""
STRICT_AOSEN_ACCEPTANCE = os.environ.get("STRICT_AOSEN_ACCEPTANCE", "").lower() in {
    "1", "true", "yes", "on",
}
AOSEN_VERIFY_TIMEOUT = int(os.environ.get("SMARTCRAWLER_AOSEN_VERIFY_TIMEOUT", "180"))
SKIP_API_KEY_VERIFY = os.environ.get("SKIP_API_KEY_VERIFY", "").lower() in {
    "1", "true", "yes", "on",
}
AOSEN_REQUIRED_TEMPLATES = {
    "product_field_fixes",
    "sku_targets",
    "promotion_signals",
    "sales_signals",
    "review_history",
}
AOSEN_DEFERRED_SITES = {
    "vidaxl_us",
    "vidaxl_ca",
    "sephora_fr_maquillage",
    "costway_ca",
    "costway_us",
}


def parse_response_body(raw: str) -> Any:
    if not raw:
        return None
    candidate = raw.strip()
    if "data:" in candidate:
        for line in candidate.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                candidate = line.split("data:", 1)[1].strip()
                break
    return json.loads(candidate)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def request(method: str, path: str, *, token: str = "", api_key: str = "",
            body: dict[str, Any] | None = None, workspace_id: int | None = None,
            extra_headers: dict[str, str] | None = None,
            timeout: int = 12) -> tuple[int, Any, dict[str, str]]:
    data = None
    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    if workspace_id:
        headers["X-Workspace-ID"] = str(workspace_id)
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            parsed = parse_response_body(raw)
            return resp.status, parsed, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return exc.code, parsed, {k.lower(): v for k, v in exc.headers.items()}


def wait_health() -> Result:
    last = ""
    for _ in range(30):
        try:
            status, body, _ = request("GET", "/health", timeout=3)
            if status == 200 and body and body.get("status") == "ok":
                return Result("health", True, "/health ok")
            last = f"{status} {body}"
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    return Result("health", False, last)


def login() -> tuple[Result, str]:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return Result("admin_login", False, "SMARTCRAWLER_ADMIN_USERNAME/PASSWORD not set"), ""
    status, body, _ = request(
        "POST", "/api/auth/login",
        body={"identifier": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    if status == 200 and body and body.get("token"):
        return Result("admin_login", True, f"logged in as {ADMIN_USERNAME}"), body["token"]
    return Result("admin_login", False, f"{status} {body}"), ""


def verify_admin_surface(token: str) -> list[Result]:
    results: list[Result] = []
    status, me, _ = request("GET", "/api/me", token=token)
    if status != 200:
        return [Result("api_me", False, f"{status} {me}")]
    workspace_id = me.get("current_workspace_id")
    results.append(Result(
        "api_me", bool(me.get("workspaces") and workspace_id),
        f"user={me.get('username')} workspaces={len(me.get('workspaces') or [])} current={workspace_id}",
    ))
    status, workspaces, _ = request("GET", "/api/workspaces", token=token)
    results.append(Result(
        "workspaces", status == 200 and isinstance(workspaces, list) and len(workspaces) >= 1,
        f"{status} count={len(workspaces) if isinstance(workspaces, list) else 'n/a'}",
    ))
    status, sites, _ = request("GET", "/api/sites", token=token, workspace_id=workspace_id)
    results.append(Result(
        "workspace_sites", status == 200 and isinstance(sites, list),
        f"{status} count={len(sites) if isinstance(sites, list) else 'n/a'}",
    ))
    status, keys, _ = request("GET", "/api/keys", token=token, workspace_id=workspace_id)
    results.append(Result(
        "workspace_keys", status == 200 and isinstance(keys, list),
        f"{status} count={len(keys) if isinstance(keys, list) else 'n/a'}",
    ))
    if isinstance(sites, list) and sites:
        site = sites[0]["site"]
        status, overview, _ = request("GET", f"/api/sites/{site}/overview", token=token, workspace_id=workspace_id)
        results.append(Result("site_overview", status == 200 and isinstance(overview, dict), f"{status} site={site}"))
    results.extend(verify_aosen_surface(token))
    return results


def verify_aosen_surface(token: str) -> list[Result]:
    results: list[Result] = []
    status, acceptance, _ = request(
        "GET",
        "/api/admin/spine/acceptance/aosen/field-quality",
        token=token,
        timeout=AOSEN_VERIFY_TIMEOUT,
    )
    acceptance_summary = acceptance.get("summary") if isinstance(acceptance, dict) else None
    acceptance_items = acceptance.get("items") if isinstance(acceptance, dict) else None
    deferred_sites = set(AOSEN_DEFERRED_SITES)
    if isinstance(acceptance_summary, dict) and isinstance(
            acceptance_summary.get("deferred_sites"), list):
        deferred_sites = {
            str(site) for site in acceptance_summary.get("deferred_sites") or []
            if str(site)
        }
    deferred_in_items = sorted(
        {
            str(item.get("site"))
            for item in acceptance_items or []
            if isinstance(item, dict) and str(item.get("site")) in deferred_sites
        }
    )
    results.append(Result(
        "aosen_field_quality_endpoint",
        (
            status == 200
            and isinstance(acceptance_summary, dict)
            and isinstance(acceptance_items, list)
            and not deferred_in_items
            and acceptance.get("final_acceptance_scope") == "production"
        ) if isinstance(acceptance, dict) else False,
        f"{status} sites={acceptance_summary.get('sites') if isinstance(acceptance_summary, dict) else 'n/a'} "
        f"deferred_in_items={deferred_in_items}",
    ))

    status, action_plan, _ = request(
        "GET",
        "/api/admin/spine/acceptance/aosen/action-plan?template_limit=3",
        token=token,
        timeout=AOSEN_VERIFY_TIMEOUT,
    )
    summary = action_plan.get("summary") if isinstance(action_plan, dict) else None
    templates = action_plan.get("templates") if isinstance(action_plan, dict) else None
    template_keys = set(templates) if isinstance(templates, dict) else set()
    missing_templates = sorted(AOSEN_REQUIRED_TEMPLATES.difference(template_keys))
    results.append(Result(
        "aosen_action_plan_endpoint",
        (
            status == 200
            and isinstance(summary, dict)
            and isinstance(templates, dict)
            and not missing_templates
            and action_plan.get("final_acceptance_scope") == "production"
        ) if isinstance(action_plan, dict) else False,
        f"{status} status={action_plan.get('status') if isinstance(action_plan, dict) else 'n/a'} "
        f"sites={summary.get('sites') if isinstance(summary, dict) else 'n/a'} "
        f"missing_templates={missing_templates}",
    ))
    if status == 200 and isinstance(action_plan, dict):
        ready = action_plan.get("status") == "ready"
        results.append(Result(
            "aosen_acceptance_ready",
            ready or not STRICT_AOSEN_ACCEPTANCE,
            (
                f"status={action_plan.get('status')} strict={STRICT_AOSEN_ACCEPTANCE} "
                f"summary={summary if isinstance(summary, dict) else {}}"
            ),
        ))
    return results


def verify_api_key_surface() -> list[Result]:
    if SKIP_API_KEY_VERIFY:
        return [Result("api_key", True, "skipped (SKIP_API_KEY_VERIFY=1)")]
    if not API_KEY:
        return [Result("api_key", False, "SMARTCRAWLER_API_KEY/API_KEY not set")]
    results: list[Result] = []
    status, sources, _ = request("GET", "/api/v2/sources", api_key=API_KEY)
    results.append(Result(
        "api_v2_sources", status == 200 and isinstance(sources, dict),
        f"{status}",
    ))
    status, sites, _ = request("GET", "/api/sites", api_key=API_KEY)
    results.append(Result(
        "api_key_workspace_sites", status == 200 and isinstance(sites, list),
        f"{status} count={len(sites) if isinstance(sites, list) else 'n/a'}",
    ))
    results.append(verify_mcp_tools())
    return results


def verify_mcp_tools() -> Result:
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smart-crawler-deploy-verify", "version": "1.0"},
        },
    }
    status, body, headers = request(
        "POST", "/mcp/", api_key=API_KEY,
        extra_headers={"Accept": "application/json, text/event-stream"},
        body=init_body,
        timeout=15,
    )
    session_id = headers.get("mcp-session-id")
    if status != 200 or not session_id:
        return Result("mcp_initialize", False, f"{status} session={bool(session_id)} body={body}")
    request(
        "POST", "/mcp/", api_key=API_KEY,
        extra_headers={
            "Mcp-Session-Id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        body={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        timeout=8,
    )
    data = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode()
    req = urllib.request.Request(
        BASE_URL + "/mcp/",
        data=data,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Authorization": f"Bearer {API_KEY}",
            "X-API-Key": API_KEY,
            "Mcp-Session-Id": session_id,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except Exception as exc:
        return Result("mcp_tools", False, str(exc))
    payload = raw
    if "data:" in raw:
        payload = raw.split("data:", 1)[1].strip()
    try:
        parsed = json.loads(payload)
        names = [t["name"] for t in parsed.get("result", {}).get("tools", [])]
    except Exception:
        return Result("mcp_tools", False, raw[:300])
    expected = {"query_warehouse", "scrape_url", "crawl_site"}
    missing = sorted(expected.difference(names))
    return Result("mcp_tools", not missing, f"tools={len(names)} missing={missing}")


def main() -> int:
    results: list[Result] = [wait_health()]
    token = ""
    login_result, token = login()
    results.append(login_result)
    if token:
        results.extend(verify_admin_surface(token))
    results.extend(verify_api_key_surface())
    print(f"smart-crawler post-deploy verification: {BASE_URL}")
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        print(f"[{mark}] {r.name}: {r.detail}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
