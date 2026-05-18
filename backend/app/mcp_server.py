"""smart-crawler MCP 服务器 —— 让 AI Agent 直接发现并调用采集能力。

设计原则（见 playbook「Agents 是新分发渠道」）：Agent 调用的是能力不是界面。
本模块把 smart-crawler 的核心能力暴露成 MCP 工具，Agent 通过 MCP 协议发现、
调用 —— 每个工具描述清晰、参数最少、返回结构化 JSON、错误自解释。

部署：FastAPI 挂载在 /mcp（见 main.py）；亦可独立运行 `python -m app.mcp_server`。
"""
from __future__ import annotations

from fastmcp import FastMCP
from sqlalchemy import func

from .db import SessionLocal
from .models import (Keyword, PriceHistory, Product, Promotion, Review,
                      ShoppingResult, Site)

mcp = FastMCP(
    "smart-crawler",
    instructions=(
        "遨森标杆竞品数据采集平台。为 AI Agent 提供跨境电商竞品情报："
        "竞品商品/价格/促销、消费者口碑评论(VOC)、Google Shopping 竞争格局，"
        "以及按 Amazon ASIN 的评论采集与 AI 口碑分析。"
        "覆盖 9 大家居品牌 46 个独立站 + 21 个评论渠道。所有数据持续采集、结构化。"
    ),
)


def _product(p: Product) -> dict:
    return {
        "sku": p.sku, "title": p.title, "brand": p.brand, "site": p.site,
        "category": p.category_path,
        "sale_price": p.sale_price, "original_price": p.original_price,
        "currency": p.currency, "on_promotion": bool(
            p.original_price and p.sale_price and p.original_price > p.sale_price),
        "rating": p.ratings, "review_count": p.review_count,
        "status": p.status, "url": p.product_url,
    }


@mcp.tool
def list_data_sources() -> list[dict]:
    """列出全部可用数据源：46 个竞品独立站 + 评论平台 + Google Shopping。
    Agent 先调这个了解能查什么品牌/站点/数据类型。"""
    s = SessionLocal()
    try:
        out = []
        for site in s.query(Site).all():
            n = s.query(Product).filter(Product.site == site.site).count()
            out.append({"site": site.site, "brand": site.brand,
                        "country": site.country, "type": "product",
                        "platform": site.platform, "product_count": n})
        for plat in ("trustpilot", "reviews_io", "google_map"):
            n = s.query(Review).filter(Review.platform == plat).count()
            out.append({"source": plat, "type": "review", "review_count": n})
        return out
    finally:
        s.close()


@mcp.tool
def search_competitor_products(
    brand: str | None = None, country: str | None = None,
    keyword: str | None = None, category: str | None = None,
    min_price: float | None = None, max_price: float | None = None,
    on_promotion: bool = False, limit: int = 20,
) -> dict:
    """搜索竞品商品。可按品牌(如 SONGMICS/Costway/Homary/Vidaxl)、国家(US/UK/DE…)、
    标题关键词、品类、价格区间筛选；on_promotion=true 只看在促销的。
    返回结构化商品列表（SKU/标题/价格/促销/评分/状态/URL）。"""
    s = SessionLocal()
    try:
        q = s.query(Product)
        if brand:
            q = q.filter(Product.brand.ilike(f"%{brand}%"))
        if country:
            q = q.filter(Product.site.ilike(f"%_{country.lower()}"))
        if keyword:
            q = q.filter(Product.title.ilike(f"%{keyword}%"))
        if category:
            q = q.filter(Product.category_path.ilike(f"%{category}%"))
        if min_price is not None:
            q = q.filter(Product.sale_price >= min_price)
        if max_price is not None:
            q = q.filter(Product.sale_price <= max_price)
        if on_promotion:
            q = q.filter(Product.original_price > Product.sale_price)
        total = q.count()
        rows = q.order_by(Product.id).limit(min(limit, 100)).all()
        return {"total": total, "returned": len(rows),
                "products": [_product(p) for p in rows]}
    finally:
        s.close()


@mcp.tool
def get_product_detail(site: str, sku: str) -> dict:
    """取单个商品的完整信息 + 历史价格曲线。site 如 songmics_us，sku 为商品编码。"""
    s = SessionLocal()
    try:
        p = s.query(Product).filter(Product.site == site,
                                    Product.sku == sku).first()
        if not p:
            return {"error": f"未找到商品 site={site} sku={sku}"}
        hist = (s.query(PriceHistory)
                .filter(PriceHistory.site == site, PriceHistory.sku == sku)
                .order_by(PriceHistory.date).all())
        d = _product(p)
        d["price_history"] = [{"date": h.date.isoformat(),
                               "sale_price": h.sale_price} for h in hist]
        return d
    finally:
        s.close()


