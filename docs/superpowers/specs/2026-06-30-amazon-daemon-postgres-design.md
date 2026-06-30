# Amazon Daemon Postgres Runtime Design

## Goal

Make `backend/app/crawlers/amazon_daemon.py` run under the new platform Postgres configuration without requiring a local MySQL server. The daemon should keep the existing legacy crawler import surface where practical, but all database work needed by the daemon must go through SQLAlchemy models or explicit Postgres-compatible adapter methods.

## Current Failure

The new `mysql_.py` module is a Postgres compatibility adapter for legacy crawler code, but daemon paths still depend on MySQL-era behavior:

- `MySQLTaskDB.ensure_single_task_callback_columns()` is called by callback retry startup but is missing from the adapter.
- `MySQLTaskDB.record_queue_depth_snapshot()` and `cleanup_queue_depth_snapshots()` are called by queue depth monitoring but are missing from the adapter.
- `task_queue_backfill` uses raw `db.cursor.execute(...)` and `db.cursor.rowcount`, while `_CompatCursor` currently logs and does not track affected rows.
- `EventLogConsumer`, `DailyAggregator`, `LongTermAnalyzer`, and `BanAnalyzer` still open `mysql.connector.connect(...)` to `localhost:3306`.

## Approach

Extend the Postgres compatibility layer only for daemon-required behavior. Do not build a general MySQL SQL translator.

The adapter will expose explicit methods backed by existing SQLAlchemy models:

- Callback schema methods become no-ops because callback fields already exist on `AmazonReviewJob` and `AmazonListingJob`.
- Queue depth snapshots write `CrawlerQueueDepthSnapshot` rows and cleanup deletes old rows by timestamp.
- Runtime status bulk read returns dictionaries from `CrawlerRuntimeStatus`.
- Stuck task reset operations update `AmazonReviewJob` and `AmazonListingJob` rows by status and timestamp using SQLAlchemy, returning affected counts.

Daemon modules that currently open MySQL directly will be migrated away from `mysql.connector.connect(...)` for the Postgres runtime. Where a module only needs aggregate queries, replace connection setup with SQLAlchemy session queries. Where a large legacy raw-SQL loop is out of scope, the daemon should degrade cleanly rather than crash when the MySQL-only analytics path is unavailable.

## Data Flow

Redis remains the source for queue depth and daemon heartbeat. Postgres remains the source for job state, callback retry state, runtime status, and monitoring snapshots.

`daemon_main` calls `MySQLTaskDB` compatibility methods. Those methods translate legacy daemon needs into SQLAlchemy operations against:

- `AmazonReviewJob`
- `AmazonListingJob`
- `CrawlerQueueDepthSnapshot`
- `CrawlerRuntimeStatus`
- existing event/summary/risk models where analytics paths are migrated

## Error Handling

Adapter methods should return safe empty results or zero affected rows for unknown legacy table names. Missing optional analytics stores should log a warning and continue, not terminate the daemon process. Real database exceptions should still be logged with traceback by the existing daemon loops.

## Testing

Add focused tests around the adapter and daemon-facing methods:

- Queue depth snapshot insert and retention cleanup.
- Runtime status bulk read.
- Callback schema compatibility no-op and retry listing still works.
- Stuck review/listing task reset from running to queued.
- `_CompatCursor` no longer causes `rowcount` attribute errors on supported daemon update paths, or the daemon path no longer uses raw cursor for those updates.

Run the relevant Amazon VOC tests and a syntax/import check for daemon modules.

## Non-Goals

- Do not restore or require local MySQL.
- Do not implement a broad MySQL-to-Postgres SQL parser.
- Do not refactor unrelated crawler worker behavior.
- Do not change Redis queue semantics.
