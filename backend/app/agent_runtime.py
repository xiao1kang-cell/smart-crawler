"""Agent-first cache, usage, and error helpers."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import AgentCache, ApiKey, Usage

DEFAULT_FREE_CREDITS = int(os.environ.get("SC_FREE_MONTHLY_CREDITS", "2000"))
AGENT_CACHE_TTL_SEC = int(os.environ.get("SC_AGENT_CACHE_TTL_SEC", "300"))


def agent_key_for_api_key(api_key_id: int | None) -> str:
    return f"apikey:{api_key_id}" if api_key_id else "session:anonymous"


def stable_cache_key(tool: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool}:{body}".encode("utf-8")).hexdigest()


def get_cached_response(
    db: Session,
    *,
    agent_key: str,
    tool: str,
    cache_key: str,
) -> dict | None:
    now = datetime.utcnow()
    try:
        db.query(AgentCache).filter(AgentCache.expires_at < now).delete(
            synchronize_session=False)
        row = (db.query(AgentCache)
                 .filter(AgentCache.agent_key == agent_key,
                         AgentCache.tool == tool,
                         AgentCache.cache_key == cache_key,
                         AgentCache.expires_at >= now)
                 .order_by(AgentCache.created_at.desc())
                 .first())
        db.commit()
    except Exception:
        db.rollback()
        return None
    if not row or not isinstance(row.response, dict):
        return None
    result = json.loads(json.dumps(row.response, default=str))
    _merge_usage(result, {
        "credits_used": 0,
        "cache_hit": True,
        "source": "agent_memory",
        "memory_ttl_sec": max(0, int((row.expires_at - now).total_seconds())),
        "fetched_5min_ago": True,
    })
    result.setdefault("warnings", [])
    result["warnings"].append({
        "code": "agent_memory_hit",
        "message": "Returned from 5-minute agent memory. No credits were used.",
        "next_step": "Set force_live=true or change the query if fresh data is required.",
    })
    return result


def store_cached_response(
    db: Session,
    *,
    agent_key: str,
    tool: str,
    cache_key: str,
    response: dict,
    ttl_sec: int = AGENT_CACHE_TTL_SEC,
) -> None:
    if not isinstance(response, dict) or not response.get("success", True):
        return
    expires = datetime.utcnow() + timedelta(seconds=ttl_sec)
    try:
        db.add(AgentCache(
            agent_key=agent_key,
            tool=tool,
            cache_key=cache_key,
            response=json.loads(json.dumps(response, ensure_ascii=False, default=str)),
            expires_at=expires,
        ))
        db.commit()
    except Exception:
        db.rollback()


def run_with_agent_memory(
    db: Session,
    *,
    agent_key: str,
    tool: str,
    payload: dict[str, Any],
    producer,
    cacheable: bool = True,
) -> dict:
    key = stable_cache_key(tool, payload)
    if cacheable:
        cached = get_cached_response(db, agent_key=agent_key, tool=tool,
                                     cache_key=key)
        if cached is not None:
            return cached
    result = producer()
    if cacheable:
        store_cached_response(db, agent_key=agent_key, tool=tool, cache_key=key,
                              response=result)
    return result


def enrich_usage(
    db: Session,
    result: dict,
    *,
    api_key: ApiKey | None = None,
    default_cost_if_retry: int | None = None,
) -> dict:
    if not isinstance(result, dict):
        return result
    usage = result.setdefault("usage", {})
    usage.setdefault("credits_used", 0)
    usage.setdefault("cache_hit", False)
    usage.setdefault("source", "unknown")
    usage.setdefault("records", _infer_records(result))
    usage.setdefault("duration_ms", 0)
    if default_cost_if_retry is not None:
        usage.setdefault("cost_if_retry", default_cost_if_retry)
    usage.setdefault("balance", _balance_after(db, api_key, usage))
    _add_natural_language_guidance(result)
    return result


def insufficient_scope_response(required: str, granted: list[str]) -> dict:
    return {
        "success": False,
        "error": "insufficient_scope",
        "required_scope": required,
        "granted_scopes": granted,
        "usage": {
            "credits_used": 0,
            "cache_hit": False,
            "source": "auth",
            "records": 0,
            "duration_ms": 0,
            "cost_if_retry": 0,
        },
        "warnings": [{
            "code": "insufficient_scope",
            "message": f"This API key needs `{required}` to run that action.",
            "next_step": f"Request `{required}` scope or retry with dry_run=true.",
        }],
    }


def advanced_retry_hint(error: str | None = None) -> dict:
    message = error or "Live scrape failed."
    return {
        "code": "advanced_retry_available",
        "message": (
            f"{message} Try advanced mode when browser/anti-bot support is enabled, "
            "or query the warehouse first."
        ),
        "next_step": "Call query_crawler_warehouse for cached data, or retry later with mode='advanced'.",
        "cost_if_retry": 3,
    }


def _merge_usage(result: dict, patch: dict) -> None:
    usage = result.setdefault("usage", {})
    usage.update(patch)


def _balance_after(db: Session, api_key: ApiKey | None, usage: dict) -> int | None:
    if not api_key:
        return None
    quota = (api_key.monthly_credit_quota
             if api_key.monthly_credit_quota is not None
             else DEFAULT_FREE_CREDITS)
    cutoff = datetime.utcnow() - timedelta(days=30)
    try:
        used = (db.query(func.coalesce(func.sum(Usage.credits_used), 0))
                  .filter(Usage.api_key_id == api_key.id,
                          Usage.occurred_at >= cutoff)
                  .scalar() or 0)
    except Exception:
        used = 0
    current = int(usage.get("credits_used") or 0)
    return max(0, int(quota) - int(used) - current)


def _add_natural_language_guidance(result: dict) -> None:
    warnings = result.setdefault("warnings", [])
    usage = result.setdefault("usage", {})
    if result.get("success") is False and not warnings:
        warnings.append({
            "code": "agent_action_needed",
            "message": "This request failed, but the agent can still try warehouse search or a supported source.",
            "next_step": "Call query_crawler_warehouse before retrying live scrape.",
        })
    if not result.get("success") and usage.get("cost_if_retry") is None:
        usage["cost_if_retry"] = 3
    if result.get("metadata", {}).get("error") and not any(
        w.get("code") == "advanced_retry_available" for w in warnings
    ):
        hint = advanced_retry_hint(str(result["metadata"]["error"]))
        warnings.append(hint)
        usage.setdefault("cost_if_retry", hint["cost_if_retry"])


def _infer_records(result: dict) -> int:
    for key in ("items", "data", "links", "products", "reviews", "promotions"):
        if isinstance(result.get(key), list):
            return len(result[key])
    if result.get("data"):
        return 1
    return int(result.get("returned") or result.get("count") or result.get("total") or 0)
