"""Backfill Magento product prices from existing product URLs.

Run inside the app container:
  PYTHONPATH=/app/backend python /tmp/aosen_magento_price_backfill.py \
    --sites costway_de,costway_es --limit 100
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from sqlalchemy import bindparam, text

from app.crawlers.magento import MagentoCrawler
from app.db import SessionLocal
from app.models import PriceHistory, Product, Site


def _site_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sites",
        default="costway_de,costway_es,costway_fr,costway_it,costway_nl,costway_uk",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--commit-every", type=int, default=20)
    parser.add_argument(
        "--history-date",
        default="",
        help="Price history date to backfill (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--source",
        choices=("history", "products"),
        default="history",
        help="history uses today's price_history missing-price rows first.",
    )
    parser.add_argument(
        "--url-contains",
        default="",
        help="Optional substring that product_url must contain.",
    )
    parser.add_argument(
        "--url-not-contains",
        default="",
        help="Optional substring that product_url must not contain.",
    )
    parser.add_argument(
        "--exclude-slug-title",
        action="store_true",
        help=(
            "Skip likely category pages whose title is just the URL slug with "
            "hyphens converted to spaces."
        ),
    )
    args = parser.parse_args()
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        raise SystemExit("--shard must be in [0, --shards)")

    sites = _site_list(args.sites)
    if not sites:
        raise SystemExit("--sites is empty")
    history_day = date.fromisoformat(args.history_date) if args.history_date else date.today()

    db = SessionLocal()
    try:
        site_rows = db.query(Site).filter(Site.site.in_(sites)).all()
        crawlers: dict[str, MagentoCrawler] = {}
        for site in site_rows:
            crawler = MagentoCrawler(site)
            crawler._fetcher = crawler.make_fetcher(
                kind="product",
                source="aosen_magento_price_backfill",
                fail_fast_blocked=False,
                retries=0,
            )
            crawlers[site.site] = crawler

        missing_sites = sorted(set(sites) - set(crawlers))
        if missing_sites:
            raise SystemExit(f"site not found: {','.join(missing_sites)}")

        if args.source == "history":
            shard_clause = (
                f"and (p.id % {args.shards}) = {args.shard}"
                if args.shards > 1 else ""
            )
            url_clauses = []
            if args.url_contains:
                url_clauses.append("and p.product_url like :url_like")
            if args.url_not_contains:
                url_clauses.append("and p.product_url not like :url_not_like")
            if args.exclude_slug_title:
                url_clauses.append(
                    "and lower(trim(coalesce(p.title, ''))) "
                    "<> lower(regexp_replace(p.sku, '[-_]+', ' ', 'g'))"
                )
            url_clause = "\n                      ".join(url_clauses)
            stmt = text(f"""
                    select p.id, p.site, p.sku, p.product_url
                    from price_history h
                    join products p on p.site = h.site and p.sku = h.sku
                    where h.date = :history_date
                      and h.site in :sites
                      and coalesce(h.sale_price, h.original_price, 0) <= 0
                      and p.product_url is not null
                      {url_clause}
                      {shard_clause}
                    order by h.site, h.id
                    limit :limit
                """).bindparams(bindparam("sites", expanding=True))
            params = {"sites": sites, "limit": args.limit, "history_date": history_day}
            if args.url_contains:
                params["url_like"] = f"%{args.url_contains}%"
            if args.url_not_contains:
                params["url_not_like"] = f"%{args.url_not_contains}%"
            rows = db.execute(
                stmt,
                params,
            ).all()
        else:
            query = (
                db.query(Product.id, Product.site, Product.sku, Product.product_url)
                .filter(Product.site.in_(sites))
                .filter((Product.sale_price == None) & (Product.original_price == None))  # noqa: E711
                .filter(Product.product_url != None)  # noqa: E711
            )
            if args.url_contains:
                query = query.filter(Product.product_url.like(f"%{args.url_contains}%"))
            if args.url_not_contains:
                query = query.filter(~Product.product_url.like(f"%{args.url_not_contains}%"))
            if args.exclude_slug_title:
                query = query.filter(
                    text(
                        "lower(trim(coalesce(products.title, ''))) "
                        "<> lower(regexp_replace(products.sku, '[-_]+', ' ', 'g'))"
                    )
                )
            if args.shards > 1:
                query = query.filter(text(f"(products.id % {args.shards}) = {args.shard}"))
            rows = query.order_by(Product.site, Product.id).limit(args.limit).all()
        print(
            f"selected={len(rows)} sites={','.join(sites)} limit={args.limit} "
            f"shard={args.shard}/{args.shards}",
            flush=True,
        )

        updated = 0
        failed = 0
        for idx, item in enumerate(rows, 1):
            crawler = crawlers[item.site]
            try:
                row = crawler._fetch_one(item.product_url)
                if not row or not (row.get("sale_price") or row.get("original_price")):
                    failed += 1
                    print(f"{idx} no_price site={item.site} sku={item.sku}", flush=True)
                    continue

                product = db.get(Product, item.id)
                if product is None:
                    failed += 1
                    print(f"{idx} missing_product id={item.id}", flush=True)
                    continue
                product.title = row.get("title") or product.title
                product.description = row.get("description") or product.description
                product.image_urls = row.get("image_urls") or product.image_urls
                product.category_path = row.get("category_path") or product.category_path
                product.sale_price = row.get("sale_price")
                product.original_price = row.get("original_price") or row.get("sale_price")
                product.currency = row.get("currency") or product.currency
                product.ratings = row.get("ratings") or product.ratings
                if row.get("review_count") is not None:
                    product.review_count = row.get("review_count")
                elif product.review_count is None:
                    product.review_count = 0
                product.status = row.get("status") or product.status
                product.updated_time = datetime.utcnow()

                hist = (
                    db.query(PriceHistory)
                    .filter(PriceHistory.site == product.site)
                    .filter(PriceHistory.sku == product.sku)
                    .filter(PriceHistory.date == history_day)
                    .order_by(PriceHistory.id.desc())
                    .first()
                )
                if hist is None:
                    hist = PriceHistory(site=product.site, sku=product.sku, date=history_day)
                    db.add(hist)
                hist.sale_price = product.sale_price
                hist.original_price = product.original_price
                hist.review_count = product.review_count
                db.flush()
                updated += 1
                print(
                    f"{idx} updated site={product.site} sku={product.sku} "
                    f"price={product.sale_price}",
                    flush=True,
                )
                if args.commit_every and idx % args.commit_every == 0:
                    db.commit()
            except Exception as exc:
                failed += 1
                db.rollback()
                print(
                    f"{idx} error site={item.site} sku={item.sku} "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
        db.commit()
        print(f"done selected={len(rows)} updated={updated} failed={failed}", flush=True)
        return 0 if updated or not rows else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
