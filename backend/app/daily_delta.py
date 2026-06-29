"""Daily Delta 增量更新子系统 · 2026-05-24

遨森客户每日增量需求：不重抓 4M SKU，每天只抓变化的部分。

5 类增量 job（每天凌晨 2:00 UTC 触发）：
1. sitemap_delta_job  · sitemap lastmod 增量找新/改 URL
2. top_sku_refresh    · top 1000 高价值 SKU 重抓（价格/库存）
3. promo_scan         · 首页/促销页扫新促销
4. review_delta       · trustpilot/reviews.io 评论增量
5. aggregate_trends   · 当日 Trends 表 daily snapshot + LLM 总结

Each job 写入 Trends 表对应 delta 字段，前端 Dashboard / Daily Email 直接读。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import desc, exists, func, or_

from .db import SessionLocal
from .models import (
    CrawlJob, Product, PriceHistory, Promotion, Review, Site, Trend,
    WorkspaceSite,
)

logger = logging.getLogger("smart-crawler.daily-delta")


# ============= 1. Sitemap lastmod 增量 =============

def sitemap_delta_job() -> dict:
    """扫所有 site 的 sitemap.xml，对比 lastmod 找新/改的 URL，入队抓取。

    返回 {site: {new_count, changed_count, fetched}}。
    """
    from curl_cffi import requests as creq
    from .runner import enqueue

    yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
    result: dict[str, dict] = {}

    db = SessionLocal()
    try:
        sites = db.query(Site).filter(Site.platform == "vidaxl").all()
        # MVP：先做 vidaxl 12 站（最大缺口）。后续扩 woltu/vonhaus 等
    finally:
        db.close()

    sess = creq.Session(impersonate="chrome", timeout=30)
    for site in sites:
        try:
            idx_url = site.url.rstrip("/") + "/sitemap_index.xml"
            resp = sess.get(idx_url, timeout=30)
            if resp.status_code != 200:
                result[site.site] = {"error": f"sitemap {resp.status_code}"}
                continue

            # 提 sub-sitemap + lastmod 对照
            sitemap_pairs = re.findall(
                r"<sitemap>\s*<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?",
                resp.text)
            fresh_maps = [
                loc for loc, lastmod in sitemap_pairs
                if lastmod and lastmod >= yesterday and "custom-product" in loc
            ]
            if not fresh_maps:
                result[site.site] = {"new_count": 0, "changed_count": 0, "fetched": "no fresh maps"}
                continue

            # 入队该 site（worker 抓时会增量处理）
            job_id = enqueue(site.site, trigger="daily_delta")
            if job_id is None:
                result[site.site] = {"skipped": "tracking_paused"}
                continue
            result[site.site] = {
                "fresh_sitemaps": len(fresh_maps),
                "fetched": job_id,
            }
            logger.info(f"sitemap_delta {site.site}: {len(fresh_maps)} fresh sub-sitemaps → job #{job_id}")
        except Exception as exc:
            result[site.site] = {"error": str(exc)[:80]}

    return result


# ============= 2. Top SKU 重抓 =============

def top_sku_refresh_job(top_n: int = 1000) -> dict:
    """从 DB 找 top N 高价值 SKU（30day_sales > 50 或 review_count > 100），
    生成 site-level enqueue 信号 → worker 重抓。

    MVP：先做 site 维度 enqueue，未来扩成 per-SKU url enqueue。
    """
    from .runner import enqueue

    db = SessionLocal()
    try:
        # 统计每个 site 的 high-value SKU 数
        rows = (
            db.query(
                Product.site,
                func.count(Product.id).label("hi_value"),
            )
            .join(Site, Site.site == Product.site)
            .filter(
                or_(Site.track_status.is_(None), Site.track_status == "tracking"),
                exists().where(
                    WorkspaceSite.site == Product.site
                ).where(
                    WorkspaceSite.enabled.is_(True),
                    WorkspaceSite.hidden.is_(False),
                ),
                (Product.thirty_day_sales >= 50) | (Product.review_count >= 100)
            )
            .group_by(Product.site)
            .order_by(desc("hi_value"))
            .limit(20)
            .all()
        )
        result: dict[str, dict] = {}
        for site_name, count in rows:
            try:
                job_id = enqueue(site_name, trigger="daily_refresh")
                if job_id is None:
                    result[site_name] = {
                        "hi_value_skus": count,
                        "skipped": "tracking_paused",
                    }
                    continue
                result[site_name] = {"hi_value_skus": count, "job_id": job_id}
            except Exception as exc:
                result[site_name] = {"error": str(exc)[:80]}
        return result
    finally:
        db.close()


# ============= 3. 促销扫描 =============

def promo_scan_job() -> dict:
    """扫所有 site 的首页/促销页找新促销。

    MVP：触发 site 重抓时已会捕获 promotion，这里只统计 yesterday 新增。
    后续可加专门的 promo 扫描 worker。
    """
    db = SessionLocal()
    try:
        yesterday = date.today() - timedelta(days=1)
        rows = (
            db.query(
                Promotion.site,
                func.count(Promotion.id).label("new_promo"),
            )
            .filter(Promotion.detected_time >= yesterday)
            .group_by(Promotion.site)
            .all()
        )
        return {site: {"new_promo_count": count} for site, count in rows}
    finally:
        db.close()


# ============= 4. 评论增量 =============

def review_delta_job() -> dict:
    """每个商家 trustpilot/reviews.io/google_maps 评论增量。

    按 review_date desc 抓到上次最新止。
    MVP：触发现有评论 crawler；后续优化按 last_date 增量。
    """
    db = SessionLocal()
    try:
        yesterday = date.today() - timedelta(days=1)
        rows = (
            db.query(
                Review.platform,
                Review.site,
                func.count(Review.id).label("new_reviews"),
                func.avg(Review.sentiment_score).label("avg_sent"),
            )
            .filter(Review.collected_time >= yesterday)
            .group_by(Review.platform, Review.site)
            .all()
        )
        return {
            f"{platform}/{site}": {
                "new_reviews": count,
                "avg_sentiment": float(avg_sent) if avg_sent else None,
            }
            for platform, site, count, avg_sent in rows
        }
    finally:
        db.close()


# ============= 5. Trends 聚合 =============

def aggregate_trends_job() -> dict:
    """每天凌晨为每个 site 写一行 Trends 快照（含 5 个 delta 字段）。

    用昨天的 ProductHistory / PriceHistory / Promotion / Review 数据填充。
    LLM 生成 delta_summary 一句话。
    """
    db = SessionLocal()
    today = date.today()
    yesterday = today - timedelta(days=1)
    result = {}

    try:
        sites = db.query(Site).all()
        for site in sites:
            # 价格变化数：昨天 PriceHistory 行数（每行=一次价格变化）
            price_changes = (
                db.query(func.count(PriceHistory.id))
                .filter(
                    PriceHistory.site == site.site,
                    PriceHistory.date == yesterday,
                )
                .scalar() or 0
            )

            # 新促销
            new_promos = (
                db.query(func.count(Promotion.id))
                .filter(
                    Promotion.site == site.site,
                    Promotion.detected_time >= yesterday,
                )
                .scalar() or 0
            )

            # 新评论
            new_reviews_count = (
                db.query(func.count(Review.id))
                .filter(
                    Review.site == site.site,
                    Review.collected_time >= yesterday,
                )
                .scalar() or 0
            )
            avg_sent = (
                db.query(func.avg(Review.sentiment_score))
                .filter(
                    Review.site == site.site,
                    Review.collected_time >= yesterday,
                )
                .scalar()
            )

            # 新 SKU
            new_skus = (
                db.query(func.count(Product.id))
                .filter(
                    Product.site == site.site,
                    Product.created_time >= yesterday,
                )
                .scalar() or 0
            )

            # 总 SKU
            sku_total = (
                db.query(func.count(Product.id))
                .filter(Product.site == site.site)
                .scalar() or 0
            )

            # 写 Trend 行（UPSERT）
            existing = (
                db.query(Trend)
                .filter(Trend.site == site.site, Trend.date == today)
                .first()
            )
            if existing:
                trend = existing
            else:
                trend = Trend(site=site.site, date=today)
                db.add(trend)
            trend.sku_count = sku_total
            trend.new_product_count = new_skus
            trend.price_change_count = price_changes
            trend.new_promo_count = new_promos
            trend.new_review_count = new_reviews_count
            trend.avg_sentiment = float(avg_sent) if avg_sent else None

            # LLM 一句话总结（可选，省成本）
            if (new_skus + price_changes + new_promos + new_reviews_count) > 0:
                summary_parts = []
                if new_skus:
                    summary_parts.append(f"新增 {new_skus} 个 SKU")
                if price_changes:
                    summary_parts.append(f"{price_changes} 个价格变化")
                if new_promos:
                    summary_parts.append(f"{new_promos} 个新促销")
                if new_reviews_count:
                    summary_parts.append(f"{new_reviews_count} 条新评论")
                trend.delta_summary = "、".join(summary_parts)
            else:
                trend.delta_summary = "无变化"

            result[site.site] = {
                "sku_total": sku_total,
                "new_skus": new_skus,
                "price_changes": price_changes,
                "new_promos": new_promos,
                "new_reviews": new_reviews_count,
                "summary": trend.delta_summary,
            }

        db.commit()
        return result
    finally:
        db.close()


# ============= 主入口（被 scheduler 调用）=============

def run_all_daily_delta() -> dict:
    """凌晨 2:00 触发 · 5 个 delta job 依次跑。"""
    logger.info("=== Daily Delta 开始 ===")
    out = {}
    try:
        out["sitemap_delta"] = sitemap_delta_job()
    except Exception as exc:
        out["sitemap_delta"] = {"error": str(exc)[:200]}

    try:
        out["top_sku_refresh"] = top_sku_refresh_job(top_n=1000)
    except Exception as exc:
        out["top_sku_refresh"] = {"error": str(exc)[:200]}

    try:
        out["promo_scan"] = promo_scan_job()
    except Exception as exc:
        out["promo_scan"] = {"error": str(exc)[:200]}

    try:
        out["review_delta"] = review_delta_job()
    except Exception as exc:
        out["review_delta"] = {"error": str(exc)[:200]}

    try:
        out["aggregate_trends"] = aggregate_trends_job()
    except Exception as exc:
        out["aggregate_trends"] = {"error": str(exc)[:200]}

    logger.info("=== Daily Delta 完成 ===")
    return out
