"""Backfill Crate&Barrel prices from the browse-model JSON endpoint.

Run inside the app container:
  python /tmp/aosen_cratebarrel_price_backfill.py --limit 100
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.crawlers.cratebarrel import CrateBarrelCrawler
from app.db import SessionLocal
from app.models import PriceHistory, Product, Site
from sqlalchemy import text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="cratebarrel_us")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--sleep-every", type=int, default=25)
    args = parser.parse_args()
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        raise SystemExit("--shard must be in [0, --shards)")

    db = SessionLocal()
    try:
        site = db.query(Site).filter(Site.site == args.site).first()
        if site is None:
            raise SystemExit(f"site not found: {args.site}")
        crawler = CrateBarrelCrawler(site, limit=args.limit)
        query = (
            db.query(Product)
            .filter(Product.site == args.site)
            .filter((Product.sale_price == None) & (Product.original_price == None))  # noqa: E711
            .filter(Product.sku != None)  # noqa: E711
        )
        if args.shards > 1:
            query = query.filter(
                text(f"(products.id % {args.shards}) = {args.shard}"))
        rows = (
            query.order_by(Product.updated_time.desc().nullslast(), Product.id)
            .limit(args.limit)
            .all()
        )
        print(
            f"selected={len(rows)} site={args.site} limit={args.limit} "
            f"shard={args.shard}/{args.shards}",
            flush=True,
        )
        updated = 0
        failed = 0
        fetcher = crawler.make_fetcher(
            kind="product", source="cratebarrel_browse_model_backfill")
        today = date.today()
        for idx, product in enumerate(rows, 1):
            row = {
                "sku": product.sku,
                "title": product.title,
                "image_urls": product.image_urls or [],
                "currency": product.currency or "USD",
                "description": product.description,
                "status": product.status,
            }
            url = f"{crawler.base}/single-product-page/get-browse-model/{product.sku}"
            try:
                res = fetcher.get(
                    url,
                    headers={
                        **crawler._headers(),
                        "Accept": "application/json,text/plain,*/*",
                        "Referer": crawler.base + "/",
                    },
                    timeout=25,
                )
                if (res.status or 0) != 200 or not (res.text or "").lstrip().startswith("{"):
                    failed += 1
                    print(f"{idx} status={res.status} skip sku={product.sku}", flush=True)
                    continue
                import json
                data = json.loads(res.text)
                if not crawler._merge_from_browse_model(row, data):
                    failed += 1
                    print(f"{idx} no_price sku={product.sku}", flush=True)
                    continue
                product.sale_price = row.get("sale_price")
                product.original_price = row.get("original_price")
                product.currency = row.get("currency") or product.currency or "USD"
                product.ratings = row.get("ratings") or product.ratings
                if row.get("review_count") is not None:
                    product.review_count = row.get("review_count")
                elif product.review_count is None:
                    product.review_count = 0
                product.description = row.get("description") or product.description
                product.image_urls = row.get("image_urls") or product.image_urls
                product.status = row.get("status") or product.status
                product.updated_time = datetime.utcnow()

                hist = (
                    db.query(PriceHistory)
                    .filter(PriceHistory.site == args.site)
                    .filter(PriceHistory.sku == product.sku)
                    .filter(PriceHistory.date == today)
                    .order_by(PriceHistory.id.desc())
                    .first()
                )
                if hist is None:
                    hist = PriceHistory(site=args.site, sku=product.sku, date=today)
                    db.add(hist)
                hist.sale_price = product.sale_price
                hist.original_price = product.original_price
                hist.review_count = product.review_count
                updated += 1
                print(f"{idx} updated sku={product.sku} price={product.sale_price}", flush=True)
                if args.sleep_every and idx % args.sleep_every == 0:
                    db.commit()
                    crawler.sleep()
            except Exception as exc:
                failed += 1
                print(
                    f"{idx} error sku={product.sku} {type(exc).__name__}: {exc}",
                    flush=True,
                )
        db.commit()
        print(f"done selected={len(rows)} updated={updated} failed={failed}", flush=True)
        return 0 if updated else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
