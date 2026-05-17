"""采集编排 —— 串起：建任务 → 跑采集器 → 清洗入库 → 促销识别 → 收尾。"""
from __future__ import annotations

import traceback
from datetime import datetime

from .crawlers.registry import get_crawler
from .db import session_scope
from .models import Category, CrawlJob, Product, Promotion, Site
from .pipeline import upsert_products


def run_site(site_name: str) -> dict:
    """采集单个站点。返回任务统计 dict。"""
    with session_scope() as s:
        site = s.query(Site).filter(Site.site == site_name).first()
        if site is None:
            raise ValueError(f"站点不存在: {site_name}")
        job = CrawlJob(site=site_name, status="running", started_at=datetime.utcnow())
        s.add(job)
        s.flush()
        job_id = job.id
        crawler = get_crawler(site)

    started = datetime.utcnow()
    try:
        result = crawler.crawl()
    except Exception as exc:                       # 采集失败 —— C-005 告警
        with session_scope() as s:
            job = s.get(CrawlJob, job_id)
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.duration_sec = (datetime.utcnow() - started).total_seconds()
            job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
        return {"job_id": job_id, "site": site_name, "status": "failed", "error": str(exc)}

    with session_scope() as s:
        stats = upsert_products(s, site_name, result.products)
        _save_categories(s, site_name, result.categories)
        s.flush()                                  # autoflush=False，促销识别前需手动 flush
        promo_count = _detect_promotions(s, site_name)

        job = s.get(CrawlJob, job_id)
        job.status = "success"
        job.finished_at = datetime.utcnow()
        job.duration_sec = (datetime.utcnow() - started).total_seconds()
        job.products_count = stats["inserted"] + stats["updated"]
        job.new_count = stats["new"]
        job.promotion_count = promo_count
        total = stats["total"] or 1
        job.success_rate = round(
            (stats["inserted"] + stats["updated"]) / total * 100, 1)

        site = s.query(Site).filter(Site.site == site_name).first()
        site.last_crawled = datetime.utcnow()

    return {
        "job_id": job_id, "site": site_name, "status": "success",
        "products": job.products_count, "new": stats["new"],
        "promotions": promo_count, "notes": result.notes,
        "duration_sec": round(job.duration_sec, 1),
    }


def run_brand(brand: str) -> list[dict]:
    """采集某品牌全部站点。"""
    with session_scope() as s:
        names = [r.site for r in s.query(Site).filter(Site.brand == brand)]
    return [run_site(n) for n in names]


def _save_categories(s, site_name: str, cats: list[dict]) -> None:
    if not cats:
        return
    s.query(Category).filter(Category.site == site_name).delete()
    for c in cats:
        s.add(Category(**c))


def _detect_promotions(s, site_name: str) -> int:
    """促销识别 —— F1-020：原价 > 售价即判定为价格促销。"""
    s.query(Promotion).filter(Promotion.site == site_name).delete()
    rows = (s.query(Product)
            .filter(Product.site == site_name)
            .filter(Product.original_price > Product.sale_price)
            .all())
    for p in rows:
        discount = None
        if p.original_price:
            discount = round((p.original_price - p.sale_price) / p.original_price * 100)
        img = p.image_urls[0] if p.image_urls else None
        s.add(Promotion(
            sku=p.sku, site=site_name, promotion_type="price_promotion",
            promotion_name=None, original_price=p.original_price,
            promotion_price=p.sale_price, discount_percent=discount,
            product_title=p.title, product_image=img,
        ))
    return len(rows)
