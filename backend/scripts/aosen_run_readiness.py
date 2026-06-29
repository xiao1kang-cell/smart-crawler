#!/usr/bin/env python3
"""Aosen production rerun readiness and safe enqueue helper.

Default mode is read-only. With --apply, only sites classified as
enqueue_quality_rerun are submitted; active, blocked, or probe-risk sites are
left untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("SMARTCRAWLER_BASE_URL", "http://127.0.0.1:8077").rstrip("/")
FOCUS_SITES = (
    "homary_de", "homary_es", "homary_fr", "homary_uk", "homary_us",
    "vidaxl_de", "vidaxl_es", "vidaxl_fr", "vidaxl_ie", "vidaxl_it",
    "vidaxl_nl", "vidaxl_pl", "vidaxl_pt", "vidaxl_ro", "vidaxl_uk",
    "vonhaus_uk",
)


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


def request(
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    data = None
    headers: dict[str, str] = {}
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
        raw = exc.read().decode(errors="ignore")
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw[:500]
        return exc.code, parsed
    except Exception as exc:
        return 0, {"error": type(exc).__name__, "detail": str(exc)[:300]}


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


def live_probe(site: str, url: str | None, *, timeout: int) -> tuple[str, dict[str, Any]]:
    if not url:
        return site, {"status": 0, "error": "missing_url"}
    headers = {"User-Agent": "Mozilla/5.0 AosenRunReadiness/1.0"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(
            req,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as resp:
            return site, {"status": resp.status, "final_url": resp.geturl()}
    except urllib.error.HTTPError as exc:
        return site, {
            "status": exc.code,
            "error": "HTTPError",
            "final_url": getattr(exc, "url", url),
        }
    except Exception as exc:
        return site, {
            "status": 0,
            "error": type(exc).__name__,
            "detail": str(exc)[:180],
        }


def classify_site(site: str, row: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    latest = row.get("latest_job") or {}
    failure = row.get("latest_failure") or {}
    queue = row.get("crawl_queue") or {}
    latest_status = latest.get("status")
    blockers: list[str] = []
    recommended: list[str] = []
    if latest_status in {"pending", "running"} or int(queue.get("active_count") or 0) > 0:
        action = "wait_existing_job"
        blockers.append(f"active_job={latest_status}#{latest.get('id')}")
    elif site in {"homary_fr", "homary_uk"} and failure.get("code") == "http_429":
        action = "fix_rate_limit_or_proxy_then_enqueue"
        blockers.append("latest_http_429")
        recommended.append("lower concurrency / increase rate interval / proxy lease before rerun")
    elif site == "vonhaus_uk" and int(probe.get("status") or 0) == 403:
        action = "verify_worker_access_then_enqueue"
        blockers.append("external_probe_403")
        recommended.append("confirm NAS worker can fetch VonHaus or configure proxy")
    else:
        action = "enqueue_quality_rerun"
    if latest_status in {"failed", "blocked"}:
        recommended.append("enqueue should create/reuse admin_quality_rerun unless preflight blocks")
    if int(row.get("promotion_count") or 0) == 0:
        recommended.append("run promotions/rebuild after crawl finishes")
    if int(row.get("sales_signal_count") or 0) == 0 or int(row.get("revenue_signal_count") or 0) == 0:
        recommended.append("needs two dated review_count snapshots before analytics can estimate sales/revenue")
    return {
        "site": site,
        "action": action,
        "blockers": blockers,
        "recommended": recommended,
        "live_probe": probe,
        "latest_job": {
            "id": latest.get("id"),
            "status": latest_status,
            "failure_code": latest.get("failure_code"),
        },
        "queue": {
            "pending": queue.get("pending"),
            "running": queue.get("running"),
            "active_count": queue.get("active_count"),
        },
        "issues": sorted(set(row.get("issues") or [])),
    }


def main(argv: list[str]) -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description="Check and safely enqueue Aosen reruns")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--tenant", default="1")
    parser.add_argument("--apply", action="store_true",
                        help="enqueue only sites classified as enqueue_quality_rerun")
    parser.add_argument("--probe-timeout", type=int, default=8)
    args = parser.parse_args(argv)
    load_env()
    BASE_URL = str(args.base_url or os.environ.get("SMARTCRAWLER_BASE_URL") or BASE_URL).rstrip("/")
    token = login()
    status, payload = request(
        "GET",
        f"/api/admin/spine/data-quality?tenant={args.tenant}",
        token=token,
        timeout=90,
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"data-quality unavailable: {status} {payload}")
    rows_by_site = {
        str(row.get("site") or ""): row
        for row in payload.get("items") or []
        if isinstance(row, dict)
    }
    probes: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                live_probe,
                site,
                (rows_by_site.get(site) or {}).get("url"),
                timeout=max(1, min(args.probe_timeout, 30)),
            )
            for site in FOCUS_SITES
        ]
        for future in as_completed(futures):
            site, probe = future.result()
            probes[site] = probe
    rows = [
        classify_site(site, rows_by_site.get(site) or {}, probes.get(site) or {})
        for site in FOCUS_SITES
    ]
    enqueue_sites = [row["site"] for row in rows if row["action"] == "enqueue_quality_rerun"]
    operations: dict[str, Any] = {"enqueue_candidates": enqueue_sites}
    if args.apply and enqueue_sites:
        enqueue_status, enqueue_body = request(
            "POST",
            "/api/admin/spine/crawl/enqueue",
            token=token,
            body={"sites": enqueue_sites},
            timeout=120,
        )
        operations["enqueue"] = {
            "status": enqueue_status,
            "body": enqueue_body,
        }
    print(json.dumps({
        "status": "applied" if args.apply else "dry_run",
        "base_url": BASE_URL,
        "tenant": args.tenant,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data_quality_status": status,
        "operations": operations,
        "rows": rows,
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