@mcp.tool
def list_promotions(site: str | None = None, limit: int = 30) -> dict:
    """列出竞品当前促销活动（售价低于原价的商品），含折扣率。可按 site 筛选。"""
    s = SessionLocal()
    try:
        q = s.query(Promotion)
        if site:
            q = q.filter(Promotion.site == site)
        total = q.count()
        rows = q.order_by(Promotion.discount_percent.desc()).limit(
            min(limit, 100)).all()
        return {"total": total, "promotions": [{
            "site": r.site, "sku": r.sku, "title": r.product_title,
            "original_price": r.original_price,
            "promotion_price": r.promotion_price,
            "discount_percent": r.discount_percent} for r in rows]}
    finally:
        s.close()


@mcp.tool
def get_voc_reviews(site: str | None = None, platform: str | None = None,
                    sentiment: str | None = None, min_rating: int | None = None,
                    limit: int = 20) -> dict:
    """取消费者口碑评论(VOC)。platform: trustpilot/reviews_io/google_map；
    sentiment: positive/negative/neutral；可按 site、最低评分筛选。
    返回评论 + NLP 情感/分类标注。"""
    s = SessionLocal()
    try:
        q = s.query(Review)
        if site:
            q = q.filter(Review.site == site)
        if platform:
            q = q.filter(Review.platform == platform)
        if sentiment:
            q = q.filter(Review.sentiment == sentiment)
        if min_rating is not None:
            q = q.filter(Review.rating >= min_rating)
        total = q.count()
        rows = q.order_by(Review.review_date.desc()).limit(
            min(limit, 100)).all()
        return {"total": total, "reviews": [{
            "platform": r.platform, "site": r.site, "rating": r.rating,
            "content": r.content, "sentiment": r.sentiment,
            "category": r.category_l1, "sub_category": r.category_l2,
            "review_date": r.review_date.isoformat() if r.review_date else None,
        } for r in rows]}
    finally:
        s.close()


@mcp.tool
def voc_summary(site: str | None = None) -> dict:
    """口碑分析汇总：情感分布 + 痛点分类占比。看竞品/自身的消费者声音全貌。"""
    s = SessionLocal()
    try:
        q = s.query(Review)
        if site:
            q = q.filter(Review.site == site)
        total = q.count()
        sent = dict(q.with_entities(Review.sentiment, func.count(Review.id))
                    .group_by(Review.sentiment).all())
        cats = dict(q.with_entities(Review.category_l1, func.count(Review.id))
                    .filter(Review.category_l1.isnot(None))
                    .group_by(Review.category_l1).all())
        return {"total_reviews": total, "sentiment": sent,
                "pain_categories": dict(sorted(cats.items(),
                                               key=lambda x: -x[1]))}
    finally:
        s.close()


@mcp.tool
def competitor_landscape(keyword: str) -> dict:
    """Google Shopping 竞争格局：某关键词下各商家的出现占有率排名。"""
    s = SessionLocal()
    try:
        rows = s.query(ShoppingResult).filter(
            ShoppingResult.keyword == keyword).all()
        total = len(rows) or 1
        agg: dict = {}
        for r in rows:
            m = r.merchant or "(unknown)"
            agg[m] = agg.get(m, 0) + 1
        share = sorted(({"merchant": m, "count": c,
                         "share_pct": round(c / total * 100, 1)}
                        for m, c in agg.items()), key=lambda x: -x["count"])
        return {"keyword": keyword, "result_count": len(rows),
                "merchant_share": share}
    finally:
        s.close()


@mcp.tool
def amazon_voc_report(asin: str, market: str = "US", limit: int = 100) -> dict:
    """取某亚马逊 ASIN 的真实评论并做 AI 口碑分析（整合自 voc-amazon-reviews）。
    返回情感分布、痛点、卖点、Listing 优化建议、中英文总结。
    asin: 10 位商品编码；market: US/GB/DE/FR/IT/ES/JP/CA 等；limit: 评论数(1-1000)。"""
    from .voc_amazon import VocError, amazon_voc_report as _report
    try:
        return _report(asin, market=market, limit=limit)
    except VocError as exc:
        return {"error": str(exc)}


@mcp.tool
def fetch_amazon_reviews(asin: str, market: str = "US",
                         limit: int = 100) -> dict:
    """只取某亚马逊 ASIN 的原始评论数组（不做分析）。
    适合 Agent 自己接分析管线。返回 {reviews, meta}。"""
    from .voc_amazon import VocError, fetch_amazon_reviews as _fetch
    try:
        return _fetch(asin, market=market, limit=limit)
    except VocError as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    import os
    mcp.run(transport="http", host="0.0.0.0",
            port=int(os.environ.get("MCP_PORT", "8078")))
