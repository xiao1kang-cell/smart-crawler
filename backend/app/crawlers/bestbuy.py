"""Best Buy 采集器 —— US 电子产品零售第一。Akamai Bot Manager（无 PX）。

反爬等级：3/5
  - curl_cffi（impersonate=chrome131）直连 PDP 通常 200
  - SRP 翻页 ~10 页/IP 后开始触发 Akamai
  - PDP 单 IP 100+ 才被慢慢限流
  - Datacenter 代理 + 真 chrome 指纹够用

数据发现：
  - SRP `/site/searchpage.jsp?st=<keyword>&_dyncharset=UTF-8&id=pcat17071&type=page&sc=Global&cp=N`
  - 商品 URL: `/site/<slug>/<sku>.p?skuId=<sku>`
  - sitemap_categoryProductSearch.xml.gz 公开

PDP 解析：
  - <script id="schemaOrgWebPage" type="application/ld+json"> 内嵌 Product schema
  - 字段：name / sku / image[] / brand.name / offers.price / offers.priceCurrency /
    aggregateRating / description
"""
from __future__ import annotations

import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("BESTBUY_LIMIT", "1000"))
DELAY = float(os.environ.get("BESTBUY_DELAY", "5.0"))
MAX_PAGES_PER_KW = int(os.environ.get("BESTBUY_MAX_PAGES_PER_KW", "6"))

_HOME_KW = [
    "laptop", "tv", "headphones", "tablet", "smartwatch", "monitor",
    "speaker", "camera", "printer", "router", "soundbar", "gaming chair",
    "office desk", "kitchen mixer", "vacuum cleaner",
]

_PDP_RE = re.compile(r'/site/[a-z0-9\-./]+/(\d{6,9})\.p\?skuId=\d+',
                     re.IGNORECASE)
# 2026-05-25 实测：SRP HTML 用 Next.js JSON，skuId 在 JSON 字符串里
_JSON_SKU_RE = re.compile(r'"skuId":\s*"(\d{6,9})"')
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_BLOCK_MARKS = (
    "Access Denied",
    "captcha-delivery",
    "blocked.html",
    "Akamai-Reference",
)


class BestBuyCrawler(BaseCrawler):
    platform = "bestbuy"

    def __init__(self, site, limit=None):
        super().__init__(site)
        self.base = "https://www.bestbuy.com"
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
        fetcher = self.make_fetcher(kind="product", source="bestbuy")
        urls: list[str] = []
        seen: set[str] = set()

        # Warmup：访问首页建立会话 / 预热 Akamai cookie（计入 api_calls）
        try:
            fetcher.get(self.base + "/", headers=self._headers(), timeout=20,
                        impersonate="chrome131")
        except Exception:
            pass

        for kw in _HOME_KW:
            if len(urls) >= self.limit * 2:
                break
            for pg in range(1, MAX_PAGES_PER_KW + 1):
                u = (f"{self.base}/site/searchpage.jsp?"
                     f"st={kw.replace(' ', '+')}&cp={pg}&intl=nosplash")
                try:
                    res = fetcher.get(u, headers=self._headers(), timeout=30,
                                      impersonate="chrome131")
                except Exception:
                    break
                if (res.status or 0) != 200 or self._blocked(res.text):
                    time.sleep(40)
                    break
                new = 0
                # 同时尝试新格式（JSON skuId）和旧格式（URL .p?skuId=）
                skus_found = set(_JSON_SKU_RE.findall(res.text))
                skus_found.update(_PDP_RE.findall(res.text))
                for sku in skus_found:
                    pdp = f"{self.base}/site/sku/{sku}.p?skuId={sku}"
                    if pdp in seen:
                        continue
                    seen.add(pdp)
                    urls.append(pdp)
                    new += 1
                if new < 5:
                    break
                self.sleep()
            result.notes.append(f"  kw={kw} 累计 {len(urls)} PDP")

        if not urls:
            result.notes.append("⚠ Best Buy SRP 失败")
            return result

        ok = denied = streak = 0
        for i, url in enumerate(urls[: self.limit * 2]):
            if ok >= self.limit:
                break
            # session rotate per 50 items：由 ProxyMiddleware 每请求轮换代理替代，
            # CrawlerFetcher 每次 _request_once 已建立新 Session，无需手动 rotate。
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30,
                                  impersonate="chrome131")
                html = res.text or ""
            except Exception:
                self.sleep()
                continue
            if (res.status or 0) in (403, 429) or self._blocked(html):
                denied += 1
                streak += 1
                if streak >= 6:
                    raise BlockedError(f"bestbuy 熔断 ok={ok}")
                time.sleep(60 * streak)
                continue
            streak = 0
            row = self._parse(html, url)
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
        if len(html) < 30_000:
            return any(m in html for m in _BLOCK_MARKS)
        return False

    def _parse(self, html: str, url: str) -> dict | None:
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
                    return self._row(node, url)
        return None

    def _row(self, doc: dict, url: str) -> dict:
        offers = doc.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _num(offers.get("price") if isinstance(offers, dict) else None)
        currency = (offers.get("priceCurrency")
                    if isinstance(offers, dict) else None) or "USD"
        avail = (str(offers.get("availability", "")).lower()
                 if isinstance(offers, dict) else "")
        imgs = doc.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        imgs = imgs or []
        brand = doc.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        rating = doc.get("aggregateRating") or {}
        m_sku = re.search(r"skuId=(\d+)", url)
        sku = (m_sku.group(1) if m_sku else None) or doc.get("sku") \
            or url.split("/")[-1]

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": doc.get("name"),
            "description": doc.get("description"),
            "image_urls": imgs,
            "category_path": None,
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
