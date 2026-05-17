"""评论 NLP 分析 —— 模块二/三（规格 F2-010 ~ F2-016）。

用 flatkey.ai OpenAI 兼容网关（GPT-5.x）做情感分析 + 多级分类 + 主题提取。
规格 F2-014：必须用原文直接分析、不翻译 —— LLM 原生多语种，prompt 强调这点。

需环境变量：
  OPENAI_API_KEY   flatkey.ai 的 sk- 密钥（必填）
  LLM_BASE_URL     默认 https://app.flatkey.ai/v1
  LLM_MODEL        默认 gpt-5.4-mini（分类任务，便宜够用）
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from .db import session_scope
from .models import Review

GATEWAY = os.environ.get("LLM_BASE_URL", "https://app.flatkey.ai/v1")
MODEL = os.environ.get("LLM_MODEL", "gpt-5.4-mini")

CATEGORIES_L1 = ["质量", "物流", "客服", "价格", "外观", "包装", "尺寸", "其他"]

_SYSTEM = (
    "你是电商评论分析专家。给定一条消费者评论（可能是任意语言），"
    "请基于原文直接分析、不要翻译。只输出 JSON，字段：\n"
    "sentiment: positive / negative / neutral 之一\n"
    "sentiment_score: -1.0 到 1.0 的浮点数\n"
    f"category_l1: 从 {CATEGORIES_L1} 中选最贴切的一个\n"
    "category_l2: 一级分类下的具体二级标签（简短中文，如「材质差」「配送慢」）\n"
    "topics: 3-6 个主题关键词数组（用评论原文语言）\n"
    "只返回 JSON，不要其他文字。"
)


def _client():
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("未配置 OPENAI_API_KEY（flatkey.ai 密钥）")
    return OpenAI(base_url=GATEWAY, api_key=key)


def analyze_text(content: str, title: str | None = None) -> dict:
    """分析一条评论文本，返回情感 + 分类 + 主题。"""
    client = _client()
    text = (f"标题：{title}\n" if title else "") + f"评论：{content}"
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": text[:4000]}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    return {
        "sentiment": data.get("sentiment"),
        "sentiment_score": _f(data.get("sentiment_score")),
        "category_l1": data.get("category_l1"),
        "category_l2": data.get("category_l2"),
        "topics": data.get("topics") or [],
    }


def analyze_pending(limit: int = 300) -> dict:
    """批量分析尚未分析的评论（sentiment 为空）。"""
    with session_scope() as s:
        pending = (s.query(Review).filter(Review.sentiment.is_(None))
                   .limit(limit).all())
        ids = [r.id for r in pending]
    done, failed = 0, 0
    for rid in ids:
        try:
            with session_scope() as s:
                r = s.get(Review, rid)
                if not r or not r.content:
                    continue
                res = analyze_text(r.content, r.title)
                r.sentiment = res["sentiment"]
                r.sentiment_score = res["sentiment_score"]
                r.category_l1 = res["category_l1"]
                r.category_l2 = res["category_l2"]
                r.nlp_topics = res["topics"]
                r.analyzed_time = datetime.utcnow()
            done += 1
        except Exception:
            failed += 1
    return {"analyzed": done, "failed": failed, "candidates": len(ids)}


def _f(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None
