"""REST API —— 满足规格 §10 数据接口需求（API-001 ~ API-008）。"""
from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..analytics import recompute
from ..db import get_db
from ..export import export_workbook
from ..models import (Category, CrawlJob, PriceHistory, Product, Promotion,
                      Site, Trend)
from ..runner import run_brand, run_site

router = APIRouter(prefix="/api")


# ---------- 序列化 ----------
def site_dict(s: Site) -> dict:
    return {"site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform, "proxy_tier": s.proxy_tier,
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None}


def product_dict(p: Product) -> dict:
    return {
        "id": p.id, "sku": p.sku, "spu": p.spu, "title": p.title,
        "image": (p.image_urls or [None])[0], "image_urls": p.image_urls,
        "category_path": p.category_path, "sale_price": p.sale_price,
        "original_price": p.original_price, "currency": p.currency,
        "attributes": p.attributes, "ratings": p.ratings,
        "review_count": p.review_count, "thirty_day_sales": p.thirty_day_sales,
        "thirty_day_revenue": p.thirty_day_revenue, "status": p.status,
        "inventory": p.inventory, "has_video": p.has_video,
        "has_free_shipping": p.has_free_shipping, "label": p.label,
        "tags": p.tags, "product_url": p.product_url,
        "product_type": p.product_type, "is_new": p.is_new,
        "is_bestseller": p.is_bestseller,
        "created_time": p.created_time.isoformat() if p.created_time else None,
        "updated_time": p.updated_time.isoformat() if p.updated_time else None,
        "site": p.site, "brand": p.brand,
    }


# ---------- 站点概览（R-001 / R-002 / §14.2）----------
@router.get("/sites")
def list_sites(db: Session = Depends(get_db)):
    out = []
    for s in db.query(Site).all():
        d = site_dict(s)
        d["sku_count"] = db.query(Product).filter(Product.site == s.site).count()
        d["spu_count"] = (db.query(Product.spu)
                          .filter(Product.site == s.site).distinct().count())
        d["category_count"] = db.query(Category).filter(
            Category.site == s.site).count()
        d["promotion_count"] = db.query(Promotion).filter(
            Promotion.site == s.site).count()
        out.append(d)
    return out


@router.get("/sites/{site}/overview")
def site_overview(site: str, db: Session = Depends(get_db)):
    """6 个指标卡 + 趋势序列。"""
    if not db.query(Site).filter(Site.site == site).first():
        raise HTTPException(404, "站点不存在")
    sku_count = db.query(Product).filter(Product.site == site).count()
    new_count = db.query(Product).filter(
        Product.site == site, Product.is_new.is_(True)).count()
    sales, revenue = db.query(
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
    ).filter(Product.site == site).first()
    trends = [{"date": t.date.isoformat(), "sku_count": t.sku_count,
               "new_product_count": t.new_product_count,
               "estimated_sales": t.estimated_sales,
               "estimated_revenue": t.estimated_revenue}
              for t in db.query(Trend).filter(Trend.site == site)
              .order_by(Trend.date).all()]
    return {
        "cards": {
            "sku_count": sku_count, "new_product_count": new_count,
            "thirty_day_sales": int(sales or 0),
            "thirty_day_revenue": round(revenue or 0, 2),
            "traffic": None, "conversion_rate": None,
        },
        "trends": trends,
    }


