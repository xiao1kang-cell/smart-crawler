"""In-memory run/dataset registry for the discover HTTP API.

A `run` is one POST /discover/runs invocation. Its dataset is the list of items
produced. State lives in process memory; the FastAPI worker losing data on
restart is acceptable (clients retry).
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone


class RunStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunRegistry:
    def __init__(self, ttl_seconds: float = 3600.0):
        self._runs: dict[str, dict] = {}
        self._items: dict[str, list[dict]] = {}
        self._lock = threading.RLock()
        self._ttl = ttl_seconds

    def create_run(self) -> str:
        rid = uuid.uuid4().hex
        with self._lock:
            self._runs[rid] = {
                "status": RunStatus.PENDING,
                "itemCount": 0,
                "error": None,
                "startedAt": _now(),
                "finishedAt": None,
                "_t": time.monotonic(),
            }
            self._items[rid] = []
        return rid

    def mark_running(self, rid: str) -> None:
        with self._lock:
            r = self._runs.get(rid)
            if r is not None:
                r["status"] = RunStatus.RUNNING

    def mark_succeeded(self, rid: str, items: list[dict]) -> None:
        with self._lock:
            r = self._runs.get(rid)
            if r is None:
                return
            self._items[rid] = list(items)
            r["status"] = RunStatus.SUCCEEDED
            r["itemCount"] = len(items)
            r["finishedAt"] = _now()
            r["_t"] = time.monotonic()

    def mark_failed(self, rid: str, error: str, partial_items: list[dict] | None = None) -> None:
        with self._lock:
            r = self._runs.get(rid)
            if r is None:
                return
            if partial_items:
                self._items[rid] = list(partial_items)
            r["status"] = RunStatus.FAILED
            r["error"] = error
            r["itemCount"] = len(self._items[rid])
            r["finishedAt"] = _now()
            r["_t"] = time.monotonic()

    def get_run(self, rid: str) -> dict | None:
        with self._lock:
            r = self._runs.get(rid)
            if r is None:
                return None
            return {k: v for k, v in r.items() if not k.startswith("_")}

    def get_items(self, rid: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        with self._lock:
            items = self._items.get(rid, [])
            if limit is None:
                return items[offset:]
            return items[offset : offset + limit]

    def gc(self) -> int:
        cutoff = time.monotonic() - self._ttl
        dropped = 0
        with self._lock:
            for rid in list(self._runs):
                if self._runs[rid].get("_t", 0) < cutoff:
                    self._runs.pop(rid, None)
                    self._items.pop(rid, None)
                    dropped += 1
        return dropped


REGISTRY = RunRegistry()
