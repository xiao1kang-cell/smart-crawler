"""评论 feed 导入 —— 模块二 F2-007 / 模块三 F3-001。

可导出的评论平台（TrustedShop / Opiniones Verificadas / Aosom 自身评论）
不必爬取 —— 直接导入其 CSV/Excel 导出文件。列名自动识别（多语言同义词）。
"""
from __future__ import annotations

import hashlib

import pandas as pd

from .review_runner import _upsert_reviews

# 列名同义词（小写匹配，覆盖 EN/DE/FR/ES）
COLMAP = {
    "review_id": ["review_id", "id", "reviewid", "review id"],
    "rating": ["rating", "stars", "score", "note", "bewertung",
               "puntuacion", "valoracion", "评分", "评星"],
    "content": ["content", "comment", "comments", "text", "review",
                "reviewbody", "review_body", "kommentar", "commentaire",
                "comentario", "评论", "评论内容"],
    "title": ["title", "headline", "titel", "titre", "titulo", "标题"],
    "reviewer_name": ["reviewer_name", "author", "name", "reviewer",
                      "kunde", "nombre", "client", "评论者"],
    "reviewer_country": ["country", "reviewer_country", "land", "pays", "pais"],
    "review_date": ["review_date", "date", "datum", "fecha", "date_created",
                    "datepublished", "published", "日期"],
    "sku": ["sku", "product_sku", "artikelnummer", "referencia"],
    "order_id": ["order_id", "order_number", "bestellnummer", "order",
                 "commande", "pedido", "订单号"],
    "language": ["language", "lang", "sprache", "langue", "idioma"],
    "reply_content": ["reply", "reply_content", "antwort", "reponse",
                      "respuesta", "商家回复"],
}


def import_feed(path: str, platform: str, site: str) -> dict:
    """导入一个评论导出文件（CSV/Excel）到 reviews 表。"""
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    cols = {str(c).lower().strip(): c for c in df.columns}

    def pick(field: str):
        for alias in COLMAP.get(field, []):
            if alias in cols:
                return cols[alias]
        return None

    mapping = {f: pick(f) for f in COLMAP}
    if not mapping["content"]:
        raise ValueError(f"未识别到评论内容列，现有列：{list(df.columns)}")

    reviews = []
    for _, row in df.iterrows():
        def val(field):
            col = mapping[field]
            if not col:
                return None
            v = row[col]
            return None if pd.isna(v) else v

        content = val("content")
        if not content:
            continue
        rid = val("review_id")
        if not rid:                       # 无 ID → 由内容指纹生成稳定 ID
            seed = f"{platform}|{site}|{content}|{val('review_date')}"
            rid = hashlib.sha1(seed.encode()).hexdigest()[:20]
        reviews.append({
            "review_id": str(rid), "platform": platform, "site": site,
            "reviewer_name": _s(val("reviewer_name")),
            "reviewer_country": _s(val("reviewer_country")),
            "rating": val("rating"),
            "title": _s(val("title")),
            "content": str(content),
            "language": _s(val("language")),
            "review_date": _s(val("review_date")),
            "order_id": _s(val("order_id")),
            "sku": _s(val("sku")),
            "reply_content": _s(val("reply_content")),
        })
    stats = _upsert_reviews(reviews)
    return {"platform": platform, "site": site,
            "rows": len(df), "parsed": len(reviews), **stats,
            "mapped_columns": {k: v for k, v in mapping.items() if v}}


def _s(v):
    return str(v) if v is not None else None
