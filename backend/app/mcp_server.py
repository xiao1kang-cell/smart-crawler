"""smart-crawler MCP 服务器 —— 让 AI Agent 直接发现并调用采集能力。

设计原则（见 playbook「Agents 是新分发渠道」）：Agent 调用的是能力不是界面。
本模块把 smart-crawler 的核心能力暴露成 MCP 工具，Agent 通过 MCP 协议发现、
调用 —— 每个工具描述清晰、参数最少、返回结构化 JSON、错误自解释。

部署：FastAPI 挂载在 /mcp（见 main.py）；亦可独立运行 `python -m app.mcp_server`。
"""
from __future__ import annotations

import json
import inspect
import time
from functools import wraps

import structlog
from fastmcp import FastMCP
from sqlalchemy import func

from .access import has_scope
from .agent_runtime import (
    agent_key_for_api_key,
    enrich_usage,
    insufficient_scope_response,
    run_with_agent_memory,
)
from .billing import record_usage
from .db import SessionLocal
from .mcp_context import get_current_api_key
from .models import (ApiKey, Keyword, PriceHistory, Product, Promotion, Review,
                      ShoppingResult, Site)

logger = structlog.get_logger(__name__)

mcp = FastMCP(
    "smart-crawler",
    instructions=(
        "Agent 时代电商情报采集引擎。对外主推 3 个 Agent-first 工具："
        "1) query_warehouse(intent, limit) 先查已有 warehouse，0 credits；"
        "2) scrape_url(url) 只在需要页面内容时抓单页；"
        "3) crawl_site(url) 默认 dry_run=true，只做可行性和成本预估。"
        "Agent 默认只传主参数；复杂参数和其他工具属于 advanced/legacy 兼容入口。"
        "所有主推工具返回 usage：credits_used、balance、cache_hit、source、records、"
        "duration_ms、cost_if_retry；失败时读取 warnings[].next_step。"
        "覆盖主流跨境电商 / 平台市场 / 社媒渠道：Walmart / Target / AliExpress / "
        "eBay / Vidaxl / Songmics / Costway / Homary / Idealo / Otto / BOL / "
        "CDiscount / IKEA / Crate&Barrel / WestElm / Wayfair / Allegro / Article / "
        "Yaheetech / VonHaus / Flexispot / Overstock / BCP / Woltu。"
    ),
)


def _product(p: Product) -> dict:
    return {
        "sku": p.sku, "title": p.title, "brand": p.brand, "site": p.site,
        "category": p.category_path,
        "sale_price": p.sale_price, "original_price": p.original_price,
        "currency": p.currency, "on_promotion": bool(
            p.original_price and p.sale_price and p.original_price > p.sale_price),
        "rating": p.ratings, "review_count": p.review_count,
        "status": p.status, "url": p.product_url,
    }


