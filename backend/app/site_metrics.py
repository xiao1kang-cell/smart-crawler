"""Persisted site-level metrics for fast dashboard queries."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import bindparam, func, or_, text
from sqlalchemy.orm import Session

from .currency import currency_for_site
from .models import CrawlUrl, PriceHistory, Product, Promotion, SiteMetric, Trend


METRIC_KEYS = (
    "sku_count",
    "product_listing_count",
    "fetched_count",
    "discovered_product_url_count",
    "price_signal_count",
    "sales_signal_count",
    "revenue_signal_count",
    "review_signal_count",
    "review_history_signal_count",
    "weak_title_count",
    "currency_missing_count",
    "currency_mismatch_count",
    "promotion_count",
    "traffic_signal_count",
    "conversion_signal_count",
    "thirty_day_sales",
    "thirty_day_revenue",
)


def empty_site_metric(site: str) -> dict:
    row = {key: 0 for key in METRIC_KEYS}
    row["site"] = site
    row["last_product_updated"] = None
    row["refreshed_at"] = None
    return row


def site_metric_to_dict(row: SiteMetric) -> dict:
    data = empty_site_metric(row.site)
    for key in METRIC_KEYS:
        data[key] = getattr(row, key) or 0
    data["last_product_updated"] = row.last_product_updated
    data["refreshed_at"] = row.refreshed_at
    return data


def load_site_metrics(
    db: Session,
    sites: list[str],
    *,
    collect_missing: bool = False,
) -> dict[str, dict]:
    site_codes = sorted({site for site in sites if site})
    if not site_codes:
        return {}
    rows = (db.query(SiteMetric)
            .filter(SiteMetric.site.in_(site_codes))
            .all())
    out = {row.site: site_metric_to_dict(row) for row in rows}
    missing = [site for site in site_codes if site not in out]
    if missing and collect_missing:
        out.update(collect_site_metrics(db, missing))
    for site in site_codes:
        out.setdefault(site, empty_site_metric(site))
    return out


def refresh_site_metrics(db: Session, sites: list[str] | None = None) -> int:
    """Recompute metrics for the requested sites.

    This is intentionally synchronous but kept off request paths. Workers call it
    for one site after a crawl; maintenance scripts can call it for all sites.
    """
    site_codes = _normalize_sites(db, sites)
    if not site_codes:
        return 0
    collected = collect_site_metrics(db, site_codes)

    now = datetime.utcnow()
    existing = {
        row.site: row
        for row in db.query(SiteMetric).filter(SiteMetric.site.in_(site_codes)).all()
    }
    for site in site_codes:
        row = existing.get(site)
        if row is None:
            row = SiteMetric(site=site)
            db.add(row)
        product = collected.get(site, {})
        row.sku_count = int(product.get("sku_count") or 0)
        row.product_listing_count = int(product.get("product_listing_count") or 0)
        row.fetched_count = int(product.get("fetched_count") or 0)
        row.discovered_product_url_count = int(product.get("discovered_product_url_count") or 0)
        row.price_signal_count = int(product.get("price_signal_count") or 0)
        row.sales_signal_count = int(product.get("sales_signal_count") or 0)
        row.revenue_signal_count = int(product.get("revenue_signal_count") or 0)
        row.review_signal_count = int(product.get("review_signal_count") or 0)
        row.review_history_signal_count = int(product.get("review_history_signal_count") or 0)
        row.weak_title_count = int(product.get("weak_title_count") or 0)
        row.currency_missing_count = int(product.get("currency_missing_count") or 0)
        row.currency_mismatch_count = int(product.get("currency_mismatch_count") or 0)
        row.promotion_count = int(product.get("promotion_count") or 0)
        row.traffic_signal_count = int(product.get("traffic_signal_count") or 0)
        row.conversion_signal_count = int(product.get("conversion_signal_count") or 0)
        row.thirty_day_sales = int(product.get("thirty_day_sales") or 0)
        row.thirty_day_revenue = float(product.get("thirty_day_revenue") or 0)
        row.last_product_updated = product.get("last_product_updated")
        row.refreshed_at = now
    return len(site_codes)


def collect_site_metrics(db: Session, sites: list[str]) -> dict[str, dict]:
    site_codes = sorted({site for site in sites if site})
    if not site_codes:
        return {}
    product_rows = _product_metrics(db, site_codes)
    fetched_counts = _fetched_counts(db, site_codes)
    discovered_counts = _discovered_product_counts(db, site_codes)
    review_history_counts = _review_history_counts(db, site_codes)
    currency_counts = _currency_quality(db, site_codes)
    promotion_counts = _promotion_counts(db, site_codes)
    trend_counts = _trend_counts(db, site_codes)
    out: dict[str, dict] = {}
    for site in site_codes:
        row = empty_site_metric(site)
        row.update(product_rows.get(site, {}))
        row["fetched_count"] = int(fetched_counts.get(site) or 0)
        row["discovered_product_url_count"] = int(discovered_counts.get(site) or 0)
        row["review_history_signal_count"] = int(review_history_counts.get(site) or 0)
        currency = currency_counts.get(site, {})
        row["currency_missing_count"] = int(currency.get("missing") or 0)
        row["currency_mismatch_count"] = int(currency.get("mismatch") or 0)
        row["promotion_count"] = int(promotion_counts.get(site) or 0)
        trend = trend_counts.get(site, {})
        row["traffic_signal_count"] = int(trend.get("traffic_signal_count") or 0)
        row["conversion_signal_count"] = int(trend.get("conversion_signal_count") or 0)
        out[site] = row
    return out


def _normalize_sites(db: Session, sites: list[str] | None) -> list[str]:
    if sites is not None:
        return sorted({site for site in sites if site})
    rows = db.query(Product.site).filter(Product.site.isnot(None)).distinct().all()
    return sorted({site for (site,) in rows if site})


def _product_metrics(db: Session, sites: list[str]) -> dict[str, dict]:
    weak_title_expr = or_(
        func.length(func.trim(func.coalesce(Product.title, ""))) == 0,
        func.length(func.trim(func.coalesce(Product.title, ""))) < 4,
        func.lower(func.trim(func.coalesce(Product.title, ""))).in_(
            ("product", "item", "sku", "untitled", "detail", "details",
             "view product", "shop now")
        ),
        func.lower(func.trim(func.coalesce(Product.title, ""))) ==
        func.lower(func.trim(func.coalesce(Product.sku, ""))),
    )
    rows = (db.query(
        Product.site,
        func.count(Product.id),
        func.count(func.distinct(func.coalesce(Product.spu, Product.sku))),
        func.count(Product.id).filter(
            func.coalesce(Product.sale_price, Product.original_price, 0) > 0
        ),
        func.count(Product.id).filter(func.coalesce(Product.thirty_day_sales, 0) > 0),
        func.count(Product.id).filter(func.coalesce(Product.thirty_day_revenue, 0) > 0),
        func.count(Product.id).filter(func.coalesce(Product.review_count, 0) > 0),
        func.count(Product.id).filter(weak_title_expr),
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
        func.max(Product.updated_time),
    ).filter(Product.site.in_(sites)).group_by(Product.site).all())
    return {
        site: {
            "sku_count": sku_count,
            "product_listing_count": product_listing_count,
            "price_signal_count": price_signal_count,
            "sales_signal_count": sales_signal_count,
            "revenue_signal_count": revenue_signal_count,
            "review_signal_count": review_signal_count,
            "weak_title_count": weak_title_count,
            "thirty_day_sales": thirty_day_sales,
            "thirty_day_revenue": thirty_day_revenue,
            "last_product_updated": last_product_updated,
        }
        for (site, sku_count, product_listing_count, price_signal_count,
             sales_signal_count, revenue_signal_count, review_signal_count,
             weak_title_count, thirty_day_sales, thirty_day_revenue,
             last_product_updated) in rows
    }


def _fetched_counts(db: Session, sites: list[str]) -> dict[str, int]:
    stmt = (text("SELECT site, count(*) FROM fetched_urls "
                 "WHERE site IN :sites GROUP BY site")
            .bindparams(bindparam("sites", expanding=True)))
    try:
        return {row[0]: int(row[1] or 0)
                for row in db.execute(stmt, {"sites": sites}).all()}
    except Exception:
        db.rollback()
        return {}


def _discovered_product_counts(db: Session, sites: list[str]) -> dict[str, int]:
    rows = (db.query(CrawlUrl.site, func.count(func.distinct(CrawlUrl.url)))
            .filter(CrawlUrl.site.in_(sites),
                    CrawlUrl.kind == "product",
                    CrawlUrl.url.isnot(None))
            .group_by(CrawlUrl.site)
            .all())
    return {site: int(count or 0) for site, count in rows}


def _review_history_counts(db: Session, sites: list[str]) -> dict[str, int]:
    grouped = (db.query(
        PriceHistory.site.label("site"),
        PriceHistory.sku.label("sku"),
    ).filter(PriceHistory.site.in_(sites),
             PriceHistory.review_count.isnot(None))
     .group_by(PriceHistory.site, PriceHistory.sku)
     .having(func.count(func.distinct(PriceHistory.date)) >= 2)
     .subquery())
    rows = db.query(grouped.c.site, func.count()).group_by(grouped.c.site).all()
    return {site: int(count or 0) for site, count in rows}


def _currency_quality(db: Session, sites: list[str]) -> dict[str, dict[str, int]]:
    out = {site: {"missing": 0, "mismatch": 0} for site in sites}
    rows = (db.query(Product.site, Product.currency, func.count(Product.id))
            .filter(Product.site.in_(sites))
            .group_by(Product.site, Product.currency)
            .all())
    for site, currency, count in rows:
        expected = currency_for_site(site)
        if not expected:
            continue
        value = str(currency or "").strip().upper()
        n = int(count or 0)
        if not value:
            out.setdefault(site, {"missing": 0, "mismatch": 0})["missing"] += n
        elif value != expected:
            out.setdefault(site, {"missing": 0, "mismatch": 0})["mismatch"] += n
    return out


def _promotion_counts(db: Session, sites: list[str]) -> dict[str, int]:
    rows = (db.query(Promotion.site, func.count(Promotion.id))
            .filter(Promotion.site.in_(sites))
            .group_by(Promotion.site)
            .all())
    return {site: int(count or 0) for site, count in rows}


def _trend_counts(db: Session, sites: list[str]) -> dict[str, dict[str, int]]:
    rows = (db.query(
        Trend.site,
        func.count(Trend.id).filter(Trend.traffic.isnot(None)),
        func.count(Trend.id).filter(Trend.conversion_rate.isnot(None)),
    ).filter(Trend.site.in_(sites)).group_by(Trend.site).all())
    return {
        site: {
            "traffic_signal_count": int(traffic or 0),
            "conversion_signal_count": int(conversion or 0),
        }
        for site, traffic, conversion in rows
    }
