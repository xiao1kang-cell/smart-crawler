"""Backfill Sephora FR prices/reviews from existing PDP URLs.

Run inside the app container:
  PYTHONPATH=/app/backend python /tmp/aosen_sephora_fr_price_backfill.py --limit 100
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.crawlers.sephora import SephoraCrawler
from app.db import SessionLocal
from app.models import PriceHistory, Product, Site


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="sephora_fr_maquillage")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--commit-every", type=int, default=20)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        site = db.query(Site).filter(Site.site == args.site).first()
        if site is None:
            raise SystemExit(f"site not found: {args.site}")
        crawler = SephoraCrawler(site)
        fetcher = crawler.make_fetcher(
            kind="product",
            source="aosen_sephora_fr_price_backfill",
            fail_fast_blocked=False,
            retries=0,
        )
        rows = (
            db.query(Product)
            .join(PriceHistory, (PriceHistory.site == Product.site)
                  & (PriceHistory.sku == Product.sku))
            .filter(PriceHistory.date == date.today())
            .filter(PriceHistory.site == args.site)
            .filter((PriceHistory.sale_price == None)
                    & (PriceHistory.original_price == None))  # noqa: E711
            .filter(Product.product_url != None)  # noqa: E711
            .order_by(PriceHistory.id)
            .limit(args.limit)
            .all()
        )
        print(f"selected={len(rows)} site={args.site} limit={args.limit}", flush=True)
        today = date.today()
        updated = 0
        failed = 0
        for idx, product in enumerate(rows, 1):
            try:
                res = fetcher.get(
                    product.product_url,
                    headers=crawler._headers(),
                    timeout=35,
                )
                if (res.status or 0) != 200 or crawler._blocked(res.text):
                    failed += 1
                    print(
                        f"{idx} status={res.status} skip sku={product.sku}",
                        flush=True,
                    )
                    continue
                row = crawler._parse_fr_pdp(res.text or "", product.product_url)
                if not row or not (row.get("sale_price") or row.get("original_price")):
                    failed += 1
                    print(f"{idx} no_price sku={product.sku}", flush=True)
                    continue

                product.title = row.get("title") or product.title
                product.description = row.get("description") or product.description
                product.image_urls = row.get("image_urls") or product.image_urls
                product.category_path = row.get("category_path") or product.category_path
                product.sale_price = row.get("sale_price")
                product.original_price = row.get("original_price") or row.get("sale_price")
                product.currency = row.get("currency") or product.currency or "EUR"
                product.ratings = row.get("ratings") or product.ratings
                product.review_count = (
                    row.get("review_count")
                    if row.get("review_count") is not None
                    else product.review_count or 0
                )
                product.status = row.get("status") or product.status
                product.updated_time = datetime.utcnow()

                hist = (
                    db.query(PriceHistory)
                    .filter(PriceHistory.site == product.site)
                    .filter(PriceHistory.sku == product.sku)
                    .filter(PriceHistory.date == today)
                    .order_by(PriceHistory.id.desc())
                    .first()
                )
                if hist is None:
                    hist = PriceHistory(site=product.site, sku=product.sku, date=today)
                    db.add(hist)
                hist.sale_price = product.sale_price
                hist.original_price = product.original_price
                hist.review_count = product.review_count
                db.flush()
                updated += 1
                print(
                    f"{idx} updated sku={product.sku} price={product.sale_price} "
                    f"reviews={product.review_count}",
                    flush=True,
                )
                if args.commit_every and idx % args.commit_every == 0:
                    db.commit()
                    crawler.sleep()
            except Exception as exc:
                failed += 1
                db.rollback()
                print(
                    f"{idx} error sku={product.sku} {type(exc).__name__}: {exc}",
                    flush=True,
                )
        db.commit()
        print(f"done selected={len(rows)} updated={updated} failed={failed}", flush=True)
        return 0 if updated or not rows else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
