from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_pytest_uses_isolated_database_by_default():
    from app import db

    project_db = Path(__file__).resolve().parents[2] / "data" / "smart_crawler.db"
    assert Path(db.DATABASE_URL.replace("sqlite:///", "")).resolve() != project_db.resolve()
