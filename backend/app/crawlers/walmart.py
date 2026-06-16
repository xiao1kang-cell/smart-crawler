"""Walmart.com 采集器 —— US #2 电商，Akamai Bot Manager + PerimeterX (HUMAN Sensor)。

反爬实测（2026-05-25 设计阶段，与 ebay/wayfair 同一武器库）：
  - curl_cffi（impersonate=chrome131）拉首页通常 200
  - PDP 单 IP 10-15 次后吃 412 / "Robot or human?" challenge
  - 住宅代理 + US 出口 必需；datacenter 段（包含 Cogent/Hurricane）全段拉黑
  - TLS / JA3 指纹检测，必须用 chrome131 高版本
  - SRP 翻页 > 8 次会触发 403

数据发现策略（SRP-first + sitemap 兜底）：
  - SRP: /search?q=<kw>&cat_id=4044&page=N，每页约 40 PDP
  - 家居谱关键词 × 大类 4044 = Home，扫到 ~600+ 唯一 PDP 后停
  - sitemap 兜底（无 cat 过滤）通过 env WALMART_USE_SITEMAP=1 启用

PDP 解析（首选 Next.js __NEXT_DATA__，JSON-LD 兜底）：
  - <script id="__NEXT_DATA__" type="application/json"> 内嵌完整商品 schema
  - 路径：props.pageProps.initialData.data.product
  - 字段全：name / usItemId / imageInfo.allImages / priceInfo.currentPrice.price /
    wasPrice / availabilityStatus / brand / category.path / numberOfReviews /
    averageRating / shortDescription

反爬等级：4/5（Akamai + PX，比 eBay 略严）。
"""
from __future__ import annotations

import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("WALMART_LIMIT", "1000"))
DELAY = float(os.environ.get("WALMART_DELAY", "6.0"))
USE_SITEMAP = os.environ.get("WALMART_USE_SITEMAP", "0") == "1"
MAX_PAGES_PER_KW = int(os.environ.get("WALMART_MAX_PAGES_PER_KW", "8"))

# 家居谱关键词 × cat_id=4044 (Home)
_HOME_KEYWORDS = [
    ("sofa", "4044"),
    ("dining table", "4044"),
    ("cookware set", "4044"),
    ("bedding", "4044"),
    ("patio chair", "4044"),
    ("area rug", "4044"),
    ("table lamp", "4044"),
    ("curtain", "4044"),
    ("wall mirror", "4044"),
    ("computer desk", "4044"),
    ("bookshelf", "4044"),
    ("blender", "4044"),
    ("knife set", "4044"),
    ("bath towel", "4044"),
    ("pillow", "4044"),
]

_IP_RE = re.compile(r'/ip/[^"\s?#]+/(\d{6,12})')
_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S)
_BLOCK_MARKS = (
    "Robot or human",
    "px-captcha",
    "captcha-delivery",
    "Access Denied",
    "blocked.html",
    "_Incapsula_Resource",
)


