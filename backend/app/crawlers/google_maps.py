"""Google Maps 商家评论采集器 —— 模块二（规格 F2-002）。

Google Maps 评论反爬极强，是全项目最难目标。本采集器用 Scrapling
StealthyFetcher 渲染商家页 + 滚动加载评论面板再解析。

注意：
  · 须配住宅代理（proxies.txt [residential]）—— Google 对数据中心 IP 极敏感
  · Google Maps DOM 类名经常变；生产环境建议叠加开源专用工具
    gosom/google-maps-scraper（Go，自带 REST API）作为兜底
  · 当前为「armed」状态：代理到位后可用，DOM 选择器可能需按实际微调
"""
from __future__ import annotations

import re
import urllib.parse

from ..proxy import get_proxy

_REVIEW_RE = re.compile(r'data-review-id="([^"]+)"')


class GoogleMapsCrawler:
    platform = "google_map"

    def __init__(self, channel: dict, max_reviews: int = 200):
        self.channel = channel
        self.query = channel["query"]            # 商家名，如 "Aosom LLC"
        self.site = channel["site"]
        self.max_reviews = channel.get("max_reviews", max_reviews)
        self.proxy = get_proxy("residential")
        self.notes: list[str] = []

    def crawl(self) -> list[dict]:
        try:
            from scrapling.fetchers import StealthyFetcher
        except Exception as exc:
            self.notes.append(f"Scrapling 未安装: {exc}")
            return []

        # 进入商家页（搜索结果首条），按"评论最新"排序
        url = ("https://www.google.com/maps/search/"
               + urllib.parse.quote(self.query))

        def scroll_reviews(page):
            """滚动评论面板加载更多。"""
            try:
                page.wait_for_timeout(2500)
                for _ in range(min(self.max_reviews // 10, 25)):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(900)
            except Exception:
                pass
            return page

        try:
            kw = dict(headless=True, network_idle=False, timeout=70000,
                      page_action=scroll_reviews)
            if self.proxy:
                kw["proxy"] = self.proxy
            fetched = StealthyFetcher.fetch(url, **kw)
        except Exception as exc:
            self.notes.append(f"抓取异常: {exc}")
            return []

        if getattr(fetched, "status", None) != 200:
            self.notes.append(f"HTTP {fetched.status}"
                              + ("（被拦截——需住宅代理）"
                                 if fetched.status == 403 else ""))
            return []

        reviews = self._extract(fetched)
        self.notes.append(f"采集 {len(reviews)} 条评论")
        return reviews

    def _extract(self, page) -> list[dict]:
        """从渲染后的页面解析评论卡片。"""
        reviews = []
        try:
            cards = page.css('[data-review-id]')
        except Exception:
            cards = []
        seen = set()
        for c in cards:
            rid = c.attrib.get("data-review-id") if hasattr(c, "attrib") else None
            if not rid or rid in seen:
                continue
            seen.add(rid)
            txt = ""
            try:
                node = c.css_first('.wiI7pd, .MyEned')
                txt = node.text if node else ""
            except Exception:
                pass
            name = ""
            try:
                nn = c.css_first('.d4r55, .TSUbDb')
                name = nn.text if nn else ""
            except Exception:
                pass
            stars = None
            try:
                sn = c.css_first('[role="img"][aria-label*="star"], '
                                 '[aria-label*="星"]')
                if sn:
                    m = re.search(r"[\d.]+", sn.attrib.get("aria-label", ""))
                    stars = int(float(m.group())) if m else None
            except Exception:
                pass
            reviews.append({
                "review_id": rid, "platform": "google_map", "site": self.site,
                "reviewer_name": name or None, "rating": stars,
                "content": txt or None,
            })
        return reviews[: self.max_reviews]
