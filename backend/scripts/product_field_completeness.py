#!/usr/bin/env python3
"""Audit and repair product field completeness safely.

Examples:
  python scripts/product_field_completeness.py audit
  python scripts/product_field_completeness.py backfill-reviews --sites vidaxl_es vidaxl_fr --apply
  python scripts/product_field_completeness.py mark-non-products --sites costway_de costway_es --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import String, func, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Product, Site  # noqa: E402
from app.product_quality import costway_non_product_filter, salable_product_filter  # noqa: E402


DEFAULT_EXCLUDED = {"sephora_fr_maquillage", "costway_ca", "costway_us"}
DEFAULT_BATCH = 5_000
DEFAULT_TIMEOUT_SEC = 45
COMMANDS = {"audit", "backfill-reviews", "mark-non-products"}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow common options before or after the subcommand.

    argparse subparsers normally require shared options after the command. This
    small shuffle keeps operator usage forgiving for one-off production runs.
    """
    for idx, token in enumerate(argv):
        if token in COMMANDS:
            if idx == 0:
                return argv
            return [token, *argv[:idx], *argv[idx + 1:]]
    return argv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and repair required product fields without long transactions."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--exclude", nargs="*", default=sorted(DEFAULT_EXCLUDED))
    common.add_argument("--sites", nargs="*", default=[])
    common.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    common.add_argument("--max-batches", type=int, default=80)
    common.add_argument("--statement-timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    common.add_argument("--apply", action="store_true")
    common.add_argument(
        "--init-db",
        action="store_true",
        help="Run init_db() before operating. Off by default for least-privilege production users.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "audit",
        parents=[common],
        help="Report salable rows missing price/image/category/review_count.",
    )
    sub.add_parser(
        "backfill-reviews",
        parents=[common],
        help="Set missing review_count to 0 in small batches.",
    )
    sub.add_parser(
        "mark-non-products",
        parents=[common],
        help="Mark known non-product Costway rows as discovered.",
    )
    return parser.parse_args(_normalize_argv(sys.argv[1:]))


def _target_sites(session, args: argparse.Namespace) -> list[str]:
    excluded = set(args.exclude or [])
    if args.sites:
        return [site for site in args.sites if site not in excluded]
    rows = session.query(Site.site).order_by(Site.site).all()
    return [row[0] for row in rows if row[0] not in excluded]


def _set_timeout(session, seconds: int) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text(f"set statement_timeout='{max(1, seconds)}s'"))


def _set_local_timeout(conn, seconds: int) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"set local statement_timeout='{max(1, seconds)}s'"))


