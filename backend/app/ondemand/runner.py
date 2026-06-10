"""按需抓取编排 —— fetch(url) → 抓 listing + 评论 → 入库。

listing 入 Product 表(经 pipeline.upsert_products);评论入 Review 表
(去重逻辑对齐 review_runner.upsert_reviews_into)。被封时切代理重试。
"""
from __future__ import annotations

import re

from .. import proxy_pool
from ..antiban import BlockedError
from ..db import session_scope
from ..pipeline import upsert_products
from ..proxy import get_proxy
from ..review_runner import upsert_reviews_into
from .base import OnDemandResult
from .registry import classify_url, detect_platform, get_crawler

_MAX_RETRY = 6  # 反爬强、住宅 IP 信誉时好时坏,多轮换出口 IP(美客多常需 2-4 次)

# 代理/隧道层失败特征 —— Chromium/Playwright 在 CONNECT 隧道握手失败时抛的是
# 普通 Exception(非 BlockedError)。这类失败是**出口坏**而非站点封禁,
# 必须换出口重试 + 把坏出口踢进冷却,否则 round-robin 一命中坏代理就连续失败。
# 线上实测坏出口表现:CONNECT 返回 502/403、或 net::ERR_TUNNEL_CONNECTION_FAILED。
_PROXY_ERR_RE = re.compile(
    r"ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|"
    r"ERR_(?:CONNECTION_(?:RESET|CLOSED|REFUSED|TIMED_OUT)|TIMED_OUT)|"
    r"NS_ERROR_PROXY_CONNECTION_REFUSED|"
    r"proxy|tunnel|502 Bad Gateway|407 Proxy",
    re.I)


def _is_proxy_error(exc: Exception) -> bool:
    return bool(_PROXY_ERR_RE.search(str(exc)))


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
    """抓单个 itemId 的 listing + 评论。

    listing 与评论解耦:listing 被封/出错只记 note,**仍继续抓评论**
    (评论按 (platform, review_id) 独立去重、无 Product 外键,可单独入库)。
    listing 抓到才算这一步成功;listing 全程被封才整体放弃。
    """
    last_err = None
    for attempt in range(_MAX_RETRY):
        proxy = get_proxy(crawler.proxy_tier)
        try:
            res.add_listing(crawler.fetch_listing(iid, url, proxy=proxy))
            proxy_pool.report_success(proxy)    # 出口可用,恢复其健康分
            _fetch_reviews_safe(crawler, iid, url, review_limit, res, proxy)
            return
        except BlockedError as exc:
            last_err = exc                       # 站点封禁 → 换出口重试
            proxy_pool.report_failure(proxy, hard=True)
            continue
        except Exception as exc:
            if _is_proxy_error(exc):             # 隧道/代理坏 → 换出口重试
                last_err = exc
                proxy_pool.report_failure(proxy, hard=True)
                continue
            res.note(f"{iid}: {exc}")            # 其它错误隔离,不重试
            return
    # listing 全程被封 —— 评论接口反爬宽松,仍单独试一轮(本地无代理时尤为关键)
    res.note(f"{iid}: listing 多次被封放弃({last_err}),仅尝试评论")
    _fetch_reviews_safe(crawler, iid, url, review_limit, res, proxy=None)


def _fetch_reviews_safe(crawler, iid, url, review_limit, res, proxy) -> None:
    """抓评论并隔离其失败 —— 不让评论问题影响已拿到的 listing。

    抓前查库该商品已有 review_id 传给 crawler:库里已有则走增量(只补最新),
    无则首次全量。增量正确性依赖 crawler 用时间倒序翻页(美客多 order=dateCreated)。
    """
    try:
        known = _known_review_ids(crawler, iid)
        # 仅当 crawler 的 fetch_reviews 支持 known_ids 才传(lazada/shopee 暂不支持增量)
        import inspect
        if "known_ids" in inspect.signature(crawler.fetch_reviews).parameters:
            reviews = crawler.fetch_reviews(iid, url, limit=review_limit,
                                            proxy=proxy, known_ids=known)
        else:
            reviews = crawler.fetch_reviews(iid, url, limit=review_limit,
                                            proxy=proxy)
        res.add_reviews(reviews)
    except Exception as exc:
        res.note(f"{iid}: 评论抓取失败({exc})")


def _known_review_ids(crawler, iid) -> set[str]:
    """查库:该平台该 sku 已有的 review_id 集合(供增量抓取碰到即停)。"""
    from ..models import Review
    site = getattr(crawler, "SITE", None) or f"ondemand_{crawler.platform}"
    sku = iid[0] if isinstance(iid, tuple) else iid
    try:
        with session_scope() as s:
            rows = (s.query(Review.review_id)
                    .filter(Review.platform == site, Review.sku == str(sku))
                    .all())
            return {r[0] for r in rows if r[0]}
    except Exception:
        return set()   # 查库失败不阻断抓取,退化为全量


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
