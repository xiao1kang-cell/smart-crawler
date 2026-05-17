"""Excel 导出 —— API-006，列结构对标 03-样本数据 的三份报表。"""
from __future__ import annotations

import io

import pandas as pd
from sqlalchemy.orm import Session

from .models import Product, Promotion, Trend

# 商品报表列（规格 §14.3 产品分析 Tab）
PRODUCT_COLS = [
    ("no", "NO."), ("image", "图片"), ("title", "产品标题"), ("sku", "SKU"),
    ("attributes", "属性"), ("label", "标签"), ("sale_price", "售价"),
    ("original_price", "原价"), ("thirty_day_sales", "30天销量"),
    ("thirty_day_revenue", "30天营收"), ("ratings", "评分"),
    ("review_count", "评论数"), ("status", "状态"), ("category_path", "品类"),
    ("inventory", "库存"), ("has_video", "视频"), ("has_free_shipping", "免运费"),
    ("created_time", "创建时间"), ("updated_time", "更新时间"),
]
PROMOTION_COLS = [
    ("no", "NO."), ("sku", "SKU"), ("detected_time", "更新时间"),
    ("product_title", "产品标题"), ("product_image", "产品图片"),
    ("promotion_type", "类型"), ("promotion_name", "活动名称"),
    ("discount_percent", "折扣"), ("original_price", "原价"),
    ("promotion_price", "活动价"), ("threshold", "门槛"),
    ("start_time", "开始时间"), ("end_time", "结束时间"),
]
TREND_COLS = [
    ("date", "日期"), ("sku_count", "SKU数"), ("new_product_count", "新增产品"),
    ("estimated_sales", "预估销量"), ("estimated_revenue", "预估营收"),
    ("traffic", "流量"), ("conversion_rate", "转化率"),
]


def _fmt(v):
    if isinstance(v, dict):
        return ", ".join(f"{k}:{x}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, bool):
        return "YES" if v else "NO"
    return v


def products_df(session: Session, site: str | None = None) -> pd.DataFrame:
    q = session.query(Product)
    if site:
        q = q.filter(Product.site == site)
    rows = []
    for i, p in enumerate(q.all(), start=1):
        d = {"no": i, "image": (p.image_urls or [None])[0]}
        for attr, _ in PRODUCT_COLS:
            if attr in ("no", "image"):
                continue
            d[attr] = _fmt(getattr(p, attr, None))
        rows.append(d)
    df = pd.DataFrame(rows, columns=[a for a, _ in PRODUCT_COLS])
    return df.rename(columns=dict(PRODUCT_COLS))


def promotions_df(session: Session, site: str | None = None) -> pd.DataFrame:
    q = session.query(Promotion)
    if site:
        q = q.filter(Promotion.site == site)
    rows = []
    for i, p in enumerate(q.all(), start=1):
        d = {"no": i}
        for attr, _ in PROMOTION_COLS:
            if attr == "no":
                continue
            d[attr] = _fmt(getattr(p, attr, None))
        rows.append(d)
    df = pd.DataFrame(rows, columns=[a for a, _ in PROMOTION_COLS])
    return df.rename(columns=dict(PROMOTION_COLS))


def trends_df(session: Session, site: str | None = None) -> pd.DataFrame:
    q = session.query(Trend)
    if site:
        q = q.filter(Trend.site == site)
    rows = []
    for t in q.order_by(Trend.date).all():
        rows.append({attr: _fmt(getattr(t, attr, None)) for attr, _ in TREND_COLS})
    df = pd.DataFrame(rows, columns=[a for a, _ in TREND_COLS])
    return df.rename(columns=dict(TREND_COLS))


def export_workbook(session: Session, site: str | None = None) -> bytes:
    """导出三 Sheet 的 Excel（商品分析 / 销售促销 / 趋势）。"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        products_df(session, site).to_excel(writer, sheet_name="商品分析", index=False)
        promotions_df(session, site).to_excel(writer, sheet_name="销售促销", index=False)
        trends_df(session, site).to_excel(writer, sheet_name="趋势报告", index=False)
    return buf.getvalue()
