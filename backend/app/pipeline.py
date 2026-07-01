"""数据清洗与质量管线 —— 规格 §8.3（C-020 ~ C-025）。

采集器产出「原始 product dict」，此模块负责清洗 / 标准化 / 入库 upsert /
价格曲线记录 / 变更检测。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from .currency import normalize_currency_for_site
from .models import PriceHistory, Product

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PRICE_RE = re.compile(r"[-+]?\d[\d\s\u00a0.,]*")
_INT_RE = re.compile(r"\d[\d\s\u00a0,]*")
_WEAK_TITLE_RE = re.compile(
    r"^(product|item|sku|untitled|detail|details|view product|shop now)$",
    re.I,
)
_PRESERVE_ON_EMPTY = {
    "title",
    "description",
    "image_urls",
    "category_path",
    "sale_price",
    "original_price",
    "currency",
    "variant_id",
    "attributes",
    "ratings",
    "review_count",
    "status",
    "inventory",
    "label",
    "tags",
    "product_url",
    "product_type",
    "mpn",
    "gtin",
    "weight",
    "shipping_time",
    "return_policy_days",
    "published_at",
}
_SALE_PRICE_ALIASES = (
    "sale_price", "price", "current_price", "final_price",
    "discount_price", "promotion_price", "promo_price",
)
_ORIGINAL_PRICE_ALIASES = (
    "original_price", "list_price", "was_price", "regular_price",
    "compare_at_price", "msrp", "rrp", "pre_price",
)
_EXISTING_PRODUCT_LOOKUP_CHUNK_SIZE = 10_000


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
    m = _PRICE_RE.search(str(value))
    if not m:
        return None
    text = m.group().replace("\u00a0", " ").replace(" ", "")
    if not text or not re.search(r"\d", text):
        return None
    sign = "-" if text.startswith("-") else ""
    text = text.lstrip("+-")
    comma = text.rfind(",")
    dot = text.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_sep = "," if comma > dot else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "")
        if decimal_sep == ",":
            text = text.replace(",", ".")
    elif comma >= 0:
        text = _normalize_single_separator_price(text, ",")
    elif dot >= 0:
        text = _normalize_single_separator_price(text, ".")
    try:
        return round(float(f"{sign}{text}"), 2)
    except ValueError:
        return None


def _normalize_single_separator_price(text: str, sep: str) -> str:
    parts = text.split(sep)
    if len(parts) == 2:
        before, after = parts
        if len(after) == 3 and 1 <= len(before) <= 3:
            return before + after
        if 1 <= len(after) <= 2:
            return before + "." + after
    if len(parts) > 2:
        tail = parts[-1]
        if 1 <= len(tail) <= 2:
            return "".join(parts[:-1]) + "." + tail
    return "".join(parts)


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
# 2026-05-24：sale_price 改可选 —— sitemap-only 站（Overstock/Wayfair 反爬严）
# 拿不到价格但 SKU/title/URL 有效，应入库。pipeline 后续可补价格历史。
REQUIRED = ("sku", "title", "product_url", "site")


def normalize(raw: dict) -> dict:
    """把采集器产出的原始 dict 清洗成可入库的标准 dict。"""
    p = dict(raw)
    review_missing = p.get("review_count") in (None, "")
    p["title"] = clean_text(p.get("title"))
    p["description"] = clean_text(p.get("description"))
    p["sale_price"] = _first_price(p, _SALE_PRICE_ALIASES)
    p["original_price"] = _first_price(p, _ORIGINAL_PRICE_ALIASES)
    if p["sale_price"] is None and p["original_price"] is not None:
        p["sale_price"] = p["original_price"]
    p["currency"] = normalize_currency_for_site(p.get("currency"), p.get("site"))
    p["published_at"] = parse_dt(p.get("published_at"))
    p["review_count"] = _review_count_or_zero(p.get("review_count"))
    p["_review_count_missing"] = review_missing
    return p


def _first_price(raw: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = to_price(raw.get(key))
        if value is not None:
            return value
    return None


def _review_count_or_zero(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value)
    m = _INT_RE.search(text)
    if not m:
        return 0
    digits = re.sub(r"[\s\u00a0,]", "", m.group())
    try:
        return max(0, int(digits))
    except ValueError:
        return 0


def is_valid(p: dict) -> bool:
    """C-023：必填字段齐全才入库。"""
    return all(p.get(k) not in (None, "") for k in REQUIRED)


def _chunks(values: list[str], size: int):
    for idx in range(0, len(values), size):
        yield values[idx:idx + size]


def upsert_products(session: Session, site: str, items: list[dict]) -> dict:
    """入库 + 去重（C-021）+ 变更检测（C-024）+ 价格曲线（F1-011）。

    返回统计：{total, inserted, updated, skipped, new, changed}。
    """
    today = date.today()
    now = datetime.utcnow()
    stats = {"total": len(items), "inserted": 0, "updated": 0,
             "skipped": 0, "new": 0, "changed": 0}

    seen: set[str] = set()
    normalized: list[dict] = []

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
        normalized.append(p)

    if not normalized:
        return stats

    existing = {}
    for sku_batch in _chunks(sorted(seen), _EXISTING_PRODUCT_LOOKUP_CHUNK_SIZE):
        existing.update({
            p.sku: p
            for p in (
                session.query(Product)
                .filter(Product.site == site, Product.sku.in_(sku_batch))
            )
        })

    for p in normalized:
        sku = p["sku"]
        row = existing.get(sku)
        if row is None:                       # 新 SKU —— F1-012 首次出现规则
            p.setdefault("is_new", True)
            obj = Product(created_time=now, updated_time=now, **_product_kwargs(p))
            session.add(obj)
            existing[sku] = obj
            product_row = obj
            stats["inserted"] += 1
            stats["new"] += 1
        else:
            if _has_changed(row, p):          # C-024：变更检测
                stats["changed"] += 1
            for k, v in _product_kwargs(p).items():
                if k in ("created_time",):
                    continue
                if (k == "review_count" and p.get("_review_count_missing")
                        and getattr(row, k, None) is not None):
                    continue
                if k == "title":
                    v = _best_title(getattr(row, k, None), v, sku)
                if k in _PRESERVE_ON_EMPTY and _is_empty(v):
                    current = getattr(row, k, None)
                    if not _is_empty(current):
                        continue
                setattr(row, k, v)
            row.updated_time = now
            product_row = row
            stats["updated"] += 1

        if not _skip_price_history(p):
            _upsert_price_history(session, site, sku, today, product_row)
    return stats


_PRODUCT_COLS = {c.name for c in Product.__table__.columns} - {"id"}


def _product_kwargs(p: dict) -> dict:
    return {k: v for k, v in p.items() if k in _PRODUCT_COLS}


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _best_title(current, incoming, sku: str | None):
    if _is_empty(incoming):
        return current
    if _is_empty(current):
        return incoming
    cur = str(current).strip()
    new = str(incoming).strip()
    if _is_weak_title(new, sku) and not _is_weak_title(cur, sku):
        return current
    if _is_weak_title(cur, sku) and not _is_weak_title(new, sku):
        return incoming
    if len(new) > len(cur) * 1.35 and len(new) - len(cur) >= 12:
        return incoming
    return incoming


def _is_weak_title(value: str | None, sku: str | None = None) -> bool:
    if not value:
        return True
    text = str(value).strip()
    if len(text) < 4:
        return True
    if sku and text.lower() == str(sku).strip().lower():
        return True
    return bool(_WEAK_TITLE_RE.match(text))


def _upsert_price_history(
        session: Session, site: str, sku: str, day: date,
        product: Product) -> None:
    values = {
        "sale_price": product.sale_price,
        "original_price": product.original_price,
        "review_count": product.review_count,
    }
    row = (session.query(PriceHistory)
           .filter(PriceHistory.site == site,
                   PriceHistory.sku == sku,
                   PriceHistory.date == day)
           .first())
    if row is None:
        session.add(PriceHistory(site=site, sku=sku, date=day, **values))
        return
    for k, v in values.items():
        if v is not None or getattr(row, k) is None:
            setattr(row, k, v)


def _skip_price_history(p: dict) -> bool:
    """Allow URL-only discovery rows to avoid creating empty daily history."""
    if not p.get("_skip_price_history_if_no_price"):
        return False
    return p.get("sale_price") is None and p.get("original_price") is None


def _has_changed(row: Product, p: dict) -> bool:
    for field in ("sale_price", "original_price", "status", "review_count"):
        if (field == "review_count" and p.get("_review_count_missing")
                and getattr(row, field) is not None):
            continue
        if p.get(field) is not None and getattr(row, field) != p.get(field):
            return True
    return False
