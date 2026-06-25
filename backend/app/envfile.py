"""Load KEY=VALUE environment files for launchd-managed workers.

launchd does not source shell profiles, so worker processes use SC_ENV_FILE to
load their runtime configuration before modules read environment variables.
"""
from __future__ import annotations

import os


def load_env_file() -> None:
    path = os.environ.get("SC_ENV_FILE")
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
