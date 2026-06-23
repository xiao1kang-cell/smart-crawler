"""Refresh persisted site metrics.

Usage:
  python scripts/refresh_site_metrics.py
  python scripts/refresh_site_metrics.py vidaxl_de vidaxl_us
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import init_db, session_scope  # noqa: E402
from app.site_metrics import refresh_site_metrics  # noqa: E402


def main() -> int:
    sites = sys.argv[1:] or None
    init_db()
    with session_scope() as db:
        count = refresh_site_metrics(db, sites)
    print(f"refreshed site_metrics rows: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
