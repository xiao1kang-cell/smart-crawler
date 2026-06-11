"""分析层 —— F1-004/005/006：销量·营收估算 + 趋势日汇总。

销量估算逻辑（规格 §4.1.1）：
  评论增量 = 本期评论数 - 上期评论数
  销量 ≈ 评论增量 / 评论率（家居类经验值，默认 2.5%）
首次采集只有 1 个快照、无法算增量时，估算值为 0（如实标注「预估」）。
"""
from __future__ import annotations

from datetime import date, timedelta

from .config import get_settings
from .db import session_scope
from .models import PriceHistory, Product, Trend


def _review_rate() -> float:
    return float(get_settings().get("review_rate", 0.025))


def recompute(site_name: str) -> dict:
    """重算某站点的商品 30 天估算 + 趋势日汇总。"""
    rate = _review_rate()
    with session_scope() as s:
        history = (s.query(PriceHistory)
                   .filter(PriceHistory.site == site_name)
                   .order_by(PriceHistory.date).all())
        # 按 sku 归集快照
        by_sku: dict[str, list[PriceHistory]] = {}
        for h in history:
            by_sku.setdefault(h.sku, []).append(h)

        cutoff = date.today() - timedelta(days=30)
        products = {p.sku: p for p in
                    s.query(Product).filter(Product.site == site_name)}

        # ---- 商品 30 天销量 / 营收估算 ----
        for sku, snaps in by_sku.items():
            p = products.get(sku)
            if p is None:
                continue
            recent = [x for x in snaps if x.date >= cutoff and x.review_count is not None]
            sales = 0
            if len(recent) >= 2:
                delta = (recent[-1].review_count or 0) - (recent[0].review_count or 0)
                sales = max(0, round(delta / rate)) if delta > 0 else 0
            p.thirty_day_sales = sales
            p.thirty_day_revenue = round(sales * (p.sale_price or 0), 2)

        # ---- 趋势日汇总 ----
        dates = sorted({h.date for h in history})
        s.query(Trend).filter(Trend.site == site_name).delete()
        prev_rev: dict[str, int] = {}
        for d in dates:
            day_snaps = [h for h in history if h.date == d]
            sku_count = len({h.sku for h in day_snaps})
            new_count = sum(1 for p in products.values()
                            if p.created_time and p.created_time.date() == d)
            # 当日评论总数 = 各 sku 当日快照 review_count 之和(每 sku 取最后一条)
            day_reviews: dict[str, int] = {}
            for h in day_snaps:
                if h.review_count is not None:
                    day_reviews[h.sku] = h.review_count
            review_total = sum(day_reviews.values())
            # 平均星级:rating 不入历史快照,用当前 Product.ratings 近似当日在售均值
            day_ratings = [products[sku].ratings for sku in day_reviews
                           if products.get(sku) and products[sku].ratings is not None]
            avg_rating = round(sum(day_ratings) / len(day_ratings), 2) if day_ratings else None
            est_sales = 0
            est_revenue = 0.0
            for h in day_snaps:
                if h.review_count is None:
                    continue
                base = prev_rev.get(h.sku)
                if base is not None and h.review_count > base:
                    units = round((h.review_count - base) / rate)
                    est_sales += units
                    est_revenue += units * (h.sale_price or 0)
                prev_rev[h.sku] = h.review_count
            s.add(Trend(
                site=site_name, date=d, sku_count=sku_count,
                new_product_count=new_count, estimated_sales=est_sales,
                estimated_revenue=round(est_revenue, 2),
                avg_rating=avg_rating, review_total=review_total,
            ))
        return {"site": site_name, "trend_days": len(dates),
                "skus": len(by_sku)}
