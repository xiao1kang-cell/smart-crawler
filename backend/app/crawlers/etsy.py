"""Etsy.com 采集器 —— 手作 / Unique products marketplace。

反爬等级：2/5 —— 比 Amazon / Walmart 软很多。
- 公开 sitemap: https://www.etsy.com/sitemap.xml → sitemap-listing-1.xml 等
- 直接 curl_cffi (impersonate=chrome131) 多能拿 200
- 商品页内嵌 JSON-LD Product + Offer，字段全
- SRP /search?q=<kw>&explicit=1&page=N 翻页稳定

PDP 解析：
  - <script type="application/ld+json"> 内嵌完整 Product schema
  - 字段：name / description / image[] / brand.name / offers.price /
    offers.priceCurrency / offers.availability / aggregateRating /
    brand 通常是 "Etsy" 不准确，用 storeName 来源更好

策略：SRP-first（家居关键词）+ JSON-LD 解析。
"""
from __future__ import annotations

import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("ETSY_LIMIT", "1000"))
DELAY = float(os.environ.get("ETSY_DELAY", "3.0"))
MAX_PAGES_PER_KW = int(os.environ.get("ETSY_MAX_PAGES_PER_KW", "6"))
MAX_ELAPSED_SEC = float(os.environ.get("ETSY_MAX_ELAPSED_SEC", "180"))

_HOME_KW = [
    "wall art print", "throw pillow cover", "table lamp", "wooden coaster",
    "ceramic mug", "macrame wall hanging", "knife block", "wooden tray",
    "candle holder", "wall mirror", "planter pot", "kitchen apron",
    "shower curtain", "duvet cover", "decorative bowl",
]

_LISTING_RE = re.compile(
    r'/listing/(\d{6,12})/[a-zA-Z0-9_-]+')
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_BLOCK_MARKS = (
    "captcha-delivery",
    "access denied",
    "rate.limit",
    "robot or human",
)


class EtsyCrawler(BaseCrawler):
    platform = "etsy"

    def __init__(self, site, limit=None):
        super().__init__(site)
        self.base = "https://www.etsy.com"
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.delay = max(self.delay, DELAY)

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base + "/",
            "Sec-Fetch-Mode": "navigate",
        }

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        fetcher = self.make_fetcher(kind="product", source="etsy")
        urls: list[str] = []
        seen: set[str] = set()

        # SRP 阶段
        for kw in _HOME_KW:
            if time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 ETSY_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回（发现 {len(urls)} 个 listing）")
                break
            if len(urls) >= self.limit * 2:
                break
            for pg in range(1, MAX_PAGES_PER_KW + 1):
                if time.monotonic() - started >= MAX_ELAPSED_SEC:
                    break
                u = (f"{self.base}/search?q={kw.replace(' ', '+')}"
                     f"&page={pg}")
                try:
                    res = fetcher.get(u, headers=self._headers(), timeout=30)
                except Exception:
                    break
                if (res.status or 0) in (403, 429) or self._blocked(res.text):
                    result.notes.append(
                        f"⚠ Etsy SRP 被拦截 status={res.status or 0} kw={kw}")
                    return result
                if (res.status or 0) != 200:
                    break
                new = 0
                for lid in _LISTING_RE.findall(res.text):
                    pdp = f"{self.base}/listing/{lid}"
                    if pdp in seen:
                        continue
                    seen.add(pdp)
                    urls.append(pdp)
                    new += 1
                if new < 5:
                    break
                self.sleep()
            result.notes.append(f"  kw={kw} 累计 {len(urls)} listings")

        if not urls:
            result.notes.append("⚠ Etsy SRP 失败 —— 检查 IP / 代理")
            return result

        # PDP 阶段
        ok = denied = 0
        for i, url in enumerate(urls[: self.limit * 2]):
            if time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 ETSY_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回已解析结果（ok={ok}, denied={denied}）")
                break
            if ok >= self.limit:
                break
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30)
                html = res.text or ""
            except Exception:
                self.sleep()
                continue
            if (res.status or 0) in (403, 429) or self._blocked(html):
                denied += 1
                if denied >= 6:
                    raise BlockedError(f"etsy 熔断 ok={ok}")
                time.sleep(30)
                continue
            row = self._parse_jsonld(html, url)
            if row:
                self.snapshot(row["sku"], html)
                result.products.append(row)
                ok += 1
            self.sleep()

        result.notes.append(f"成功 {ok} · 拦截 {denied}")
        return result

    @staticmethod
    def _blocked(html: str) -> bool:
        if not html:
            return True
        if len(html) < 20_000:
            return any(m in html.lower() for m in _BLOCK_MARKS)
        return False

    def _parse_jsonld(self, html: str, url: str) -> dict | None:
        product_doc = None
        breadcrumbs: list[str] = []
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            nodes = (doc if isinstance(doc, list)
                     else doc.get("@graph", [doc]) if isinstance(doc, dict)
                     else [])
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    product_doc = product_doc or node
                elif t == "BreadcrumbList":
                    for el in (node.get("itemListElement") or []):
                        if isinstance(el, dict):
                            name = (el.get("name")
                                    or (el.get("item") or {}).get("name"))
                            if name and name.lower() not in (
                                    "home", "etsy", ""):
                                breadcrumbs.append(name)
        if not product_doc:
            return None

        m_id = re.search(r"/listing/(\d+)", url)
        listing_id = m_id.group(1) if m_id else None

        offers = product_doc.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _num(offers.get("price") or offers.get("lowPrice"))
        currency = (offers.get("priceCurrency")
                    if isinstance(offers, dict) else None) or "USD"

        imgs = product_doc.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]

        brand = product_doc.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        rating = product_doc.get("aggregateRating") or {}

        avail = ""
        if isinstance(offers, dict):
            avail = str(offers.get("availability", "")).lower()

        return {
            "sku": str(listing_id) if listing_id else url.split("/")[-1],
            "spu": str(listing_id) if listing_id else None,
            "title": product_doc.get("name"),
            "description": product_doc.get("description"),
            "image_urls": imgs,
            "category_path": "/".join(breadcrumbs[:3]) or None,
            "sale_price": price,
            "original_price": price,
            "currency": currency,
            "ratings": _num(rating.get("ratingValue")),
            "review_count": _int(rating.get("reviewCount")
                                 or rating.get("ratingCount")),
            "status": "out_of_stock" if "outofstock" in avail else "on_sale",
            "brand": brand or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }


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
