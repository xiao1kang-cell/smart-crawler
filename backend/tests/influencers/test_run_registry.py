"""Unit tests for in-memory run registry."""
from __future__ import annotations

import time

import pytest

from app.influencers.run_registry import (
    RunRegistry,
    RunStatus,
)


pytestmark = pytest.mark.unit


def test_create_run_returns_pending():
    reg = RunRegistry()
    rid = reg.create_run()
    run = reg.get_run(rid)
    assert run["status"] == RunStatus.PENDING
    assert run["itemCount"] == 0
    assert run["error"] is None
    assert run["startedAt"] is not None


def test_mark_running_succeeded_with_items():
    reg = RunRegistry()
    rid = reg.create_run()
    reg.mark_running(rid)
    assert reg.get_run(rid)["status"] == RunStatus.RUNNING
    reg.mark_succeeded(rid, items=[{"a": 1}, {"a": 2}])
    run = reg.get_run(rid)
    assert run["status"] == RunStatus.SUCCEEDED
    assert run["itemCount"] == 2
    assert run["finishedAt"] is not None
    assert reg.get_items(rid) == [{"a": 1}, {"a": 2}]


def test_mark_failed_preserves_partial_items():
    reg = RunRegistry()
    rid = reg.create_run()
    reg.mark_failed(rid, error="cookies_expired_instagram", partial_items=[{"x": 1}])
    run = reg.get_run(rid)
    assert run["status"] == RunStatus.FAILED
    assert run["error"] == "cookies_expired_instagram"
    assert reg.get_items(rid) == [{"x": 1}]


def test_get_items_supports_pagination():
    reg = RunRegistry()
    rid = reg.create_run()
    reg.mark_succeeded(rid, items=[{"i": i} for i in range(10)])
    assert reg.get_items(rid, limit=3, offset=0) == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert reg.get_items(rid, limit=3, offset=7) == [{"i": 7}, {"i": 8}, {"i": 9}]


def test_get_run_unknown_returns_none():
    reg = RunRegistry()
    assert reg.get_run("nope") is None
    assert reg.get_items("nope") == []


def test_gc_drops_runs_older_than_ttl():
    reg = RunRegistry(ttl_seconds=0.05)
    rid = reg.create_run()
    reg.mark_succeeded(rid, items=[])
    time.sleep(0.1)
    n = reg.gc()
    assert n == 1
    assert reg.get_run(rid) is None