# ---------- 商品分析（R-010 / §14.3 / API-002）----------
@router.get("/products")
def list_products(
    site: str | None = None,
    tab: str = Query("all", pattern="^(all|bestseller|new)$"),
    search: str | None = None,
    status: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    q = db.query(Product)
    if site:
        q = q.filter(Product.site == site)
    if tab == "bestseller":
        q = q.filter(Product.is_bestseller.is_(True))
    elif tab == "new":
        q = q.filter(Product.is_new.is_(True))
    if search:
        like = f"%{search}%"
        q = q.filter((Product.title.ilike(like)) | (Product.sku.ilike(like)))
    if status:
        q = q.filter(Product.status == status)
    if min_price is not None:
        q = q.filter(Product.sale_price >= min_price)
    if max_price is not None:
        q = q.filter(Product.sale_price <= max_price)
    total = q.count()
    rows = (q.order_by(Product.id)
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "items": [product_dict(p) for p in rows]}


@router.get("/products/{pid}")
def get_product(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    return product_dict(p)


@router.get("/products/{pid}/price-history")
def price_history(pid: int, db: Session = Depends(get_db)):
    """单 SKU 价格曲线 —— R-012。"""
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    rows = (db.query(PriceHistory)
            .filter(PriceHistory.site == p.site, PriceHistory.sku == p.sku)
            .order_by(PriceHistory.date).all())
    return [{"date": r.date.isoformat(), "sale_price": r.sale_price,
             "original_price": r.original_price,
             "review_count": r.review_count} for r in rows]


# ---------- 促销分析（§14.4 / API-005）----------
@router.get("/promotions")
def list_promotions(site: str | None = None, page: int = 1,
                    page_size: int = 50, db: Session = Depends(get_db)):
    q = db.query(Promotion)
    if site:
        q = q.filter(Promotion.site == site)
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [{
        "id": r.id, "sku": r.sku, "site": r.site,
        "promotion_type": r.promotion_type, "promotion_name": r.promotion_name,
        "original_price": r.original_price, "promotion_price": r.promotion_price,
        "discount_percent": r.discount_percent, "threshold": r.threshold,
        "product_title": r.product_title, "product_image": r.product_image,
        "detected_time": r.detected_time.isoformat() if r.detected_time else None,
    } for r in rows]}


# ---------- 趋势 / 分类（API-004）----------
@router.get("/trends")
def list_trends(site: str, db: Session = Depends(get_db)):
    return [{"date": t.date.isoformat(), "sku_count": t.sku_count,
             "new_product_count": t.new_product_count,
             "estimated_sales": t.estimated_sales,
             "estimated_revenue": t.estimated_revenue}
            for t in db.query(Trend).filter(Trend.site == site)
            .order_by(Trend.date).all()]


@router.get("/categories")
def list_categories(site: str, db: Session = Depends(get_db)):
    rows = db.query(Category).filter(Category.site == site).all()
    return [{"category_id": c.category_id, "name": c.category_name,
             "url": c.category_url, "level": c.level,
             "product_count": c.product_count} for c in rows]


# ---------- 采集任务看板（C-030 / C-003）----------
@router.get("/jobs")
def list_jobs(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit).all()
    return [{
        "id": j.id, "site": j.site, "status": j.status,
        "products_count": j.products_count, "new_count": j.new_count,
        "promotion_count": j.promotion_count, "success_rate": j.success_rate,
        "duration_sec": round(j.duration_sec, 1) if j.duration_sec else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "error": j.error,
    } for j in rows]


def _run_and_recompute(site: str | None, brand: str | None):
    results = run_brand(brand) if brand else [run_site(site)]
    for r in results:
        if r["status"] == "success":
            recompute(r["site"])


@router.post("/jobs/trigger")
def trigger(background: BackgroundTasks, site: str | None = None,
            brand: str | None = None, db: Session = Depends(get_db)):
    """手动触发采集 —— C-003。后台异步执行。"""
    if not site and not brand:
        raise HTTPException(400, "需指定 site 或 brand")
    if site and not db.query(Site).filter(Site.site == site).first():
        raise HTTPException(404, "站点不存在")
    background.add_task(_run_and_recompute, site, brand)
    return {"status": "queued", "site": site, "brand": brand,
            "queued_at": datetime.utcnow().isoformat()}


@router.get("/scheduler")
def scheduler_jobs():
    """定时采集任务列表 —— C-001。"""
    try:
        from ..scheduler import list_scheduled_jobs
        return list_scheduled_jobs()
    except Exception:
        return []


# ---------- Excel 导出（API-006）----------
@router.get("/export/products")
def export_products(site: str | None = None, db: Session = Depends(get_db)):
    data = export_workbook(db, site)
    fname = f"smart-crawler_{site or 'all'}_{datetime.now():%Y%m%d}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
