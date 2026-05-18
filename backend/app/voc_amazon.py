"""Amazon VOC —— 按 ASIN 取亚马逊评论 + AI 洞察分析。

整合自 voc-amazon-reviews 项目：把「输入 ASIN → 拉真实评论 → 结构化口碑报告」
的能力并入 smart-crawler，作为 MCP 工具暴露给 Agent。

评论数据走 Shulex VOC OpenAPI（异步任务：提交 RtTask01 → 轮询 RtQry01）；
分析走 smart-crawler 既有的 LLM 网关（见 nlp.py）。

环境变量：
  VOC_API_KEY   Shulex VOC OpenAPI 密钥（apps.voc.ai/openapi 免费注册）
  LLM_*         复用 nlp.py 的 LLM 网关配置
"""
from __future__ import annotations

import json
import os
import re
import time

from curl_cffi import requests as creq

from .nlp import MODEL, _client

API_BASE = "https://openapi.shulex.com"
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class VocError(RuntimeError):
    """VOC 采集/分析失败 —— 错误信息对 Agent 自解释。"""


def _validate_asin(asin: str) -> str:
    asin = (asin or "").strip().upper()
    if not _ASIN_RE.match(asin):
        raise VocError(f"ASIN 非法: {asin!r} —— 须为 10 位字母数字（如 B08N5WRWNW）")
    return asin


def fetch_amazon_reviews(asin: str, market: str = "US",
                         limit: int = 100) -> dict:
    """经 Shulex VOC API 拉取某 ASIN 的亚马逊真实评论。

    返回 {"reviews": [...], "meta": {asin, market, total_available, fetched}}。
    """
    asin = _validate_asin(asin)
    limit = max(1, min(int(limit), 1000))
    key = os.environ.get("VOC_API_KEY")
    if not key:
        raise VocError("未配置 VOC_API_KEY —— 在 apps.voc.ai/openapi 免费注册获取")
    max_page = max(1, min((limit + 9) // 10, 100))
    headers = {"X-API-Key": key, "Content-Type": "application/json"}

    # Step 1 —— 提交实时评论任务
    sub = creq.post(f"{API_BASE}/v1/api/RtTask01", headers=headers, timeout=30,
                    data=json.dumps({"asin": asin, "market": market,
                                     "maxPage": max_page, "platform": "AMAZON"}))
    try:
        sj = sub.json()
    except Exception:
        raise VocError(f"提交任务失败，响应非 JSON: {sub.text[:200]}")
    task_id = (sj.get("data") or {}).get("taskId")
    if not task_id or str(sj.get("code")) != "0":
        raise VocError(f"提交任务失败: {sj.get('message') or sj}")

    # Step 2 —— 轮询至 SUCCESS / FAILED（最多 ~120s）
    status, poll = "PENDING", {}
    for _ in range(24):
        time.sleep(5)
        r = creq.get(f"{API_BASE}/v1/api/RtQry01", headers=headers, timeout=30,
                     params={"taskId": task_id, "pageNo": 1, "pageSize": limit})
        try:
            poll = r.json()
        except Exception:
            continue
        status = (poll.get("data") or {}).get("status", "UNKNOWN")
        if status in ("SUCCESS", "FAILED"):
            break
    if status != "SUCCESS":
        d = poll.get("data") or {}
        raise VocError(f"评论任务未成功（status={status}）: "
                       f"{d.get('errorMsg') or d.get('message') or '超时'}")

    # Step 3 —— 规范化评论
    data = poll.get("data") or {}
    out = []
    for rv in (data.get("reviews") or [])[:limit]:
        out.append({
            "rating": rv.get("rating"),
            "title": rv.get("title", ""),
            "body": rv.get("body") or rv.get("content", ""),
            "date": rv.get("reviewDate", ""),
            "verified": bool(rv.get("verified") or rv.get("verifiedPurchase")),
            "author": rv.get("author") or rv.get("reviewerName", ""),
            "helpful": rv.get("helpfulVotes", 0),
        })
    return {"reviews": out, "meta": {
        "asin": asin, "market": market,
        "total_available": data.get("total", 0), "fetched": len(out)}}


_ANALYSIS_PROMPT = (
    "你是亚马逊 VOC（消费者之声）分析师。给定某 ASIN 的真实评论，"
    "产出结构化口碑洞察。只返回 JSON，不要多余文字。schema：\n"
    '{"sentiment":{"positive":int,"neutral":int,"negative":int}（百分比，和为100），'
    '"pain_points":[{"zh":"中文","en":"English","count":int}]（按提及量降序，最多6条），'
    '"selling_points":[{"zh":"中文","en":"English","count":int}]（最多6条），'
    '"listing_tips":[{"zh":"中文建议","en":"English suggestion"}]（最多5条），'
    '"summary_zh":"两三句中文总结","summary_en":"2-3 sentence English summary"}'
)


def analyze_amazon_voc(asin: str, reviews: list[dict]) -> dict:
    """对已取得的评论做 AI 口碑分析 —— 情感分布/痛点/卖点/Listing 建议。"""
    if not reviews:
        raise VocError("无评论可分析")
    sample = reviews[:120]
    blob = "\n".join(
        f"[{r.get('rating')}★] {r.get('title','')} —— {(r.get('body') or '')[:300]}"
        for r in sample)
    cli = _client()
    resp = cli.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _ANALYSIS_PROMPT},
            {"role": "user", "content": f"ASIN {asin}，{len(sample)} 条评论：\n{blob}"},
        ],
        response_format={"type": "json_object"},
    )
    try:
        report = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        raise VocError(f"分析结果解析失败: {exc}")
    report["asin"] = asin
    report["reviews_analyzed"] = len(sample)
    return report


def amazon_voc_report(asin: str, market: str = "US", limit: int = 100) -> dict:
    """一站式：取 ASIN 评论 + AI 分析 —— 「给我这个 ASIN 的 VOC 报告」即调此。"""
    fetched = fetch_amazon_reviews(asin, market=market, limit=limit)
    if not fetched["reviews"]:
        raise VocError(f"未取到 {asin} 的评论")
    report = analyze_amazon_voc(asin, fetched["reviews"])
    report["market"] = market
    report["meta"] = fetched["meta"]
    return report
