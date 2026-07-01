# Amazon Daemon Postgres Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Amazon crawler daemon run against the new platform Postgres/SQLAlchemy data model without requiring a local MySQL server.

**Architecture:** Keep the legacy `MySQLTaskDB` import surface, but implement daemon-required behavior through explicit SQLAlchemy methods. Avoid generic SQL translation; change daemon/backfill callers to use adapter methods when they need Postgres-compatible behavior.

**Tech Stack:** Python 3.11, SQLAlchemy ORM, pytest, existing `app.models` Amazon VOC tables.

---

### Task 1: Monitoring Adapter Methods

**Files:**
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/mysql_.py`
- Test: `backend/tests/test_amazon_voc.py`

- [ ] **Step 1: Write failing tests**

Add tests that initialize the DB, call `MySQLTaskDB.ensure_monitoring_tables()`, `record_queue_depth_snapshot()`, `cleanup_queue_depth_snapshots()`, `update_runtime_status()`, and `get_runtime_statuses()`, then assert rows exist or are cleaned in `CrawlerQueueDepthSnapshot` and `CrawlerRuntimeStatus`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_mysql_adapter_records_queue_depth_and_runtime_status -q`

Expected: FAIL because `record_queue_depth_snapshot` or `get_runtime_statuses` is missing.

- [ ] **Step 3: Implement adapter methods**

In `mysql_.py`, import `CrawlerQueueDepthSnapshot` and add no-op schema creation plus ORM-backed insert, cleanup, and bulk runtime status read methods.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_mysql_adapter_records_queue_depth_and_runtime_status -q`

Expected: PASS.

### Task 2: Callback Schema and Stuck Task Reset

**Files:**
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/mysql_.py`
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/task_queue_backfill.py`
- Test: `backend/tests/test_amazon_voc.py`

- [ ] **Step 1: Write failing tests**

Add tests for `ensure_single_task_callback_columns()` being callable and for running review/listing jobs with old heartbeats being reset to queued by explicit adapter methods.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_mysql_adapter_resets_stuck_amazon_jobs -q`

Expected: FAIL because the adapter reset method is missing or `task_queue_backfill` still relies on raw cursor rowcount.

- [ ] **Step 3: Implement adapter methods and caller switch**

Add `ensure_single_task_callback_columns()` as a no-op. Add `reset_stuck_tasks_to_retry(table, time_field, stuck_minutes)` using `AmazonReviewJob` for `crawl_single_tasks` and `AmazonListingJob` for `crawl_asin_detail_tasks` / `crawl_asin_tasks`. Update `TaskQueueBackfill._timeout_to_retry()` to call this method when present.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_mysql_adapter_resets_stuck_amazon_jobs -q`

Expected: PASS.

### Task 3: MySQL-Only Daemon Subprocesses

**Files:**
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/event_logger.py`
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/daily_aggregator.py`
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/long_term_analyzer.py`
- Modify: `backend/app/crawlers/amazon_crawler/shuler/util/ban_analyzer.py`
- Test: `backend/tests/test_amazon_voc.py`

- [ ] **Step 1: Write failing tests**

Add import/runtime smoke tests that monkeypatch `mysql.connector.connect` to raise and assert Postgres-mode startup helpers do not require localhost MySQL for the daemon path.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_daemon_mysql_only_helpers_degrade_without_local_mysql -q`

Expected: FAIL because `_init_mysql()` calls `mysql.connector.connect`.

- [ ] **Step 3: Implement safe degradation**

Add a small helper in each MySQL-only daemon module so missing local MySQL logs once and causes that optional analytics loop to return instead of crashing the subprocess. Keep Redis-backed analyzer fallback working.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_amazon_voc.py::test_daemon_mysql_only_helpers_degrade_without_local_mysql -q`

Expected: PASS.

### Task 4: Verification

**Files:**
- Verify: `backend/tests/test_amazon_voc.py`
- Verify: daemon module imports

- [ ] **Step 1: Run focused adapter tests**

Run: `python -m pytest backend/tests/test_amazon_voc.py -q`

Expected: PASS, or report unrelated existing failures with exact failure names.

- [ ] **Step 2: Run daemon import check**

Run: `.venv/bin/python -m py_compile backend/app/crawlers/amazon_daemon.py backend/app/crawlers/amazon_crawler/shuler/util/daemon_main.py backend/app/crawlers/amazon_crawler/shuler/util/mysql_.py backend/app/crawlers/amazon_crawler/shuler/util/task_queue_backfill.py`

Expected: exit code 0.
