"""Pytest-wide safety rails.

Tests that import ``app.db`` should never point at the local development
database. A surprising number of API tests call ``init_db()`` directly; without
an early DATABASE_URL override they pollute ``data/smart_crawler.db`` and make
the admin console show fake sites/jobs.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_ALLOW_REAL_DB = os.environ.get("SC_ALLOW_REAL_DB_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if not _ALLOW_REAL_DB:
    _tmp_dir = Path(tempfile.mkdtemp(prefix="smart-crawler-pytest-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir / 'smart_crawler_test.db'}"
    os.environ.setdefault("SC_TESTING", "1")

    @atexit.register
    def _cleanup_test_db() -> None:
        shutil.rmtree(_tmp_dir, ignore_errors=True)
