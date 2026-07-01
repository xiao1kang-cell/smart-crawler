from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


CRAWLERS_DIR = Path(__file__).resolve().parent
APP_DIR = CRAWLERS_DIR.parent
BACKEND_DIR = APP_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent

for path in (str(BACKEND_DIR), str(CRAWLERS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _normalize_env() -> None:
    from app.envfile import load_env_file

    load_env_file(PROJECT_DIR / ".env")
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env == "production":
        os.environ["APP_ENV"] = "prod"
    elif app_env in {"test", "testing"}:
        os.environ["APP_ENV"] = "dev"
    _bridge_redis_env()


def _bridge_redis_env() -> None:
    redis_url = os.getenv("AMAZON_VOC_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return
    parsed = urlparse(redis_url)
    if parsed.hostname:
        os.environ.setdefault("REDIS_HOST", parsed.hostname)
    if parsed.port:
        os.environ.setdefault("REDIS_PORT", str(parsed.port))
    if parsed.username:
        os.environ.setdefault("REDIS_USERNAME", parsed.username)
    if parsed.password:
        os.environ.setdefault("REDIS_PASSWORD", parsed.password)
    db = parsed.path.strip("/") if parsed.path else ""
    if db:
        os.environ.setdefault("REDIS_DB", db)
    os.environ.setdefault("REDIS_QUEUE_DB", os.getenv("AMAZON_VOC_REDIS_QUEUE_DB", db or "0"))


def main() -> None:
    _normalize_env()
    from app.crawlers.amazon_crawler.shuler.util.daemon_main import main as legacy_main

    legacy_main()


if __name__ == "__main__":
    main()
