"""数据清洗与质量管线 —— 规格 §8.3（C-020 ~ C-025）。

采集器产出「原始 product dict」，此模块负责清洗 / 标准化 / 入库 upsert /
价格曲线记录 / 变更检测。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from .models import PriceHistory, Product

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PRICE_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def clean_text(value):
    """C-020：去 HTML 标签、压缩空白。"""
    if value is None:
        return None
    text = _TAG_RE.sub(" ", str(value))
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def to_price(value):
    """C-022：价格统一为数值型（去货币符号 / 千分位）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    m = _PRICE_RE.search(str(value).replace(",", ""))
    return round(float(m.group()), 2) if m else None


def parse_dt(value):
    """C-022：日期统一为 datetime（ISO 8601 容错解析）。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


# 必填字段 —— 缺失则标记为异常（C-023）
REQUIRED = ("sku", "title", "sale_price", "product_url", "site")


def normalize(raw: dict) -> dict:
    """把采集器产出的原始 dict 清洗成可入库的标准 dict。"""
    p = dict(raw)
    p["title"] = clean_text(p.get("title"))
    p["description"] = clean_text(p.get("description"))
    p["sale_price"] = to_price(p.get("sale_price"))
    p["original_price"] = to_price(p.get("original_price"))
    p["published_at"] = parse_dt(p.get("published_at"))
    if p.get("original_price") is None:
        p["original_price"] = p.get("sale_price")
    return p


def is_valid(p: dict) -> bool:
    """C-023：必填字段齐全才入库。"""
    return all(p.get(k) not in (None, "") for k in REQUIRED)


def upsert_products(session: Session, site: str, items: list[dict]) -> dict:
    """入库 + 去重（C-021）+ 变更检测（C-024）+ 价格曲线（F1-011）。

    返回统计：{total, inserted, updated, skipped, new, changed}。
    """
    today = date.today()
    now = datetime.utcnow()
    stats = {"total": len(items), "inserted": 0, "updated": 0,
             "skipped": 0, "new": 0, "changed": 0}

    existing = {p.sku: p for p in session.query(Product).filter(Product.site == site)}
    seen: set[str] = set()

    for raw in items:
        p = normalize(raw)
        if not is_valid(p):
            stats["skipped"] += 1
            continue
        sku = p["sku"]
        if sku in seen:                       # C-021：同周期去重
            stats["skipped"] += 1
            continue
        seen.add(sku)

        row = existing.get(sku)
        if row is None:                       # 新 SKU —— F1-012 首次出现规则
            p.setdefault("is_new", True)
            obj = Product(created_time=now, updated_time=now, **_product_kwargs(p))
            session.add(obj)
            stats["inserted"] += 1
            stats["new"] += 1
        else:
            if _has_changed(row, p):          # C-024：变更检测
                stats["changed"] += 1
            for k, v in _product_kwargs(p).items():
                if k in ("created_time",):
                    continue
                setattr(row, k, v)
            row.updated_time = now
            stats["updated"] += 1

        session.add(PriceHistory(
            site=site, sku=sku, date=today,
            sale_price=p.get("sale_price"),
            original_price=p.get("original_price"),
            review_count=p.get("review_count"),
        ))
    return stats


_PRODUCT_COLS = {c.name for c in Product.__table__.columns} - {"id"}


def _product_kwargs(p: dict) -> dict:
    return {k: v for k, v in p.items() if k in _PRODUCT_COLS}


def _has_changed(row: Product, p: dict) -> bool:
    for field in ("sale_price", "original_price", "status", "review_count"):
        if p.get(field) is not None and getattr(row, field) != p.get(field):
            return True
    return False