class WalmartCrawler(BaseCrawler):
    platform = "walmart"

    def __init__(self, site, limit=None):
        super().__init__(site)
        self.base = "https://www.walmart.com"
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.delay = max(self.delay, DELAY)

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base + "/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="walmart")
        urls: list[str] = []
        seen: set[str] = set()

        # ---------- Warmup：建立会话 cookie，计入 api_calls ----------
        try:
            fetcher.get(self.base + "/", headers=self._headers(),
                        timeout=20, impersonate="chrome131")
        except Exception:
            pass

        # ---------- SRP 阶段：多关键词 × 翻页 ----------
        for kw, cat in _HOME_KEYWORDS:
            if len(urls) >= self.limit * 2:
                break
            for pg in range(1, MAX_PAGES_PER_KW + 1):
                u = (f"{self.base}/search?q={kw.replace(' ', '+')}"
                     f"&cat_id={cat}&page={pg}")
                try:
                    res = fetcher.get(u, headers=self._headers(),
                                      timeout=30, impersonate="chrome131")
                except Exception:
                    break
                if (res.status or 0) != 200 or self._blocked(res.text):
                    result.notes.append(
                        f"  SRP {kw} p{pg} blocked (status {res.status or 0})")
                    time.sleep(45)
                    break
                new = 0
                for m in _IP_RE.findall(res.text):
                    pdp = f"{self.base}/ip/{m}"
                    if pdp in seen:
                        continue
                    seen.add(pdp)
                    urls.append(pdp)
                    new += 1
                if new < 10:
                    break
                self.sleep()
            result.notes.append(f"  kw={kw} 累计 {len(urls)} PDP")
            self.sleep()

        if not urls:
            result.notes.append("⚠ SRP 全程被拦，住宅代理无法穿透 Akamai")
            return result

        # ---------- PDP 阶段：解析 __NEXT_DATA__ ----------
        ok = denied = streak = 0
        for i, url in enumerate(urls[: self.limit * 2]):
            if ok >= self.limit:
                break
            try:
                res = fetcher.get(url, headers=self._headers(),
                                  timeout=30, impersonate="chrome131")
                html = res.text or ""
            except Exception:
                self.sleep()
                continue
            if (res.status or 0) in (412, 403, 429) or self._blocked(html):
                denied += 1
                streak += 1
                if streak >= 8:
                    raise BlockedError(f"walmart 熔断 ok={ok} denied={denied}")
                time.sleep(min(90 * streak, 600))
                continue
            streak = 0
            row = self._parse_next(html, url)
            if row:
                self.snapshot(row["sku"], html)
                result.products.append(row)
                ok += 1
            self.sleep()

        result.notes.append(f"成功 {ok} · 反爬 {denied} · 总扫描 {len(urls)}")
        return result

    @staticmethod
    def _blocked(html: str) -> bool:
        if not html:
            return True
        if len(html) < 30_000:
            return any(m in html for m in _BLOCK_MARKS)
        return False

    def _parse_next(self, html: str, url: str) -> dict | None:
        m = _NEXT_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            prod = (data.get("props", {}).get("pageProps", {})
                    .get("initialData", {}).get("data", {}).get("product"))
        except (json.JSONDecodeError, AttributeError):
            return None
        if not prod or not isinstance(prod, dict):
            return None

        item_id = prod.get("usItemId") or prod.get("id")
        price_info = prod.get("priceInfo") or {}
        cur_price = price_info.get("currentPrice") or {}
        was_price = price_info.get("wasPrice") or {}
        images = prod.get("imageInfo") or {}
        all_imgs = images.get("allImages") or []
        img_urls = [
            (im.get("url") if isinstance(im, dict) else im) for im in all_imgs
        ]
        img_urls = [u for u in img_urls if u]
        if not img_urls and images.get("thumbnailUrl"):
            img_urls = [images["thumbnailUrl"]]

        category = prod.get("category") or {}
        path = category.get("path") or []
        crumbs = [p.get("name") for p in path if isinstance(p, dict)]

        review_count = prod.get("numberOfReviews")
        avg_rating = prod.get("averageRating")

        avail = (prod.get("availabilityStatus") or "").lower()
        status = "out_of_stock" if "out" in avail else "on_sale"

        return {
            "sku": str(item_id) if item_id else url.split("/")[-1],
            "spu": str(item_id) if item_id else None,
            "title": prod.get("name"),
            "description": prod.get("shortDescription") or prod.get(
                "longDescription"),
            "image_urls": img_urls,
            "category_path": "/".join(crumbs) if crumbs else None,
            "sale_price": _num(cur_price.get("price")),
            "original_price": _num(was_price.get("price")
                                   or cur_price.get("price")),
            "currency": (cur_price.get("currencyUnit")
                         or cur_price.get("currencyUnitSymbol") or "USD"),
            "ratings": _num(avg_rating),
            "review_count": _int(review_count),
            "status": status,
            "brand": prod.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }


# ---------- 工具 ----------
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").strip()
    m = re.search(r"[\d.]+", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
