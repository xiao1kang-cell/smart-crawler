"""Trustpilot 评论采集器 —— 模块二。

Trustpilot 是 Next.js 站，AWS WAF / Cloudflare 防护。用 Scrapling 的
StealthyFetcher（Camoufox 隐身浏览器）突破，评论数据在页面 `__NEXT_DATA__`。

注意：Trustpilot 对数据中心 / 被标记网段 IP 直接 403 —— 实测我方 AT&T 网段
被拦（与 Vidaxl 同因）。须配住宅代理（proxies.txt 的 [residential] 段），
见 docs/风控策略评估.md。代理到位后此采集器即可工作。
"""
from __future__ import annotations

import json
import re

from ..proxy import get_proxy

_ND_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class TrustpilotCrawler:
    platform = "trustpilot"

    def __init__(self, channel: dict, max_pages: int = 10):
        self.channel = channel
        self.domain = channel["domain"]
        self.site = channel["site"]
        self.host = channel.get("host", "www.trustpilot.com")
        self.max_pages = channel.get("max_pages", max_pages)
        self.proxy = get_proxy("residential")
        self.notes: list[str] = []

    def crawl(self) -> list[dict]:
        """返回标准化的评论 dict 列表。"""
        try:
            from scrapling.fetchers import StealthyFetcher
        except Exception as exc:
            self.notes.append(f"Scrapling 未安装: {exc}")
            return []

        reviews: list[dict] = []
        for page in range(1, self.max_pages + 1):
            url = f"https://{self.host}/review/{self.domain}?page={page}"
            try:
                kw = dict(headless=True, network_idle=True, timeout=60000)
                if self.proxy:
                    kw["proxy"] = self.proxy
                fetched = StealthyFetcher.fetch(url, **kw)
            except Exception as exc:
                self.notes.append(f"page{page} 抓取异常: {exc}")
                break
            status = getattr(fetched, "status", None)
            if status != 200:
                self.notes.append(
                    f"page{page} HTTP {status}"
                    + ("（被拦截——需住宅代理）" if status == 403 else ""))
                break
            data = self._next_data(fetched.html_content)
            page_reviews = self._extract(data)
            if not page_reviews:
                break
            reviews.extend(page_reviews)
        self.notes.append(f"采集 {len(reviews)} 条评论")
        return reviews

    def _next_data(self, html: str) -> dict:
        m = _ND_RE.search(html or "")
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

    def _extract(self, data: dict) -> list[dict]:
        """从 __NEXT_DATA__ 定位 reviews 列表并标准化。"""
        raw = self._find_reviews(data)
        out = []
        for r in raw or []:
            if not isinstance(r, dict):
                continue
            consumer = r.get("consumer") or {}
            dates = r.get("dates") or {}
            reply = r.get("reply") or {}
            labels = r.get("labels") or {}
            verification = (labels.get("verification") or {})
            rid = r.get("id") or r.get("reviewId")
            if not rid:
                continue
            out.append({
                "review_id": str(rid),
                "platform": "trustpilot",
                "site": self.site,
                "reviewer_name": consumer.get("displayName"),
                "reviewer_country": consumer.get("countryCode"),
                "rating": r.get("stars") or r.get("rating"),
                "title": r.get("title"),
                "content": r.get("text"),
                "language": r.get("language"),
                "review_date": dates.get("publishedDate"),
                "purchase_date": dates.get("experiencedDate"),
                "reply_content": reply.get("message"),
                "reply_date": reply.get("publishedDate"),
                "is_verified": bool(verification.get("isVerified")),
                "review_topics": r.get("labels", {}).get("merged")
                or r.get("tags"),
            })
        return out

    @staticmethod
    def _find_reviews(obj, depth: int = 0):
        """递归在 __NEXT_DATA__ 里找 reviews 数组。"""
        if depth > 8 or obj is None:
            return None
        if isinstance(obj, dict):
            rv = obj.get("reviews")
            if isinstance(rv, list) and rv and isinstance(rv[0], dict) \
                    and ("text" in rv[0] or "stars" in rv[0]):
                return rv
            for v in obj.values():
                res = TrustpilotCrawler._find_reviews(v, depth + 1)
                if res:
                    return res
        elif isinstance(obj, list):
            for v in obj[:20]:
                res = TrustpilotCrawler._find_reviews(v, depth + 1)
                if res:
                    return res
        return None
