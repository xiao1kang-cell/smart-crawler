"""TrustedShops 评论采集器 —— 欧洲（主要 DE）评论平台。

TrustedShops 是德国头部评论 SaaS，覆盖 30w+ 商家。
特点：
- 商家页 URL: https://www.trustedshops.com/buyerrating/info_{TS_ID}.html
- 评论 API（公开）: /buyerratings/{TS_ID}/_reviews?after_id=&start=0&count=50
- 反爬：Cloudflare 中级，curl_cffi 大部分可过；遇 403 切 StealthyFetcher

API 返回 JSON 结构（基于 2026-05-24 实测）：
{
  "reviews": [
    {"id":"...", "comment":"...", "mark":4.5, "createdDate":"2026-05-23T...",
     "anonymousAlias":"...", "reply":{"comment":"...","createdDate":"..."}},
    ...
  ],
  "remaining": int
}

输出对齐 Review 模型字段。

批C 收编（2026-06）：
  - 继承 BaseCrawler（从 channel 合成 Site，同 reviews_io 模式）
  - curl 段改用 make_fetcher().get()，自动计 api_calls
  - stealth 段用 count_browser_fetch 包裹，成功计 browser_opens
  - 删 proxy 自管(_session → _headers())，删 creq import
  - 构造签名 / crawl 返回类型不变（向后兼容 review_runner）
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .base import BaseCrawler
from ..models import Site


_TS_ID_RE = re.compile(r"info_([A-F0-9]{32})\.html")


class TrustedShopsCrawler(BaseCrawler):
    """通用 TrustedShops 评论抓取器（按 ts_id 抓某商家所有评论）。"""

    platform = "trustedshops"

    def __init__(self, channel: dict, max_pages: int = 20):
        """channel: {site, ts_id, domain, host, max_pages, country}"""
        # 从 channel 合成 Site，供 BaseCrawler 使用
        site = Site(
            site=channel.get("site") or "trustedshops",
            url=f"https://{channel.get('host', 'www.trustedshops.com')}",
            country=channel.get("country"),
            platform="trustedshops",
            proxy_tier="residential",
        )
        super().__init__(site)
        self.channel = channel
        self.ts_id = channel.get("ts_id") or self._extract_ts_id(
            channel.get("info_url", ""))
        self.host = channel.get("host", "www.trustedshops.com")
        self.country = channel.get("country", "DE")
        self.max_pages = channel.get("max_pages", max_pages)
        self.notes: list[str] = []

    @staticmethod
    def _extract_ts_id(url: str) -> str | None:
        m = _TS_ID_RE.search(url)
        return m.group(1) if m else None

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        }

    def crawl(self) -> list[dict]:                  # type: ignore[override]
        """返回标准化的评论 dict 列表（review_runner 直接调用此接口）。"""
        if not self.ts_id:
            self.notes.append(f"⚠ 缺 ts_id（{self.channel}）")
            return []

        reviews: list[dict] = []
        fetcher = self.make_fetcher(kind="product", source="trustedshops")
        start = 0
        page_size = 50

        for page in range(self.max_pages):
            url = (
                f"https://{self.host}/buyerratings/{self.ts_id}/_reviews"
                f"?start={start}&count={page_size}"
            )
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30)
            except Exception as exc:
                self.notes.append(f"page{page} fetch 异常: {exc}")
                # 试 stealth fallback
                stealth_text = self._fetch_via_stealth(url)
                if stealth_text:
                    try:
                        data = json.loads(stealth_text)
                    except Exception:
                        break
                else:
                    break
            else:
                if (res.status or 0) != 200:
                    self.notes.append(
                        f"page{page} HTTP {res.status or 0}"
                        + ("（403 → 试 stealth）" if (res.status or 0) == 403 else ""))
                    if (res.status or 0) in (403, 451):
                        stealth_text = self._fetch_via_stealth(url)
                        if not stealth_text:
                            break
                        try:
                            data = json.loads(stealth_text)
                        except Exception:
                            break
                    else:
                        break
                else:
                    try:
                        data = res.json() or {}
                    except Exception:
                        self.notes.append(f"page{page} JSON 解析失败")
                        break

            page_reviews = data.get("reviews", [])
            if not page_reviews:
                break

            for r in page_reviews:
                normalized = self._normalize(r)
                if normalized:
                    reviews.append(normalized)

            start += page_size
            if data.get("remaining", 0) <= 0:
                break

        self.notes.append(
            f"TrustedShops {self.ts_id}: 抓 {len(reviews)} 条评论"
        )
        return reviews

    def _normalize(self, r: dict) -> dict | None:
        """对齐 Review 模型字段。"""
        try:
            mark = r.get("mark")
            rating = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            rating = None

        created = r.get("createdDate") or r.get("created_at") or ""
        review_date = None
        if created:
            try:
                review_date = datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                ).date()
            except Exception:
                pass

        reply = r.get("reply") or {}
        return {
            "review_id": str(r.get("id") or r.get("review_id") or ""),
            "platform": "trustedshops",
            "site": self.channel.get("site") or f"trustedshops_{self.ts_id}",
            "reviewer_name": r.get("anonymousAlias") or r.get("anonymous_alias"),
            "reviewer_country": self.country,
            "rating": rating,
            "title": r.get("title"),
            "content": r.get("comment") or r.get("text"),
            "language": "de" if self.country == "DE" else "en",
            "review_date": review_date,
            "purchase_date": None,
            "reply_content": reply.get("comment"),
            "reply_date": None,
            "is_verified": True,
            "sku": None,
            "product_url": None,
        }

    def _fetch_via_stealth(self, url: str) -> str | None:
        """curl_cffi 失败时走 Scrapling StealthyFetcher fallback。

        批C：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，
        成功时自动 browser_opens += 1。stealth kw 参数 / persist_profile /
        profile 目录逻辑全部原样保留，只在最外层套计数。
        """
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.country,
                persist_profile_key=f"trustedshops_{self.ts_id}",
                timeout_ms=45000,
            )

            def _do_fetch():
                return StealthyFetcher.fetch(url, **kw)

            # 成功标准：status == 200（原 _fetch_via_stealth 判断）
            def _success(page) -> bool:
                return getattr(page, "status", None) == 200

            page = self.count_browser_fetch(_do_fetch, success=_success)
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            pass
        return None
