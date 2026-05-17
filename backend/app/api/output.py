"""数据输出模块 v1 —— 面向 AI Agent 的结构化数据 API。

参考 Scrapling 的「干净结构化输出」理念：
  · 统一信封（object / data / count / total / has_more），Stripe/OpenAI 风格
  · 稳定字段 schema，站点改版由采集层吸收，Agent 侧零感知
  · Bearer Token 或 X-API-Key 均可鉴权 —— Agent 用密钥直连

挂载于 /api/v1/*，与看板 API（/api/*）分离，作为对外稳定数据契约。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (Category, PriceHistory, Product, Promotion, Review,
                      Site, Trend)
from .routes import require_user

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)],
                   tags=["v1 数据输出（Agent API）"])


def _envelope(data: list, total=None, page=None, page_size=None) -> dict:
    out = {"object": "list", "count": len(data), "data": data}
    if total is not None:
        out["total"] = total
        out["has_more"] = bool(page and page * page_size < total)
    return out


def _iso(v):
    return v.isoformat() if v else None


def _product(p: Product) -> dict:
    """干净的商品 schema —— 对外稳定契约。"""
    return {
        "sku": p.sku, "spu": p.spu, "site": p.site, "brand": p.brand,
        "title": p.title, "category": p.category_path,
        "price": {"sale": p.sale_price, "original": p.original_price,
                  "currency": p.currency,
                  "on_promotion": bool(p.original_price and p.sale_price
                                       and p.original_price > p.sale_price)},
        "rating": {"score": p.ratings, "review_count": p.review_count},
        "estimate": {"thirty_day_sales": p.thirty_day_sales,
                     "thirty_day_revenue": p.thirty_day_revenue},
        "status": p.status, "inventory": p.inventory,
        "attributes": p.attributes or {}, "tags": p.tags or [],
        "label": p.label, "is_new": bool(p.is_new),
        "is_bestseller": bool(p.is_bestseller),
        "has_video": bool(p.has_video),
        "images": p.image_urls or [], "url": p.product_url,
        "identifiers": {"mpn": p.mpn, "gtin": p.gtin,
                        "variant_id": p.variant_id},
        "first_seen": _iso(p.created_time), "last_seen": _iso(p.updated_time),
    }


@router.get("")
def index(caller: str = Depends(require_user)):
    """API 索引 —— 供 Agent 发现可用端点。"""
    return {
        "service": "smart-crawler data API", "version": "v1",
        "auth": "Bearer <token> 或 X-API-Key: sck_…",
        "endpoints": {
            "GET /api/v1/sites": "标杆站点清单 + 指标",
            "GET /api/v1/products": "商品查询（site/brand/category/price/status/label 过滤，分页）",
            "GET /api/v1/products/{site}/{sku}": "单个商品 + 价格历史",
            "GET /api/v1/promotions": "促销活动",
            "GET /api/v1/trends": "趋势日序列",
            "GET /api/v1/site/{site}": "单站点完整快照（指标 + 商品 + 促销 + 趋势）",
        },
    }


@router.get("/sites")
def v1_sites(db: Session = Depends(get_db)):
    rows = []
    for s in db.query(Site).all():
        sku = db.query(Product).filter(Product.site == s.site).count()
        rows.append({
            "site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform, "sku_count": sku,
            "last_crawled": _iso(s.last_crawled),
        })
    return _envelope(rows)


@router.get("/products")
def v1_products(
    site: str | None = None, brand: str | None = None,
    category: str | None = None, status: str | None = None,
    label: str | None = None, min_price: float | None = None,
    max_price: float | None = None, search: str | None = None,
    page: int = 1, page_size: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(Product)
    if site:
        q = q.filter(Product.site == site)
    if brand:
        q = q.filter(Product.brand == brand)
    if category:
        q = q.filter(Product.category_path.ilike(f"%{category}%"))
    if status:
        q = q.filter(Product.status == status)
    if label == "new":
        q = q.filter(Product.is_new.is_(True))
    elif label == "bestseller":
        q = q.filter(Product.is_bestseller.is_(True))
    if min_price is not None:
        q = q.filter(Product.sale_price >= min_price)
    if max_price is not None:
        q = q.filter(Product.sale_price <= max_price)
    if search:
        like = f"%{search}%"
        q = q.filter((Product.title.ilike(like)) | (Product.sku.ilike(like)))
    total = q.count()
    rows = (q.order_by(Product.id)
            .offset((page - 1) * page_size).limit(page_size).all())
    return _envelope([_product(p) for p in rows], total, page, page_size)


@router.get("/products/{site}/{sku}")
def v1_product(site: str, sku: str, db: Session = Depends(get_db)):
    p = (db.query(Product)
         .filter(Product.site == site, Product.sku == sku).first())
    if not p:
        raise HTTPException(404, "商品不存在")
    hist = (db.query(PriceHistory)
            .filter(PriceHistory.site == site, PriceHistory.sku == sku)
            .order_by(PriceHistory.date).all())
    d = _product(p)
    d["price_history"] = [{"date": h.date.isoformat(),
                           "sale": h.sale_price, "original": h.original_price,
                           "review_count": h.review_count} for h in hist]
    return d


@router.get("/promotions")
def v1_promotions(site: str | None = None, page: int = 1,
                  page_size: int = Query(50, le=200),
                  db: Session = Depends(get_db)):
    q = db.query(Promotion)
    if site:
        q = q.filter(Promotion.site == site)
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    data = [{
        "sku": r.sku, "site": r.site, "type": r.promotion_type,
        "name": r.promotion_name, "original_price": r.original_price,
        "promotion_price": r.promotion_price,
        "discount_percent": r.discount_percent, "threshold": r.threshold,
        "product_title": r.product_title, "detected_at": _iso(r.detected_time),
    } for r in rows]
    return _envelope(data, total, page, page_size)


@router.get("/trends")
def v1_trends(site: str, db: Session = Depends(get_db)):
    rows = (db.query(Trend).filter(Trend.site == site)
            .order_by(Trend.date).all())
    return _envelope([{
        "date": t.date.isoformat(), "sku_count": t.sku_count,
        "new_product_count": t.new_product_count,
        "estimated_sales": t.estimated_sales,
        "estimated_revenue": t.estimated_revenue,
    } for t in rows])


@router.get("/reviews")
def v1_reviews(site: str | None = None, platform: str | None = None,
               min_rating: int | None = None, max_rating: int | None = None,
               page: int = 1, page_size: int = Query(50, le=200),
               db: Session = Depends(get_db)):
    """口碑评论查询 —— 模块二。"""
    q = db.query(Review)
    if site:
        q = q.filter(Review.site == site)
    if platform:
        q = q.filter(Review.platform == platform)
    if min_rating is not None:
        q = q.filter(Review.rating >= min_rating)
    if max_rating is not None:
        q = q.filter(Review.rating <= max_rating)
    total = q.count()
    rows = (q.order_by(Review.review_date.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    data = [{
        "review_id": r.review_id, "platform": r.platform, "site": r.site,
        "reviewer": {"name": r.reviewer_name, "country": r.reviewer_country},
        "rating": r.rating, "title": r.title, "content": r.content,
        "language": r.language,
        "review_date": _iso(r.review_date),
        "purchase_date": _iso(r.purchase_date),
        "reply": {"content": r.reply_content, "date": _iso(r.reply_date)},
        "is_verified": r.is_verified, "topics": r.review_topics or [],
        "sentiment": r.sentiment, "sku": r.sku,
    } for r in rows]
    return _envelope(data, total, page, page_size)


@router.get("/site/{site}")
def v1_site_snapshot(site: str, db: Session = Depends(get_db)):
    """单站点完整快照 —— Agent 一次调用拿全。"""
    s = db.query(Site).filter(Site.site == site).first()
    if not s:
        raise HTTPException(404, "站点不存在")
    sku = db.query(Product).filter(Product.site == site).count()
    promo = db.query(Promotion).filter(Promotion.site == site).count()
    cats = db.query(Category).filter(Category.site == site).count()
    return {
        "object": "site_snapshot",
        "site": {"site": s.site, "brand": s.brand, "country": s.country,
                 "url": s.url, "platform": s.platform,
                 "last_crawled": _iso(s.last_crawled)},
        "metrics": {"sku_count": sku, "promotion_count": promo,
                    "category_count": cats},
        "links": {
            "products": f"/api/v1/products?site={site}",
            "promotions": f"/api/v1/promotions?site={site}",
            "trends": f"/api/v1/trends?site={site}",
        },
    }