def audit(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        _set_timeout(session, args.statement_timeout_sec)
        sites = _target_sites(session, args)
        print(f"sites={len(sites)} excluded={','.join(args.exclude or [])}", flush=True)
        gap_sites = 0
        for site in sites:
            try:
                query = (
                    session.query(Product)
                    .filter(Product.site == site)
                    .filter(salable_product_filter())
                )
                salable = query.count()
                if not salable:
                    continue
                image_text = Product.image_urls.cast(String)
                category_text = func.btrim(Product.category_path.cast(String))
                counts = {
                    "salable": salable,
                    "price_missing": query.filter(Product.sale_price.is_(None)).count(),
                    "image_missing": query.filter(
                        Product.image_urls.is_(None)
                        | image_text.in_(("[]", "{}", "null", ""))
                    ).count(),
                    "category_missing": query.filter(
                        Product.category_path.is_(None)
                        | category_text.in_(("", "[]", "{}", "null"))
                    ).count(),
                    "review_missing": query.filter(Product.review_count.is_(None)).count(),
                }
            except Exception as exc:
                session.rollback()
                print(f"ERR {site} {type(exc).__name__}: {str(exc)[:180]}", flush=True)
                continue
            if any(value for key, value in counts.items() if key != "salable"):
                gap_sites += 1
                print(f"GAP {site} {counts}", flush=True)
        print(f"gap_sites={gap_sites}", flush=True)
        return 0
    finally:
        session.close()


def backfill_reviews(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        sites = _target_sites(session, args)
    finally:
        session.close()

    print(
        f"mode={'apply' if args.apply else 'dry-run'} sites={len(sites)} "
        f"batch={args.batch} max_batches={args.max_batches}",
        flush=True,
    )
    total = 0
    for site in sites:
        site_total = 0
        print(f"SITE_START {site}", flush=True)
        for idx in range(1, args.max_batches + 1):
            with engine.begin() as conn:
                _set_local_timeout(conn, args.statement_timeout_sec)
                if args.apply:
                    result = conn.execute(
                        text(
                            """
                            with todo as (
                              select id
                              from products
                              where site=:site and review_count is null
                              limit :batch
                            )
                            update products p
                            set review_count=0
                            from todo
                            where p.id=todo.id
                            """
                        ),
                        {"site": site, "batch": args.batch},
                    )
                    updated = int(result.rowcount or 0)
                else:
                    updated = int(
                        conn.execute(
                            text(
                                """
                                select count(*) from (
                                  select id
                                  from products
                                  where site=:site and review_count is null
                                  limit :batch
                                ) todo
                                """
                            ),
                            {"site": site, "batch": args.batch},
                        ).scalar()
                        or 0
                    )
            label = "updated" if args.apply else "pending"
            if not updated:
                print(f"SITE_DONE {site} {label}={site_total}", flush=True)
                break
            site_total += updated
            total += updated
            if idx == 1 or idx % 10 == 0 or updated < args.batch:
                print(
                    f"batch={idx} site={site} {label}={updated} "
                    f"site_total={site_total} total={total}",
                    flush=True,
                )
            if not args.apply:
                break
            time.sleep(0.03)
        else:
            label = "updated" if args.apply else "pending"
            print(f"SITE_STOP_LIMIT {site} {label}={site_total}", flush=True)
    print(f"TOTAL {'updated' if args.apply else 'pending'}={total}", flush=True)
    return 0


def mark_non_products(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        sites = _target_sites(session, args)
    finally:
        session.close()
    costway_sites = [site for site in sites if site.startswith("costway_")]
    print(
        f"mode={'apply' if args.apply else 'dry-run'} costway_sites={len(costway_sites)}",
        flush=True,
    )
    total = 0
    for site in costway_sites:
        if args.apply:
            # Re-run with ORM expression for correctness and report exact rows.
            session = SessionLocal()
            try:
                _set_timeout(session, args.statement_timeout_sec)
                query = (
                    session.query(Product)
                    .filter(Product.site == site)
                    .filter(
                        func.lower(func.trim(func.coalesce(Product.status, ""))).in_(
                            ("", "active", "available", "in_stock", "instock", "on_sale")
                        )
                    )
                    .filter(costway_non_product_filter())
                )
                updated = query.update({"status": "discovered"}, synchronize_session=False)
                session.commit()
            finally:
                session.close()
        else:
            session = SessionLocal()
            try:
                _set_timeout(session, args.statement_timeout_sec)
                updated = (
                    session.query(Product)
                    .filter(Product.site == site)
                    .filter(
                        func.lower(func.trim(func.coalesce(Product.status, ""))).in_(
                            ("", "active", "available", "in_stock", "instock", "on_sale")
                        )
                    )
                    .filter(costway_non_product_filter())
                    .count()
                )
            finally:
                session.close()
        total += int(updated or 0)
        print(f"SITE {site} rows={updated}", flush=True)
    print(f"TOTAL rows={total}", flush=True)
    return 0


def main() -> int:
    args = _parse_args()
    if args.init_db:
        init_db()
    if args.command == "audit":
        return audit(args)
    if args.command == "backfill-reviews":
        return backfill_reviews(args)
    if args.command == "mark-non-products":
        return mark_non_products(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