def metered_tool(required_scope: str = "crawler:read", cacheable: bool = False):
    """Register an MCP tool with scope checks and usage metering."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = get_current_api_key()
            if ctx and not has_scope(ctx.scopes, required_scope):
                return _insufficient_scope(required_scope, ctx.scopes)
            started = time.perf_counter()
            agent_key = agent_key_for_api_key(ctx.api_key_id if ctx else None)
            use_cache = cacheable and not kwargs.get("force_live", False)
            if kwargs.get("mode") == "advanced":
                use_cache = False
            if use_cache:
                s = SessionLocal()
                try:
                    result = run_with_agent_memory(
                        s,
                        agent_key=agent_key,
                        tool=fn.__name__,
                        payload=_normalized_tool_payload(fn, args, kwargs),
                        producer=lambda: fn(*args, **kwargs),
                    )
                finally:
                    s.close()
            else:
                result = fn(*args, **kwargs)
            duration_ms = int((time.perf_counter() - started) * 1000)
            if isinstance(result, dict):
                _enrich_mcp_usage(ctx.api_key_id if ctx else None, result,
                                  duration_ms=duration_ms)
            if ctx:
                _record_mcp_usage(ctx.api_key_id, fn.__name__, result, duration_ms)
            return result
        return mcp.tool(wrapper)
    return decorator


def _normalized_tool_payload(fn, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception:
        return {"args": args, "kwargs": kwargs}


def _record_mcp_usage(api_key_id: int, tool_name: str, result, duration_ms: int) -> None:
    try:
        payload = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
    except Exception:
        payload = b""
    try:
        record_usage(
            api_key_id=api_key_id,
            endpoint=f"/mcp/{tool_name}",
            record_count=_infer_records(result),
            credits_used=_infer_credits(result),
            bytes_returned=len(payload),
            duration_ms=duration_ms,
        )
    except Exception as exc:
        logger.warning(
            "mcp.usage_record_failed",
            api_key_id=api_key_id,
            tool_name=tool_name,
            duration_ms=duration_ms,
            error=str(exc),
        )


def _infer_records(result) -> int:
    if not isinstance(result, dict):
        return len(result) if isinstance(result, list) else int(bool(result))
    usage = result.get("usage") or {}
    if usage.get("records") is not None:
        return int(usage.get("records") or 0)
    for key in ("products", "reviews", "promotions", "items", "data", "sources"):
        if isinstance(result.get(key), list):
            return len(result[key])
    if isinstance(result.get("top_contributors"), list):
        return len(result["top_contributors"])
    return int(result.get("returned") or result.get("itemCount") or
               result.get("count") or result.get("total") or 0)


def _infer_credits(result) -> int:
    if isinstance(result, dict):
        usage = result.get("usage") or {}
        if usage.get("credits_used") is not None:
            return int(usage.get("credits_used") or 0)
    return _infer_records(result)


def _insufficient_scope(required: str, granted: list[str]) -> dict:
    return insufficient_scope_response(required, granted)


def _enrich_mcp_usage(api_key_id: int | None, result: dict,
                      duration_ms: int) -> None:
    s = SessionLocal()
    try:
        try:
            key = s.get(ApiKey, api_key_id) if api_key_id else None
        except Exception:
            s.rollback()
            key = None
        usage = result.setdefault("usage", {})
        usage.setdefault("duration_ms", duration_ms)
        enrich_usage(s, result, api_key=key, default_cost_if_retry=3)
    finally:
        s.close()


@metered_tool()
def list_data_sources() -> list[dict]:
    """[LEGACY] 列出全部可用数据源：46 个竞品独立站 + 评论平台 + Google Shopping。
    Agent 先调这个了解能查什么品牌/站点/数据类型。"""
    s = SessionLocal()
    try:
        out = []
        for site in s.query(Site).all():
            n = s.query(Product).filter(Product.site == site.site).count()
            out.append({"site": site.site, "brand": site.brand,
                        "country": site.country, "type": "product",
                        "platform": site.platform, "product_count": n})
        for plat in ("trustpilot", "reviews_io", "google_map"):
            n = s.query(Review).filter(Review.platform == plat).count()
            out.append({"source": plat, "type": "review", "review_count": n})
        return out
    finally:
        s.close()


@metered_tool()
def search_competitor_products(
    brand: str | None = None, country: str | None = None,
    keyword: str | None = None, category: str | None = None,
    min_price: float | None = None, max_price: float | None = None,
    on_promotion: bool = False, limit: int = 20,
) -> dict:
    """[LEGACY] 搜索竞品商品。可按品牌(如 SONGMICS/Costway/Homary/Vidaxl)、国家(US/UK/DE…)、
    标题关键词、品类、价格区间筛选；on_promotion=true 只看在促销的。
    返回结构化商品列表（SKU/标题/价格/促销/评分/状态/URL）。"""
    s = SessionLocal()
    try:
        q = s.query(Product)
        if brand:
            q = q.filter(Product.brand.ilike(f"%{brand}%"))
        if country:
            q = q.filter(Product.site.ilike(f"%_{country.lower()}"))
        if keyword:
            q = q.filter(Product.title.ilike(f"%{keyword}%"))
        if category:
            q = q.filter(Product.category_path.ilike(f"%{category}%"))
        if min_price is not None:
            q = q.filter(Product.sale_price >= min_price)
        if max_price is not None:
            q = q.filter(Product.sale_price <= max_price)
        if on_promotion:
            q = q.filter(Product.original_price > Product.sale_price)
        total = q.count()
        rows = q.order_by(Product.id).limit(min(limit, 100)).all()
        return {"total": total, "returned": len(rows),
                "products": [_product(p) for p in rows]}
    finally:
        s.close()


@metered_tool()
def get_product_detail(site: str, sku: str) -> dict:
    """[LEGACY] 取单个商品的完整信息 + 历史价格曲线。site 如 songmics_us，sku 为商品编码。"""
    s = SessionLocal()
    try:
        p = s.query(Product).filter(Product.site == site,
                                    Product.sku == sku).first()
        if not p:
            return {"error": f"未找到商品 site={site} sku={sku}"}
        hist = (s.query(PriceHistory)
                .filter(PriceHistory.site == site, PriceHistory.sku == sku)
                .order_by(PriceHistory.date).all())
        d = _product(p)
        d["price_history"] = [{"date": h.date.isoformat(),
                               "sale_price": h.sale_price} for h in hist]
        return d
    finally:
        s.close()


@metered_tool()
def list_promotions(site: str | None = None, limit: int = 30) -> dict:
    """[LEGACY] 列出竞品当前促销活动（售价低于原价的商品），含折扣率。可按 site 筛选。"""
    s = SessionLocal()
    try:
        q = s.query(Promotion)
        if site:
            q = q.filter(Promotion.site == site)
        total = q.count()
        rows = q.order_by(Promotion.discount_percent.desc()).limit(
            min(limit, 100)).all()
        return {"total": total, "promotions": [{
            "site": r.site, "sku": r.sku, "title": r.product_title,
            "original_price": r.original_price,
            "promotion_price": r.promotion_price,
            "discount_percent": r.discount_percent} for r in rows]}
    finally:
        s.close()


@metered_tool()
def get_voc_reviews(site: str | None = None, platform: str | None = None,
                    sentiment: str | None = None, min_rating: int | None = None,
                    limit: int = 20) -> dict:
    """[LEGACY] 取消费者口碑评论(VOC)。platform: trustpilot/reviews_io/google_map；
    sentiment: positive/negative/neutral；可按 site、最低评分筛选。
    返回评论 + NLP 情感/分类标注。"""
    s = SessionLocal()
    try:
        q = s.query(Review)
        if site:
            q = q.filter(Review.site == site)
        if platform:
            q = q.filter(Review.platform == platform)
        if sentiment:
            q = q.filter(Review.sentiment == sentiment)
        if min_rating is not None:
            q = q.filter(Review.rating >= min_rating)
        total = q.count()
        rows = q.order_by(Review.review_date.desc()).limit(
            min(limit, 100)).all()
        return {"total": total, "reviews": [{
            "platform": r.platform, "site": r.site, "rating": r.rating,
            "content": r.content, "sentiment": r.sentiment,
            "category": r.category_l1, "sub_category": r.category_l2,
            "review_date": r.review_date.isoformat() if r.review_date else None,
        } for r in rows]}
    finally:
        s.close()


@metered_tool()
def voc_summary(site: str | None = None) -> dict:
    """[LEGACY] 口碑分析汇总：情感分布 + 痛点分类占比。看竞品/自身的消费者声音全貌。"""
    s = SessionLocal()
    try:
        q = s.query(Review)
        if site:
            q = q.filter(Review.site == site)
        total = q.count()
        sent = dict(q.with_entities(Review.sentiment, func.count(Review.id))
                    .group_by(Review.sentiment).all())
        cats = dict(q.with_entities(Review.category_l1, func.count(Review.id))
                    .filter(Review.category_l1.isnot(None))
                    .group_by(Review.category_l1).all())
        return {"total_reviews": total, "sentiment": sent,
                "pain_categories": dict(sorted(cats.items(),
                                               key=lambda x: -x[1]))}
    finally:
        s.close()


@metered_tool()
def competitor_landscape(keyword: str) -> dict:
    """[LEGACY] Google Shopping 竞争格局：某关键词下各商家的出现占有率排名。"""
    s = SessionLocal()
    try:
        rows = s.query(ShoppingResult).filter(
            ShoppingResult.keyword == keyword).all()
        total = len(rows) or 1
        agg: dict = {}
        for r in rows:
            m = r.merchant or "(unknown)"
            agg[m] = agg.get(m, 0) + 1
        share = sorted(({"merchant": m, "count": c,
                         "share_pct": round(c / total * 100, 1)}
                        for m, c in agg.items()), key=lambda x: -x["count"])
        return {"keyword": keyword, "result_count": len(rows),
                "merchant_share": share}
    finally:
        s.close()


@metered_tool(required_scope="crawler:scrape")
def amazon_voc_report(asin: str, market: str = "US", limit: int = 100) -> dict:
    """[ADVANCED] 取某亚马逊 ASIN 的真实评论并做 AI 口碑分析（整合自 voc-amazon-reviews）。
    返回情感分布、痛点、卖点、Listing 优化建议、中英文总结。
    asin: 10 位商品编码；market: US/GB/DE/FR/IT/ES/JP/CA 等；limit: 评论数(1-1000)。"""
    from .voc_amazon import VocError, amazon_voc_report as _report
    try:
        return _report(asin, market=market, limit=limit)
    except VocError as exc:
        return {"error": str(exc)}


@metered_tool(required_scope="crawler:scrape")
def fetch_amazon_reviews(asin: str, market: str = "US",
                         limit: int = 100) -> dict:
    """[ADVANCED] 只取某亚马逊 ASIN 的原始评论数组（不做分析）。
    适合 Agent 自己接分析管线。返回 {reviews, meta}。"""
    from .voc_amazon import VocError, fetch_amazon_reviews as _fetch
    try:
        return _fetch(asin, market=market, limit=limit)
    except VocError as exc:
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Reddit Playbook Tools
# ─────────────────────────────────────────────────────────────────────────────

@metered_tool(required_scope="crawler:scrape")
def reddit_top_contributors(subreddit: str, top_n: int = 3) -> dict:
    """[ADVANCED] 找某 subreddit 的 top N 贡献者（按发帖数 + 总赞数综合排名）。
    返回用户名、karma、账号年龄、帖子统计。后续可传给 reddit_user_activity 或
    reddit_subreddit_playbook 做深度分析。
    示例：reddit_top_contributors("entrepreneur", top_n=3)"""
    from .crawlers.reddit import get_top_contributors
    try:
        return {"subreddit": subreddit,
                "top_contributors": get_top_contributors(subreddit, top_n=top_n)}
    except Exception as exc:
        return {"error": str(exc), "subreddit": subreddit}


@metered_tool(required_scope="crawler:scrape")
def reddit_user_activity(username: str, subreddit: str | None = None,
                          post_limit: int = 100,
                          comment_limit: int = 100) -> dict:
    """[ADVANCED] 取一位 Reddit 用户的完整发帖 + 评论活动。
    subreddit 不填则取全站活动。返回：profile stats / top posts / 月度活跃时间线。
    示例：reddit_user_activity("user123", subreddit="entrepreneur")"""
    from .crawlers.reddit import get_user_activity
    try:
        return get_user_activity(username, subreddit=subreddit,
                                 post_limit=min(post_limit, 200),
                                 comment_limit=min(comment_limit, 200))
    except Exception as exc:
        return {"error": str(exc), "username": username}


@metered_tool(required_scope="crawler:scrape")
def reddit_subreddit_playbook(subreddit: str, top_n: int = 3) -> dict:
    """[ADVANCED] 一键生成 subreddit 的 top N 贡献者 playbook。

    完整流程：① 找 top N 贡献者 → ② 抓每人的帖子/评论 → ③ LLM 分析生成 playbook。
    每位贡献者的 playbook 包含：成长时间线、内容公式、爆款分析、5步可复制路径。
    返回结构化 JSON + Markdown 格式的完整 playbook 文档。

    注意：每人约 3-5 分钟（Reddit 限流 + LLM 调用），top_n=3 约 10-15 分钟。
    示例：reddit_subreddit_playbook("entrepreneur", top_n=3)"""
    from .reddit_playbook import generate_subreddit_playbook
    try:
        return generate_subreddit_playbook(subreddit, top_n=top_n)
    except Exception as exc:
        return {"error": str(exc), "subreddit": subreddit}


# ─────────────────────────────────────────────────────────────────────────────
# Influencer Discovery Tools — TikTok / Instagram / Facebook / YouTube
# Native replacement for Apify + ScraperAPI (deployed 2026-05-28).
# ─────────────────────────────────────────────────────────────────────────────

@metered_tool(required_scope="crawler:scrape")
def discover_creators_by_hashtag(
    platform: str, hashtags: list[str], limit: int = 38,
) -> dict:
    """[ADVANCED] 按 hashtag 发现创作者 —— 替代 Apify TikTok/Instagram/Facebook scraper。

    platform: "tiktok" / "instagram" / "facebook"
      - tiktok 走 HTTP 抓取，当前被 2026-05 反爬挡（返回 0 items，需走 tiktok_phone）
      - instagram 需要 NAS 路径有 IG_COOKIES_PATH 指向的 cookie 文件
      - facebook 需要 FB_COOKIES_PATH cookie；hashtags 字段作为搜索关键词
    hashtags: 不带 # 的列表，如 ["amazonfba", "amazonseller"]
    limit: 返回创作者上限（1-200，默认 38）

    返回每条 CreatorRecord:
      {channelId, name, platform, profileUrl, handle,
       followerCount, email, websiteUrl}
    """
    from .influencers.discover import dispatch
    if platform not in ("tiktok", "instagram", "facebook"):
        return {"error": f"platform must be tiktok/instagram/facebook, got {platform}"}
    try:
        items = dispatch(platform, {"hashtags": hashtags}, limit=max(1, min(limit, 200)))
        return {"platform": platform, "hashtags": hashtags,
                "itemCount": len(items), "items": items}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                "platform": platform, "hashtags": hashtags}


@metered_tool(required_scope="crawler:scrape")
def enrich_youtube_about(urls: list[str]) -> dict:
    """[ADVANCED] 从 YouTube About 页抽取 email + 外链 —— 替代 ScraperAPI。

    urls: YouTube 频道 URL 列表，如 ["https://www.youtube.com/@MrBeast",
          "https://www.youtube.com/@MKBHD/about"]
    返回每个 url 对应的 {email, websiteUrl}（按输入顺序）。
    """
    from .influencers.discover import dispatch
    try:
        items = dispatch("youtube_about", {"urls": urls}, limit=len(urls))
        return {"itemCount": len(items),
                "items": [{"url": u, **it} for u, it in zip(urls, items)]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "urls": urls}


@metered_tool(required_scope="crawler:scrape")
def ingest_tiktok_phone_results(hashtag: str, items: list[dict]) -> dict:
    """[ADVANCED] 收 matrix-mvp 手机驱动推上来的 TikTok 创作者批次。

    items: 每条为 {"authorMeta": {"uniqueId", "nickName", "fans", ...}}（Apify 兼容形态）
    返回 {runId, datasetId, itemCount} —— 调用方可后续用 get_discover_run_items 取数据。
    主要给 matrix-mvp/poc-tiktok/phone_driver.py 调用。
    """
    from .influencers.discover_models import map_tiktok
    from .influencers.run_registry import REGISTRY
    mapped = []
    for raw in items:
        rec = map_tiktok(raw)
        if rec is not None:
            mapped.append(rec.to_dict())
    rid = REGISTRY.create_run()
    REGISTRY.mark_succeeded(rid, items=mapped)
    return {"runId": rid, "datasetId": rid, "itemCount": len(mapped),
            "hashtag": hashtag}


@metered_tool()
def get_discover_run_items(
    run_id: str, limit: int = 1000, offset: int = 0,
) -> dict:
    """[ADVANCED] 取一个 discover run 已采集到的 items（带分页）。

    主要用于回看之前 discover_creators_by_hashtag / ingest_tiktok_phone_results 的结果。
    run/dataset 在内存里保留 1 小时后 GC。
    """
    from .influencers.run_registry import REGISTRY
    run = REGISTRY.get_run(run_id)
    if run is None:
        return {"error": f"run not found: {run_id}"}
    items = REGISTRY.get_items(run_id, limit=max(1, min(limit, 10000)),
                                offset=max(0, offset))
    return {"runId": run_id, "status": run["status"],
            "itemCount": run["itemCount"], "items": items,
            "startedAt": run["startedAt"], "finishedAt": run["finishedAt"]}


# ─────────────────────────────────────────────────────────────────────────────
# Agent-first Crawler Tools — shared with /api/v2
# ─────────────────────────────────────────────────────────────────────────────

@metered_tool(required_scope="crawler:scrape", cacheable=True)
def scrape_url(url: str, formats: list[str] | None = None,
               force_live: bool = False, mode: str = "standard") -> dict:
    """Agent 推荐只传 url：抓取单个页面。

    默认返回 markdown + structured，优先 warehouse，未命中再 live scrape 并消耗 credits。
    同一 API key 5 分钟内重复调用会命中 agent memory，credits_used=0。
    usage 返回 credits_used、balance、cache_hit、source、records、duration_ms、cost_if_retry。
    formats/force_live/mode 是 advanced 兼容参数；普通 Agent 不要主动填写。
    mode="advanced" 会走 browser_pool 本地 Playwright 渲染，成本更高，通常只在标准抓取失败后重试。
    失败时看 warnings[].next_step：通常先 query_warehouse，再决定是否 advanced 重试。
    """
    from .agent_crawler import scrape_url as _scrape_url
    s = SessionLocal()
    try:
        return _scrape_url(s, url, formats=formats,
                           force_live=force_live, mode=mode)
    finally:
        s.close()


@metered_tool(cacheable=True)
def map_site(url: str, limit: int = 1000, search: str | None = None) -> dict:
    """[ADVANCED] 列出某个已配置站点在仓库中的已知商品 URL。

    适合 Agent 在大规模 crawl 前先判断已有覆盖，避免重复抓取。
    """
    from .agent_crawler import map_site as _map_site
    s = SessionLocal()
    try:
        return _map_site(s, url, limit=limit, search=search)
    finally:
        s.close()


@metered_tool()
def crawl_site(url: str, limit: int = 1000, dry_run: bool = True) -> dict:
    """Agent 推荐只传 url：验证或触发一个已配置站点的异步整站采集。

    默认 dry_run=true，不会入队，credits_used=0，只返回可行性和成本提示。
    只有用户明确要求
    "开始采集/执行整站抓取"且 API key 有 crawler:crawl scope 时，才用 dry_run=false。
    usage 返回 credits_used、balance、cache_hit、source、records、duration_ms、cost_if_retry。
    """
    from .agent_crawler import crawl_site as _crawl_site
    ctx = get_current_api_key()
    if not dry_run and ctx and not has_scope(ctx.scopes, "crawler:crawl"):
        return _insufficient_scope("crawler:crawl", ctx.scopes)
    s = SessionLocal()
    try:
        return _crawl_site(s, url, limit=limit, dry_run=dry_run)
    finally:
        s.close()


@metered_tool()
def get_crawl_job(job_id: int) -> dict:
    """[ADVANCED] 查询 crawl_site 返回的采集任务状态和前 100 条结果。"""
    from .agent_crawler import get_crawl_job as _get_crawl_job
    s = SessionLocal()
    try:
        return _get_crawl_job(s, job_id)
    finally:
        s.close()


@metered_tool(required_scope="crawler:scrape", cacheable=True)
def extract_structured_data(urls: list[str], schema: dict | None = None,
                            instruction: str | None = None) -> dict:
    """[ADVANCED] 按 JSON schema 从 URL 抽结构化字段。

    当前优先使用仓库/JSON-LD/页面 metadata；LLM schema extraction 可作为后续 fallback。
    """
    from .agent_crawler import extract_structured_data as _extract
    s = SessionLocal()
    try:
        return _extract(s, urls, schema or {}, instruction=instruction)
    finally:
        s.close()


@metered_tool(cacheable=True)
def query_warehouse(intent: str, limit: int = 20) -> dict:
    """Agent 推荐只传 intent：按自然语言意图查询 smart-crawler 商品 warehouse。

    这是 Agent 的首选入口：warehouse 查询不耗 credits，毫秒级返回。
    usage 返回 credits_used、balance、cache_hit、source、records、duration_ms、cost_if_retry。
    示例：query_warehouse("vidaxl patio storage top discounts", limit=20)
    如果结果不足，再调用 scrape_url(url) 抓单页；不要一上来 live scrape。
    """
    from .agent_crawler import query_warehouse as _query
    s = SessionLocal()
    try:
        return _query(s, intent, limit=limit)
    finally:
        s.close()


@metered_tool(cacheable=True)
def query_crawler_warehouse(query: str, site: str | None = None,
                            brand: str | None = None,
                            limit: int = 20) -> dict:
    """[LEGACY] 查询 smart-crawler 已有商品仓库，避免 Agent 一上来就实时抓取。

    query 可填关键词、SKU 或类目词；site/brand 可进一步限定范围。
    新 Agent 优先调用 query_warehouse(intent, limit)。
    """
    from .agent_crawler import query_warehouse as _query
    s = SessionLocal()
    try:
        return _query(s, query, site=site, brand=brand, limit=limit)
    finally:
        s.close()


def _call_fetch_listing_voc(url: str, max_items: int = 100,
                            review_limit: int = 100) -> dict:
    """按需抓取核心逻辑(供 MCP 工具与测试共用)。"""
    from . import ondemand

    res = ondemand.fetch(url, max_items=max_items, review_limit=review_limit)
    return {
        "url": url,
        "listings": res.listings,
        "listings_count": len(res.listings),
        "reviews": res.reviews,
        "reviews_count": len(res.reviews),
        "notes": res.notes,
    }


@metered_tool(required_scope="crawler:scrape")
def fetch_listing_voc(url: str, max_items: int = 100,
                      review_limit: int = 100) -> dict:
    """[ADVANCED] 指定 URL 抓取 listing + VOC(评论原文)。

    支持 美客多(MercadoLibre)/ Lazada / 虾皮(Shopee)。
    url 可为单商品页(精抓一条)或店铺/类目/搜索页(枚举批量抓)。
    max_items: 列表页枚举上限;review_limit: 每商品评论上限。
    数据同时落 Product/Review 表,可在控制台看板查看。"""
    return _call_fetch_listing_voc(url, max_items=max_items,
                                   review_limit=review_limit)


def _ws_id_from_ctx(db) -> int | None:
    ctx = get_current_api_key()
    if not ctx:
        return None
    row = db.get(ApiKey, ctx.api_key_id)
    return row.workspace_id if row else None


@metered_tool(required_scope="crawler:scrape", cacheable=False)
def crawl_custom_source(url: str, dataset: str, schema: dict | None = None,
                        entity_type: str = "generic", force_live: bool = False,
                        save_policy: str = "promote_if_valid",
                        max_age_sec: int | None = None) -> dict:
    """通用数据采集:任意 URL → 探测/抓取 → 带 provenance 入指定 dataset。

    warehouse-first:dataset 内同 URL 在 TTL(max_age_sec 或 dataset 默认)内命中则
    credits_used=0 直接返回。force_live=true 强制实时抓(默认进 staging 不污染主库)。
    save_policy: promote_if_valid(默认)/staging/main/quarantine。
    返回 record_id/quality_status/confidence/provenance/warnings。
    """
    from . import spine
    s = SessionLocal()
    try:
        ws = _ws_id_from_ctx(s)
        ds = spine.get_or_create_dataset(s, dataset, workspace_id=ws,
                                         entity_type=entity_type)
        out = spine.resolve(s, url, ds, workspace_id=ws, force_live=force_live,
                            max_age_sec=max_age_sec, save_policy=save_policy)
        # schema 投影(复用现有 _shape_to_schema)
        if schema and out.get("data"):
            from .agent_crawler import _shape_to_schema
            out["data"] = _shape_to_schema(out["data"], schema)
        return out
    finally:
        s.close()


@metered_tool(required_scope="crawler:read", cacheable=True)
def query_dataset(dataset: str, query: str | None = None,
                  entity_type: str | None = None, include_staging: bool = False,
                  limit: int = 20) -> dict:
    """查通用数据集(extracted_records)。默认只返 main;include_staging=true 带 staging。"""
    from . import spine
    s = SessionLocal()
    try:
        ws = _ws_id_from_ctx(s)
        ds = spine.get_or_create_dataset(s, dataset, workspace_id=ws)
        return spine.query_dataset(s, ds, query=query, entity_type=entity_type,
                                   include_staging=include_staging, limit=limit)
    finally:
        s.close()


if __name__ == "__main__":
    import os
    mcp.run(transport="http", host="0.0.0.0",
            port=int(os.environ.get("MCP_PORT", "8078")))
