"""Excel 导出 —— 完全对标 03-样本数据 的三份报表，并额外提供更多字段。

对标的样本（甲方原始交付物）：
  product_analysis_report.xlsx  — 20 列
  sales_promotion_report.xlsx   — 13 列
  trend_report.xlsx             — 8 列
本模块输出的工作簿在「完全复刻这三张表的列名与顺序」之外，额外提供
「商品全字段」「分类树」「站点概览」三张扩展表 —— 信息只多不少。
"""
from __future__ import annotations

import io

import pandas as pd
from sqlalchemy.orm import Session

from .models import (Category, PriceHistory, Product, Promotion,
                      Review, Site, Trend)

# ---- 对标 product_analysis_report.xlsx 的 20 列（列名/顺序与样本完全一致）----
PRODUCT_SAMPLE_COLS = [
    "NO.", "Sku", "Image", "Title", "label", "VariantId", "Attributes",
    "Sale Price", "Price", "30-Days Sales", "30-Days Revenue", "Ratings",
    "Reviews", "Status", "Category", "Inventory", "Video", "Free shipping",
    "Created Time", "Update Time",
]
# ---- 对标 sales_promotion_report.xlsx 的 13 列 ----
PROMO_SAMPLE_COLS = [
    "NO.", "SKU", "Update Time", "Product Title", "Product Image", "Type",
    "Name", "Discount", "Orignal-Price", "Post-Price", "Threshold",
    "Start Time", "End Time",
]
# ---- 对标 trend_report.xlsx 的 8 列 ----
TREND_SAMPLE_COLS = [
    "NO.", "Date", "Sku Count", "New Product Count", "Sales", "Revenue",
    "Traffic", "Conversion Rate",
]
# ---- 扩展表：规格 §4.1.2 全部 32 个 SKU 字段 ----
PRODUCT_FULL_COLS = [
    "NO.", "site", "brand", "sku", "spu", "variant_id", "title", "description",
    "category_path", "product_type", "attributes", "tags", "label",
    "sale_price", "original_price", "currency", "ratings", "review_count",
    "thirty_day_sales", "thirty_day_revenue", "status", "inventory",
    "has_video", "has_free_shipping", "mpn", "gtin", "weight", "shipping_time",
    "return_policy_days", "image_count", "image_urls", "product_url",
    "is_new", "is_bestseller", "published_at", "created_time", "updated_time",
]

_STATUS = {"on_sale": "on sale", "out_of_stock": "out of stock",
           "discontinued": "discontinued"}


def _yn(v) -> str:
    return "YES" if v else "NO"


def _attrs(a) -> str:
    if not a:
        return ""
    return " ".join(f"{k}:{v}" for k, v in a.items())


def _list(v) -> str:
    return ", ".join(str(x) for x in v) if v else ""


