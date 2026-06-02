#!/usr/bin/env python3
"""Run the 50-task Agent-first benchmark against a local/remote v2 API."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmarks" / "agent_first_tasks.json"
BASE = os.environ.get("SMARTCRAWLER_BASE_URL", "http://127.0.0.1:8077")
KEY = os.environ.get("SMARTCRAWLER_API_KEY") or os.environ.get("SMARTCRAWLER_LOCAL_API_KEY")


def post(path: str, payload: dict) -> tuple[int, dict, int]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KEY or 'missing'}",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}"), int((time.perf_counter() - started) * 1000)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {"error": raw.decode("utf-8", "replace")}
        return exc.code, data, int((time.perf_counter() - started) * 1000)


def run_task(task: dict) -> dict:
    typ = task["type"]
    if typ in {"warehouse", "warehouse_empty", "agent_flow", "memory"} and not task.get("url"):
        status, data, ms = post("/api/v2/query", {"query": task["prompt"], "limit": 5})
    elif typ == "extract" or typ == "extract_bad_url":
        status, data, ms = post("/api/v2/extract", {
            "urls": [task["url"]],
            "schema": {"title": "string", "price": "number", "image": "string"},
        })
    elif typ == "crawl":
        status, data, ms = post("/api/v2/crawl", {"url": task["url"], "dry_run": True, "limit": 100})
    elif typ == "scope":
        status, data, ms = post("/api/v2/crawl", {"url": task["url"], "dry_run": False, "limit": 10})
    else:
        status, data, ms = post("/api/v2/scrape", {"url": task["url"]})
    usage = data.get("usage") if isinstance(data, dict) else {}
    actual_success = bool(status < 400 and (not isinstance(data, dict) or data.get("success", True)))
    expected_success = bool(task.get("expected_success", True))
    expected_status = task.get("expected_status")
    expected_cache_hit = task.get("expected_cache_hit")
    expected_item_success = task.get("expected_item_success")
    actual_item_success = None
    if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
        actual_item_success = all(bool(item.get("success", True)) for item in data["items"])
    status_ok = True
    if expected_status is not None:
        allowed = expected_status if isinstance(expected_status, list) else [expected_status]
        status_ok = status in allowed
    cache_ok = True
    if expected_cache_hit is not None:
        cache_ok = bool((usage or {}).get("cache_hit")) is bool(expected_cache_hit)
    item_ok = True
    if expected_item_success is not None:
        item_ok = actual_item_success is bool(expected_item_success)
    passed = actual_success is expected_success and status_ok and cache_ok and item_ok
    return {
        "id": task["id"],
        "type": typ,
        "expected_tool": task["expected_tool"],
        "http_status": status,
        "success": passed,
        "actual_success": actual_success,
        "expected_success": expected_success,
        "expected_status": expected_status,
        "expected_cache_hit": expected_cache_hit,
        "actual_item_success": actual_item_success,
        "expected_item_success": expected_item_success,
        "credits": int((usage or {}).get("credits_used") or 0),
        "cache_hit": bool((usage or {}).get("cache_hit")),
        "source": (usage or {}).get("source"),
        "duration_ms": ms,
        "tool_calls": 1,
        "token_estimate": len(json.dumps(data, ensure_ascii=False, default=str)) // 4,
    }


def main() -> int:
    if not KEY:
        print("Set SMARTCRAWLER_API_KEY or SMARTCRAWLER_LOCAL_API_KEY first.")
        return 2
    tasks = json.loads(TASKS.read_text())
    rows = [run_task(t) for t in tasks]
    summary = {
        "task_count": len(rows),
        "success_count": sum(1 for r in rows if r["success"]),
        "total_credits": sum(r["credits"] for r in rows),
        "cache_hits": sum(1 for r in rows if r["cache_hit"]),
        "avg_duration_ms": round(sum(r["duration_ms"] for r in rows) / max(1, len(rows)), 1),
        "avg_token_estimate": round(sum(r["token_estimate"] for r in rows) / max(1, len(rows)), 1),
        "competitor_baseline": {
            "firecrawl_xcrawl": "Run the same prompts manually and fill token/credit/success columns.",
            "smart_crawler": "Measured by this script.",
        },
    }
    print(json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
