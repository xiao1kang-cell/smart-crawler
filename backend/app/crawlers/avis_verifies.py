"""Avis Vérifiés (法国头部评论 SaaS) 评论采集器。

Avis Vérifiés 是法语电商最大的评论认证平台，覆盖 6w+ 商家。
特点：
- 商家页 URL: https://www.avis-verifies.com/avis-clients/{merchant_slug}.html
- 评论页 URL: ?page=N
- 反爬：中低，Cloudflare 但 curl_cffi 大部分可过
- 评论在页面 HTML 中（不是 API），需 selectolax 解析

输出对齐 Review 模型字段。
"""
from __future__ import annotations

import re
from datetime import datetime

from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult
from ..models import Site

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_RATING_RE = re.compile(r"([\d.]+)\s*/\s*5")


class AvisVerifiesCrawler(BaseCrawler):
    """通用 Avis Vérifiés 评论抓取（按 merchant_slug 抓评论）。"""

    platform = "avis_verifies"

    def __init__(self, channel: dict, max_pages: int = 20):
        """channel: {site, merchant_slug, country=FR, host, max_pages}"""
        # 从 channel 合成 Site，供 BaseCrawler 使用
        site = Site(
            site=channel.get("site") or f"avis_{channel.get('merchant_slug', 'unknown')}",
            url="https://www.avis-verifies.com",
            country=channel.get("country", "FR"),
            platform="avis_verifies",
            proxy_tier="residential",
        )
        super().__init__(site)
        self.channel = channel
        self.merchant_slug = channel.get("merchant_slug") or channel.get("slug")
        self.host = channel.get("host", "www.avis-verifies.com")
        self.country = channel.get("country", "FR")
        self.max_pages = channel.get("max_pages", max_pages)
        self.notes: list[str] = []

    def _headers(self) -> dict:
        return {
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
            "User-Agent": self.ua(),
        }

    def crawl(self) -> list[dict]:                   # type: ignore[override]
        """返回标准化的评论 dict 列表（review_runner 直接调用此接口）。"""
        if not self.merchant_slug:
            self.notes.append(f"⚠ 缺 merchant_slug（{self.channel}）")
            return []

        reviews: list[dict] = []
        fetcher = self.make_fetcher(kind="product", source="avis_verifies")

        for page in range(1, self.max_pages + 1):
            url = (
                f"https://{self.host}/avis-clients/{self.merchant_slug}.html"
                f"?page={page}"
            )
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30)
            except Exception as exc:
                self.notes.append(f"page{page} 异常: {exc}")
                break

            if (res.status or 0) != 200:
                self.notes.append(f"page{page} HTTP {res.status or 0}")
                if (res.status or 0) in (403, 451):
                    # 尝试 stealth fallback
                    stealth_html = self._fetch_via_stealth(url)
                    if stealth_html:
                        page_reviews = self._parse_page(stealth_html)
                        if page_reviews:
                            reviews.extend(page_reviews)
                            continue
                break

            page_reviews = self._parse_page(res.text)
            if not page_reviews:
                self.notes.append(f"page{page} 无评论 → 抓取结束")
                break

            reviews.extend(page_reviews)

        self.notes.append(
            f"AvisVérifiés {self.merchant_slug}: 抓 {len(reviews)} 条"
        )
        return reviews

    def _parse_page(self, html: str) -> list[dict]:
        """解析一页评论。Avis Vérifiés 用 [itemprop="review"] 微数据标记。"""
        out: list[dict] = []
        tree = HTMLParser(html)
        for el in tree.css('[itemprop="review"]'):
            review = self._parse_review(el)
            if review:
                out.append(review)
        # 如果没有 itemprop=review，尝试 .review-card 类
        if not out:
            for el in tree.css('.review-card, .avis-card, .review-item'):
                review = self._parse_review(el)
                if review:
                    out.append(review)
        return out

    def _parse_review(self, el) -> dict | None:
        # 1) 评论 ID
        rid = el.attributes.get("data-review-id") or el.attributes.get("id")

        # 2) 内容（itemprop=reviewBody 或 .comment）
        content_node = (el.css_first('[itemprop="reviewBody"]')
                        or el.css_first(".comment")
                        or el.css_first(".review-body"))
        content = content_node.text(strip=True) if content_node else None
        if not content:
            return None

        # 3) 评分（itemprop=ratingValue 或 .rating）
        rating_node = (el.css_first('[itemprop="ratingValue"]')
                       or el.css_first(".rating"))
        rating = None
        if rating_node:
            raw = (rating_node.attributes.get("content")
                   or rating_node.text(strip=True))
            try:
                rating = float(raw.replace(",", "."))
            except (ValueError, AttributeError):
                m = _RATING_RE.search(raw or "")
                if m:
                    try:
                        rating = float(m.group(1))
                    except ValueError:
                        pass

        # 4) 日期（itemprop=datePublished 或 .date）
        date_node = (el.css_first('[itemprop="datePublished"]')
                     or el.css_first(".date"))
        review_date = None
        if date_node:
            raw_date = (date_node.attributes.get("datetime")
                        or date_node.attributes.get("content")
                        or date_node.text(strip=True))
            if raw_date:
                try:
                    review_date = datetime.fromisoformat(
                        raw_date.replace("Z", "+00:00")
                    ).date()
                except Exception:
                    m = _DATE_RE.search(raw_date)
                    if m:
                        try:
                            review_date = datetime(
                                int(m.group(3)),
                                int(m.group(2)),
                                int(m.group(1)),
                            ).date()
                        except Exception:
                            pass

        # 5) 作者（itemprop=author）
        author_node = (el.css_first('[itemprop="author"]')
                       or el.css_first(".author"))
        author = author_node.text(strip=True) if author_node else None

        # 6) 标题
        title_node = (el.css_first('[itemprop="name"]')
                      or el.css_first(".review-title"))
        title = title_node.text(strip=True) if title_node else None

        return {
            "review_id": str(rid or f"{self.merchant_slug}_{hash(content)}"),
            "platform": "avis_verifies",
            "site": self.channel.get("site") or f"avis_{self.merchant_slug}",
            "reviewer_name": author,
            "reviewer_country": self.country,
            "rating": rating,
            "title": title,
            "content": content,
            "language": "fr",
            "review_date": review_date,
            "purchase_date": None,
            "reply_content": None,
            "reply_date": None,
            "is_verified": True,
            "sku": None,
            "product_url": None,
        }

    def _fetch_via_stealth(self, url: str) -> str | None:
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.country,
                persist_profile_key=f"avis_{self.merchant_slug}",
                timeout_ms=45000,
            )
            page = self.count_browser_fetch(
                lambda: StealthyFetcher.fetch(url, **kw),
                success=lambda p: getattr(p, "status", None) == 200,
            )
            if page is not None and getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            pass
        return None