def _dt(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M:%S") if v else ""


def _apply_cat_filter(q, model_class, categories: list[str] | None):
    """对 query 加品类过滤（OR 模糊匹配 category_path 任一关键词）。"""
    if not categories:
        return q
    from sqlalchemy import or_
    return q.filter(or_(*[model_class.category_path.ilike(f"%{c}%")
                          for c in categories]))


def _apply_site_filter(q, model_class, site):
    """对 query 加站点过滤。site 可以是 str / list[str] / None。"""
    if site is None:
        return q
    if isinstance(site, str):
        return q.filter(model_class.site == site)
    if isinstance(site, (list, tuple)) and site:
        if len(site) == 1:
            return q.filter(model_class.site == site[0])
        return q.filter(model_class.site.in_(site))
    return q


# ---------- 对标样本：商品分析报表 ----------
def products_sample_df_from_rows(products) -> pd.DataFrame:
    rows = []
    for i, p in enumerate(products, start=1):
        rows.append({
            "NO.": i, "Sku": p.sku, "Image": (p.image_urls or [""])[0],
            "Title": p.title, "label": p.label or "", "VariantId": p.variant_id,
            "Attributes": _attrs(p.attributes), "Sale Price": p.sale_price,
            "Price": p.original_price, "30-Days Sales": p.thirty_day_sales or 0,
            "30-Days Revenue": p.thirty_day_revenue or 0.0,
            "Ratings": p.ratings or 0.0, "Reviews": p.review_count or 0,
            "Status": _STATUS.get(p.status, p.status), "Category": p.category_path,
            "Inventory": p.inventory, "Video": _yn(p.has_video),
            "Free shipping": _yn(p.has_free_shipping),
            "Created Time": _dt(p.created_time), "Update Time": _dt(p.updated_time),
        })
    return pd.DataFrame(rows, columns=PRODUCT_SAMPLE_COLS)


def products_sample_df(session: Session, site=None,
                       categories: list[str] | None = None) -> pd.DataFrame:
    q = _apply_site_filter(session.query(Product), Product, site)
    q = _apply_cat_filter(q, Product, categories)
    return products_sample_df_from_rows(q.order_by(Product.id).all())


# ---------- 对标样本：销售促销报表 ----------
def promotions_sample_df_from_rows(promotions) -> pd.DataFrame:
    rows = []
    for i, p in enumerate(promotions, start=1):
        rows.append({
            "NO.": i, "SKU": p.sku, "Update Time": _dt(p.detected_time),
            "Product Title": p.product_title, "Product Image": p.product_image,
            "Type": p.promotion_type, "Name": p.promotion_name or p.promotion_type,
            "Discount": p.discount_percent, "Orignal-Price": p.original_price,
            "Post-Price": p.promotion_price, "Threshold": p.threshold or "/",
            "Start Time": _dt(p.start_time), "End Time": _dt(p.end_time),
        })
    return pd.DataFrame(rows, columns=PROMO_SAMPLE_COLS)


def promotions_sample_df(session: Session, site=None,
                         categories: list[str] | None = None) -> pd.DataFrame:
    q = _apply_site_filter(session.query(Promotion), Promotion, site)
    if categories:
        # Promotion 没 category_path，通过 Product join 过滤
        from sqlalchemy import or_
        sku_q = _apply_site_filter(session.query(Product.sku), Product, site)
        skus = [r[0] for r in sku_q.filter(
            or_(*[Product.category_path.ilike(f"%{c}%") for c in categories])).all()]
        if skus:
            q = q.filter(Promotion.sku.in_(skus))
        else:
            q = q.filter(Promotion.id == -1)  # empty result
    return promotions_sample_df_from_rows(q.all())


# ---------- 对标样本：趋势报表 ----------
def trends_sample_df(session: Session, site=None) -> pd.DataFrame:
    q = _apply_site_filter(session.query(Trend), Trend, site)
    rows = []
    for i, t in enumerate(q.order_by(Trend.date).all(), start=1):
        rows.append({
            "NO.": i, "Date": t.date.isoformat() if t.date else "",
            "Sku Count": t.sku_count, "New Product Count": t.new_product_count,
            "Sales": t.estimated_sales, "Revenue": t.estimated_revenue,
            "Traffic": t.traffic if t.traffic is not None else "/",
            "Conversion Rate": t.conversion_rate
            if t.conversion_rate is not None else "/",
        })
    return pd.DataFrame(rows, columns=TREND_SAMPLE_COLS)


# ---------- 扩展表：商品全字段（32 字段，信息只多不少）----------
def products_full_df(session: Session, site=None,
                     categories: list[str] | None = None) -> pd.DataFrame:
    q = _apply_site_filter(session.query(Product), Product, site)
    q = _apply_cat_filter(q, Product, categories)
    rows = []
    for i, p in enumerate(q.order_by(Product.id).all(), start=1):
        rows.append({
            "NO.": i, "site": p.site, "brand": p.brand, "sku": p.sku,
            "spu": p.spu, "variant_id": p.variant_id, "title": p.title,
            "description": (p.description or "")[:500], "category_path": p.category_path,
            "product_type": p.product_type, "attributes": _attrs(p.attributes),
            "tags": _list(p.tags), "label": p.label, "sale_price": p.sale_price,
            "original_price": p.original_price, "currency": p.currency,
            "ratings": p.ratings, "review_count": p.review_count,
            "thirty_day_sales": p.thirty_day_sales,
            "thirty_day_revenue": p.thirty_day_revenue, "status": p.status,
            "inventory": p.inventory, "has_video": _yn(p.has_video),
            "has_free_shipping": _yn(p.has_free_shipping), "mpn": p.mpn,
            "gtin": p.gtin, "weight": p.weight, "shipping_time": p.shipping_time,
            "return_policy_days": p.return_policy_days,
            "image_count": len(p.image_urls or []), "image_urls": _list(p.image_urls),
            "product_url": p.product_url, "is_new": _yn(p.is_new),
            "is_bestseller": _yn(p.is_bestseller), "published_at": _dt(p.published_at),
            "created_time": _dt(p.created_time), "updated_time": _dt(p.updated_time),
        })
    return pd.DataFrame(rows, columns=PRODUCT_FULL_COLS)


# ---------- 扩展表：分类树 ----------
def categories_df(session: Session, site=None) -> pd.DataFrame:
    q = _apply_site_filter(session.query(Category), Category, site)
    rows = []
    for i, c in enumerate(q.all(), start=1):
        rows.append({
            "NO.": i, "site": c.site, "category_id": c.category_id,
            "category_name": c.category_name, "category_url": c.category_url,
            "parent_id": c.parent_id, "level": c.level,
            "product_count": c.product_count, "collected_time": _dt(c.collected_time),
        })
    return pd.DataFrame(rows, columns=["NO.", "site", "category_id",
                        "category_name", "category_url", "parent_id", "level",
                        "product_count", "collected_time"])


# ---------- 扩展表：站点概览 ----------
def sites_overview_df(session: Session, site=None) -> pd.DataFrame:
    rows = []
    q = _apply_site_filter(session.query(Site), Site, site)
    for i, s in enumerate(q.all(), start=1):
        sku = session.query(Product).filter(Product.site == s.site).count()
        spu = (session.query(Product.spu)
               .filter(Product.site == s.site).distinct().count())
        cats = session.query(Category).filter(Category.site == s.site).count()
        promo = session.query(Promotion).filter(Promotion.site == s.site).count()
        rows.append({
            "NO.": i, "site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform, "proxy_tier": s.proxy_tier,
            "SKU数": sku, "SPU数": spu, "分类数": cats, "促销数": promo,
            "最后采集": _dt(s.last_crawled),
        })
    return pd.DataFrame(rows, columns=["NO.", "site", "brand", "country", "url",
                        "platform", "proxy_tier", "SKU数", "SPU数", "分类数",
                        "促销数", "最后采集"])


def export_workbook(session: Session, site=None,
                    categories: list[str] | None = None,
                    include_price_history: bool = False,
                    include_voc: bool = False,
                    include_images: bool = True,
                    split_by_category: bool = False) -> bytes:
    """导出 Excel。
    基础 6 Sheet + 可选「价格曲线」「评论 VOC」 + 按品类拆分子表。

    - include_price_history: True 时新增「价格曲线(90天)」sheet
    - include_voc: True 时新增「评论 VOC」sheet
    - include_images: False 时商品全字段表去掉 image_urls 列（节省体积）
    - split_by_category: True 时每个品类一个独立 sheet
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        products_sample_df(session, site, categories).to_excel(
            w, sheet_name="商品分析", index=False)
        promotions_sample_df(session, site, categories).to_excel(
            w, sheet_name="销售促销", index=False)
        trends_sample_df(session, site).to_excel(
            w, sheet_name="趋势报告", index=False)
        full_df = products_full_df(session, site, categories)
        if not include_images and "image_urls" in full_df.columns:
            full_df = full_df.drop(columns=["image_urls", "image_count"],
                                   errors="ignore")
        full_df.to_excel(w, sheet_name="商品全字段(扩展)", index=False)
        categories_df(session, site).to_excel(
            w, sheet_name="分类树(扩展)", index=False)
        sites_overview_df(session, site).to_excel(
            w, sheet_name="站点概览(扩展)", index=False)

        # 可选 sheets
        if include_price_history:
            price_history_df(session, site, categories).to_excel(
                w, sheet_name="价格曲线(90天)", index=False)
        if include_voc:
            reviews_voc_df(session, site, categories).to_excel(
                w, sheet_name="评论 VOC", index=False)

        # 按品类拆分子表
        if split_by_category:
            cat_groups = _group_skus_by_category(session, site, categories)
            for cat_name, sku_list in list(cat_groups.items())[:30]:
                if not sku_list:
                    continue
                sub_df = full_df[full_df["sku"].isin(sku_list)].copy()
                if sub_df.empty:
                    continue
                sheet = ("品类·" + cat_name)[:31]  # excel sheet name ≤ 31
                sub_df.to_excel(w, sheet_name=sheet, index=False)

    return buf.getvalue()


def export_csv(session: Session, site=None,
               categories: list[str] | None = None) -> bytes:
    """导出 CSV（商品全字段表，UTF-8 BOM 兼容 Excel）。"""
    df = products_full_df(session, site, categories)
    return ("﻿" + df.to_csv(index=False)).encode("utf-8")


def export_json(session: Session, site=None,
                categories: list[str] | None = None) -> bytes:
    """导出 JSON（dict array of full products）。"""
    df = products_full_df(session, site, categories)
    return df.to_json(orient="records", force_ascii=False,
                      indent=2).encode("utf-8")


def export_zip(session: Session, sites: list[str],
               categories: list[str] | None = None,
               **workbook_kwargs) -> bytes:
    """导出 ZIP：每个 site 一个 xlsx 文件。"""
    import zipfile
    buf = io.BytesIO()
    site_list = sites if isinstance(sites, list) else [sites] if sites else []
    if not site_list:
        # 无站点指定时降级为单个 all xlsx
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("smart-crawler_all.xlsx",
                        export_workbook(session, None, categories,
                                        **workbook_kwargs))
        return buf.getvalue()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in site_list:
            data = export_workbook(session, s, categories, **workbook_kwargs)
            zf.writestr(f"smart-crawler_{s}.xlsx", data)
    return buf.getvalue()


# ---------- 新增 sheet builders（Step 2 toggle） ----------
def price_history_df(session: Session, site=None,
                     categories: list[str] | None = None,
                     days: int = 90) -> pd.DataFrame:
    """价格曲线：最近 N 天每 SKU 多行（site/sku/date/sale_price/original_price）。"""
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days)
    skus = _get_filtered_skus(session, site, categories)
    if not skus:
        return pd.DataFrame(columns=["NO.", "site", "sku", "date",
                                     "sale_price", "original_price"])
    q = session.query(PriceHistory).filter(
        PriceHistory.date >= cutoff,
        PriceHistory.sku.in_(skus))
    if site:
        q = _apply_site_filter(q, PriceHistory, site)
    rows = []
    for i, p in enumerate(q.order_by(PriceHistory.site, PriceHistory.sku,
                                     PriceHistory.date).all(), start=1):
        rows.append({
            "NO.": i, "site": p.site, "sku": p.sku,
            "date": p.date.isoformat() if p.date else "",
            "sale_price": p.sale_price,
            "original_price": p.original_price,
            "discount_pct": (
                round((1 - p.sale_price / p.original_price) * 100, 1)
                if p.sale_price and p.original_price and p.original_price > 0
                else None
            ),
        })
    return pd.DataFrame(rows)


def reviews_voc_df(session: Session, site=None,
                   categories: list[str] | None = None,
                   limit_per_sku: int = 10) -> pd.DataFrame:
    """评论 VOC：每 SKU 最多 N 条评论，含 sentiment / nlp_topics。"""
    skus = _get_filtered_skus(session, site, categories)
    if not skus:
        return pd.DataFrame(columns=["NO.", "platform", "site", "sku",
                                     "rating", "sentiment", "content"])
    # 按 sku 分组取 top N（简单实现：先全取再 client-side limit）
    q = session.query(Review).filter(Review.sku.in_(skus))
    if site:
        q = _apply_site_filter(q, Review, site)
    rows = []
    sku_counts: dict[str, int] = {}
    for r in q.order_by(Review.sku, Review.review_date.desc()).all():
        c = sku_counts.get(r.sku, 0)
        if c >= limit_per_sku:
            continue
        sku_counts[r.sku] = c + 1
        rows.append({
            "platform": r.platform, "site": r.site, "sku": r.sku,
            "rating": r.rating, "sentiment": r.sentiment,
            "sentiment_score": r.sentiment_score,
            "reviewer_name": r.reviewer_name,
            "review_date": (r.review_date.isoformat()
                            if r.review_date else ""),
            "content": (r.content or "")[:500],
            "nlp_topics": _list(r.nlp_topics) if r.nlp_topics else "",
        })
    for i, row in enumerate(rows, start=1):
        row["NO."] = i
    return pd.DataFrame(rows, columns=["NO.", "platform", "site", "sku",
                                       "rating", "sentiment",
                                       "sentiment_score", "reviewer_name",
                                       "review_date", "content",
                                       "nlp_topics"])


def _get_filtered_skus(session: Session, site,
                       categories: list[str] | None) -> list[str]:
    """获取符合过滤的 SKU 列表（用于 PriceHistory / Review join）。"""
    from sqlalchemy import or_
    q = _apply_site_filter(session.query(Product.sku), Product, site)
    if categories:
        q = q.filter(or_(*[Product.category_path.ilike(f"%{c}%")
                           for c in categories]))
    return [r[0] for r in q.all() if r[0]]


def _group_skus_by_category(session: Session, site,
                            categories: list[str] | None) -> dict[str, list[str]]:
    """按 category_path 一级分组 SKU（split_by_category 用）。"""
    q = _apply_site_filter(session.query(Product.sku, Product.category_path),
                           Product, site)
    q = _apply_cat_filter(q, Product, categories)
    groups: dict[str, list[str]] = {}
    for sku, path in q.all():
        if not path or not sku:
            continue
        # 取一级品类（按 / 分割）
        top = path.split("/")[0].strip() or path
        groups.setdefault(top, []).append(sku)
    return groups
