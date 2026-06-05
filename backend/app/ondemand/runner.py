"""按需抓取编排 —— fetch(url) → 抓 listing + 评论 → 入库。

listing 入 Product 表(经 pipeline.upsert_products);评论入 Review 表
(去重逻辑对齐 review_runner.upsert_reviews_into)。被封时切代理重试。
"""
from __future__ import annotations

from ..antiban import BlockedError
from ..db import session_scope
from ..pipeline import upsert_products
from ..proxy import get_proxy
from ..review_runner import upsert_reviews_into
from .base import OnDemandResult
from .registry import classify_url, detect_platform, get_crawler

_MAX_RETRY = 3


def fetch(url: str, *, max_items: int = 100, review_limit: int = 100,
          crawler=None, kind: str | None = None,
          do_persist: bool = True) -> OnDemandResult:
    """抓取一条 URL(单品或列表页)的 listing + 评论。

    crawler/kind 仅供测试注入;生产调用只传 url。
    do_persist=False 时只抓不入库(单测用)。
    """
    res = OnDemandResult()
    platform = getattr(crawler, "platform", None) or detect_platform(url)
    if platform is None:
        res.note(f"无法识别平台: {url}")
        return res
    if crawler is None:
        crawler = get_crawler(platform)
    kind = kind or classify_url(url)

    # ---- 收集待抓 itemId ----
    try:
        if kind == "product":
            item_ids = [crawler.parse_item_id(url)]
        else:
            proxy = get_proxy(crawler.proxy_tier)
            item_ids = crawler.enumerate_listing(url, max_items=max_items,
                                                 proxy=proxy)
            if len(item_ids) >= max_items:
                res.note(f"列表枚举达上限 {max_items},可能有截断")
    except Exception as exc:
        res.note(f"解析/枚举失败: {exc}")
        return res

    # ---- 逐 ID 抓 listing + 评论 ----
    for iid in item_ids:
        _fetch_one(crawler, iid, url, review_limit, res)

    if do_persist:
        with session_scope() as s:
            persist(res, session=s)
    return res


def _fetch_one(crawler, iid, url, review_limit, res: OnDemandResult) -> None:
    last_err = None
    for attempt in range(_MAX_RETRY):
        proxy = get_proxy(crawler.proxy_tier)
        try:
            res.add_listing(crawler.fetch_listing(iid, url, proxy=proxy))
            res.add_reviews(crawler.fetch_reviews(iid, url, limit=review_limit,
                                                  proxy=proxy))
            return
        except BlockedError as exc:
            last_err = exc                      # 切代理重试
            continue
        except Exception as exc:
            res.note(f"{iid}: {exc}")           # 失败隔离,不重试
            return
    res.note(f"{iid}: 多次被封放弃({last_err})")


def persist(res: OnDemandResult, *, session) -> dict:
    """listing → Product upsert;评论 → Review upsert。返回统计。"""
    by_site: dict[str, list[dict]] = {}
    for p in res.listings:
        by_site.setdefault(p["site"], []).append(p)
    listing_stats = {"inserted": 0, "updated": 0, "skipped": 0}
    for site, items in by_site.items():
        st = upsert_products(session, site, items)
        for k in listing_stats:
            listing_stats[k] += st.get(k, 0)

    review_stats = upsert_reviews_into(session, res.reviews)
    return {"listings": listing_stats, "reviews": review_stats}
