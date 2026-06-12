"""通用采集器 —— 覆盖无专用采集器的站点（Flexispot / VonHaus / Woltu / Vidaxl 等）。

策略：sitemap 发现商品 URL → 逐页多策略解析：
  1. JSON-LD <script type="application/ld+json"> 的 Product schema
  2. OpenGraph + 微数据（og:title / product:price:amount / itemprop="price"）
  3. 站内 dataLayer JSON 兜底

sites.yaml 中该站点可选字段：
  sitemap:        sitemap 入口（默认 {url}/sitemap.xml）
  product_match:  商品 URL 必含子串（如 "/p/"）
  max_products:   单次抓取上限（默认 GENERIC_LIMIT）
"""
from __future__ import annotations

import gzip
import json
import os
import re

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from ..config import get_sites
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("GENERIC_LIMIT", "200"))
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "IE": "EUR", "DE": "EUR",
             "IT": "EUR", "ES": "EUR", "FR": "EUR", "RO": "RON", "PT": "EUR",
             "NL": "EUR", "PL": "PLN"}
_PRICE_RE = re.compile(r"[\d.,]+")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class GenericCrawler(BaseCrawler):
    platform = "generic"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.sitemap = hints.get("sitemap") or (
            site.url.rstrip("/") + "/sitemap.xml")
        self.product_match = hints.get("product_match", "")
        self.exclude_match = hints.get("exclude_match", "")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua(),
                          "Accept-Language": "en-US,en;q=0.9"})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def _sitemap_locs(self, sess: creq.Session, url: str, depth: int = 0) -> list[str]:
        """递归展开 sitemap（索引 / .gz / 普通），返回全部 <loc>。"""
        if depth > 3:
            return []
        try:
            raw = sess.get(url, timeout=30).content
        except Exception:
            return []
        try:
            text = (gzip.decompress(raw) if url.endswith(".gz")
                    else raw).decode("utf-8", "ignore")
        except (OSError, gzip.BadGzipFile):
            text = raw.decode("utf-8", "ignore")
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
        sub = [l for l in locs if l.endswith(".xml") or l.endswith(".xml.gz")]
        if sub and len(sub) == len(locs):            # 纯 sitemap 索引，递归
            out: list[str] = []
            for s in sub[:12]:
                out.extend(self._sitemap_locs(sess, s, depth + 1))
            return out
        return locs

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        locs = self._sitemap_locs(sess, self.sitemap)
        products = [l for l in locs
                    if (not self.product_match or self.product_match in l)
                    and (not self.exclude_match or self.exclude_match not in l)
                    and not l.endswith((".xml", ".xml.gz"))]
        # 去掉明显非商品页
        products = [u for u in products if not re.search(
            r"(blog|/article|/news|care-center|/category/|/help|/about|/contact)",
            u)]
        total = len(products)
        targets = products[: self.limit]
        result.notes.append(
            f"sitemap 发现 {total} 个候选商品 URL，本次抓取 {len(targets)} 条")
        if not targets:
            result.notes.append("⚠ sitemap 未发现商品 URL，需为该站点配置 "
                                 "sitemap / product_match")
            return result

        ok = 0
        for url in targets:
            try:
                row = self._parse(sess, url)
                if row:
                    result.products.append(row)
                    ok += 1
            except Exception as exc:
                result.notes.append(f"跳过 {url[:60]}: {exc}")
            self.sleep()
        result.notes.append(f"成功解析 {ok}/{len(targets)} 个商品页")
        return result

    def _parse(self, sess: creq.Session, url: str) -> dict | None:
        html = sess.get(url, timeout=30).text
        self.snapshot(self._slug(url), html)       # 原始商品页归档
        tree = HTMLParser(html)
        data = self._from_jsonld(html) or {}

        title = data.get("name") or self._meta(tree, "og:title")
        if not title:
            return None
        sale = data.get("price") or self._meta_price(tree)
        if sale is None:
            return None
        original = data.get("original_price") or sale

        return {
            "sku": data.get("sku") or self._slug(url),
            "spu": data.get("sku") or self._slug(url),
            "title": (title or "").strip(),
            "description": data.get("description")
            or self._meta(tree, "og:description"),
            "image_urls": data.get("images")
            or ([self._meta(tree, "og:image")] if self._meta(tree, "og:image") else []),
            "category_path": data.get("category"),
            "sale_price": sale,
            "original_price": original,
            "currency": data.get("currency")
            or _CURRENCY.get(self.site.country, "USD"),
            "ratings": data.get("rating"),
            "review_count": data.get("review_count"),
            "status": data.get("status", "on_sale"),
            "has_video": "<video" in html,
            "mpn": data.get("mpn"),
            "gtin": data.get("gtin"),
            "brand": data.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    @staticmethod
    def _from_jsonld(html: str) -> dict | None:
        """解析 JSON-LD 的 Product schema。"""
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            for it in (doc if isinstance(doc, list) else
                       doc.get("@graph", [doc]) if isinstance(doc, dict) else []):
                if not isinstance(it, dict):
                    continue
                t = it.get("@type")
                is_product = t == "Product" or (
                    isinstance(t, list) and "Product" in t)
                if not is_product:
                    continue
                offers = it.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                rating = it.get("aggregateRating") or {}
                brand = it.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                avail = str(offers.get("availability", "")).lower()
                imgs = it.get("image")
                if isinstance(imgs, str):
                    imgs = [imgs]
                return {
                    "name": it.get("name"),
                    "sku": it.get("sku") or it.get("mpn"),
                    "description": it.get("description"),
                    "images": imgs or [],
                    "price": GenericCrawler._num(offers.get("price")),
                    "currency": offers.get("priceCurrency"),
                    "status": "out_of_stock" if "outofstock" in avail
                    or "soldout" in avail else "on_sale",
                    "rating": GenericCrawler._num(rating.get("ratingValue")),
                    "review_count": GenericCrawler._int(rating.get("reviewCount")),
                    "mpn": it.get("mpn"),
                    "gtin": it.get("gtin13") or it.get("gtin"),
                    "brand": brand,
                }
        return None

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    def _meta_price(self, tree: HTMLParser):
        for sel in ('meta[property="product:price:amount"]',
                    'meta[property="og:price:amount"]',
                    '[itemprop="price"]'):
            node = tree.css_first(sel)
            if node:
                val = node.attributes.get("content") or node.text(strip=True)
                p = self._num(val)
                if p:
                    return p
        return None

    @staticmethod
    def _slug(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0][:80]

    @staticmethod
    def _num(v):
        if v is None:
            return None
        m = _PRICE_RE.search(str(v).replace(",", "."))
        if not m:
            return None
        try:
            return float(m.group())
        except ValueError:
            return None

    @staticmethod
    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
