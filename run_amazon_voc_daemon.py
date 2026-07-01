"""Direct launcher for the Amazon VOC daemon.

Use this file when PyCharm should run a normal Python script instead of
`python -m app.crawlers.amazon_daemon`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.chdir(ROOT_DIR)

# Load project env before daemon modules read environment variables.
from app.envfile import load_env_file  # noqa: E402

load_env_file(ROOT_DIR / ".env")

from app.crawlers.amazon_daemon import main  # noqa: E402


if __name__ == "__main__":
    main()
