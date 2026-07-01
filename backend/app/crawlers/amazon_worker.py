from __future__ import annotations

import argparse
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


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _bootstrap_runtime(*, init_database: bool | None = None) -> None:
    _normalize_env()
    if init_database is None:
        init_database = _env_bool("AMAZON_VOC_INIT_DB", True)
    if init_database:
        from app.db import init_db

        init_db()


def _worker_count() -> int:
    for name in (
        "COUNT",
        "AMAZON_VOC_LISTING_WORKER_PROCESSES",
        "AMAZON_VOC_REVIEW_US_WORKER_PROCESSES",
        "AMAZON_VOC_REVIEW_NON_US_WORKER_PROCESSES",
    ):
        raw = os.getenv(name)
        if raw:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return 3


def _country() -> str | None:
    markets = os.getenv("AMAZON_VOC_WORKER_MARKETS", "").strip()
    exclude = os.getenv("AMAZON_VOC_WORKER_EXCLUDE_MARKETS", "").strip().upper()
    if markets:
        return markets.split(",", 1)[0].strip().upper()
    if exclude == "US" or int(os.getenv("AMAZON_VOC_REVIEW_NON_US_WORKER_PROCESSES", "0") or 0) > 0:
        return "other"
    if int(os.getenv("AMAZON_VOC_REVIEW_US_WORKER_PROCESSES", "0") or 0) > 0:
        return "US"
    return None


def main() -> None:
    _bootstrap_runtime()
    parser = argparse.ArgumentParser(description="Amazon VOC legacy review worker adapter")
    parser.add_argument("--listing", action="store_true")
    parser.add_argument("--workers", type=int, default=_worker_count())
    parser.add_argument("--country", default=_country())
    parser.add_argument("--source", default=os.getenv("AMAZON_VOC_WORKER_SOURCE") or None)
    parser.add_argument("--start-stagger", type=float, default=float(os.getenv("WORKER_START_STAGGER_SECONDS", "0.5")))
    parser.add_argument("--start-jitter", type=float, default=float(os.getenv("WORKER_START_STAGGER_JITTER_SECONDS", "1.5")))
    args = parser.parse_args()

    if args.listing or int(os.getenv("AMAZON_VOC_LISTING_WORKER_PROCESSES", "0") or 0) > 0:
        from app.crawlers.amazon_crawler.shuler.services.amazon.asin_worker import main as asin_main

        sys.argv = [
            sys.argv[0],
            "--workers",
            str(args.workers),
        ]
        if args.country and str(args.country).lower() != "other":
            sys.argv.extend(["--region", str(args.country).upper()])
        asin_main()
        return

    from app.crawlers.amazon_crawler.shuler.services.amazon.get_reviews_main import start_workers

    start_workers(
        worker_mode="single",
        country=args.country,
        workers=args.workers,
        source=args.source,
        start_stagger_seconds=args.start_stagger,
        start_stagger_jitter_seconds=args.start_jitter,
    )


if __name__ == "__main__":
    main()
