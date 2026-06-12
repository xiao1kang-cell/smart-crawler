"""Magento 采集器 —— 覆盖 Costway 欧洲站、VonHaus 等 Magento 架构站点。

Magento 站特征：sitemap 把分类页和商品页混在一起，商品页带 JSON-LD Product
（或 OpenGraph product meta）。本采集器：
  1. 从 robots.txt 发现 sitemap（Magento 常在 /media/sitemap/ 下，非默认路径）
  2. 递归展开 sitemap 索引，收集全部页面 URL
  3. 并发抓取页面、按 JSON-LD/OG 判别商品 —— 并发是关键，因为要扫大量分类页才
     凑够商品数；顺序逐页（旧 VonHaus 做法）会慢到生产任务超时

sites.yaml 可选字段：
  sitemap:        指定 sitemap 入口（跳过自动发现）
  product_match:  商品 URL 必含子串（配了能大幅减少要扫的页数）
  max_products:   单次抓取上限（默认 200）
  scan_cap:       最多扫描多少个候选页（默认 1500）
"""
from __future__ import annotations

import gzip
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from ..config import get_sites
from .base import BaseCrawler, CrawlResult
from .generic import GenericCrawler

DEFAULT_LIMIT = int(os.environ.get("MAGENTO_LIMIT", "200"))
DEFAULT_SCAN_CAP = int(os.environ.get("MAGENTO_SCAN_CAP", "2800"))
WORKERS = int(os.environ.get("MAGENTO_WORKERS", "8"))
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "IE": "EUR", "DE": "EUR",
             "IT": "EUR", "ES": "EUR", "FR": "EUR", "RO": "RON", "PT": "EUR",
             "NL": "EUR", "PL": "PLN"}
_SKIP_RE = re.compile(
    r"(blog|/article|/news|/help|/about|/contact|/customer|/checkout|"
    r"/catalogsearch|/privacy|/terms|\.(jpg|png|webp|pdf|css|js)(\?|$))", re.I)


class MagentoCrawler(BaseCrawler):
    platform = "magento"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.base = site.url.rstrip("/")
        self.sitemap_hint = hints.get("sitemap")
        self.product_match = hints.get("product_match", "")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        self.scan_cap = int(hints.get("scan_cap", DEFAULT_SCAN_CAP))

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua(),
                          "Accept-Language": "en-US,en;q=0.9"})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def _discover_sitemap(self, sess: creq.Session) -> str | None:
        """从 robots.txt 找 Sitemap，再退化到 Magento 常见路径。"""
        try:
            rb = sess.get(self.base + "/robots.txt", timeout=20).text
            m = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", rb)
            if m:
                return m[0].strip()
        except Exception:
            pass
        for p in ("/media/sitemap/sitemap.xml", "/sitemap.xml",
                  "/pub/media/sitemap.xml", "/sitemap/sitemap.xml"):
            try:
                r = sess.get(self.base + p, timeout=20)
                if r.status_code == 200 and "<loc>" in r.text:
                    return self.base + p
            except Exception:
                continue
        return None

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

        sitemap = self.sitemap_hint or self._discover_sitemap(sess)
        if not sitemap:
            result.notes.append("⚠ 未发现 sitemap —— 无法采集")
            return result
        result.notes.append(f"sitemap: {sitemap}")

        locs = self._sitemap_locs(sess, sitemap)
        cands = [u for u in locs
                 if not u.endswith((".xml", ".xml.gz"))
                 and not _SKIP_RE.search(u)
                 and (not self.product_match or self.product_match in u)]
        # 去重；打散 —— Magento sitemap 把分类、商品分在不同子图，顺序扫会
        # 先撞上整段分类页。随机打散后命中率≈商品占比，分批扫即可提前停。
        seen_u: set[str] = set()
        cands = [u for u in cands if not (u in seen_u or seen_u.add(u))]
        total = len(cands)
        random.shuffle(cands)
        cands = cands[: self.scan_cap]
        result.notes.append(
            f"sitemap 候选页 {total} 个，打散后扫描上限 {len(cands)}，"
            f"目标商品 {self.limit}")
        if not cands:
            result.notes.append("⚠ 候选页为空")
            return result

        # 并发抓取 + 判别 —— 分批，凑够 limit 即停，不空跑剩余批次
        hit = 0
        scanned = 0
        batch = WORKERS * 6
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i in range(0, len(cands), batch):
                for row in pool.map(self._fetch_one, cands[i:i + batch]):
                    scanned += 1
                    if row:
                        result.products.append(row)
                        hit += 1
                if hit >= self.limit:
                    break
        result.notes.append(f"扫描 {scanned} 页，命中商品 {hit} 个")
        return result

    def _fetch_one(self, url: str) -> dict | None:
        """抓单页并判别 —— 是商品返回 row，否则 None。"""
        try:
            resp = creq.get(url, impersonate="chrome", timeout=25,
                            proxies=({"http": self.proxy, "https": self.proxy}
                                     if self.proxy else None))
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        html = resp.text
        data = GenericCrawler._from_jsonld(html) or {}
        tree = HTMLParser(html)

        title = data.get("name") or self._meta(tree, "og:title")
        sale = data.get("price")
        if sale is None:
            sale = self._og_price(tree)
        if not title or sale is None:        # 无商品价格 → 分类/内容页
            return None

        self.snapshot(url.rstrip("/").split("/")[-1][:80], html)
        original = data.get("original_price") or sale
        imgs = data.get("images") or (
            [self._meta(tree, "og:image")] if self._meta(tree, "og:image") else [])
        slug = url.rstrip("/").split("/")[-1].split("?")[0][:80]
        return {
            "sku": data.get("sku") or slug,
            "spu": data.get("sku") or slug,
            "title": title.strip(),
            "description": data.get("description")
            or self._meta(tree, "og:description"),
            "image_urls": imgs,
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
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    def _og_price(self, tree: HTMLParser):
        for sel in ('meta[property="product:price:amount"]',
                    'meta[property="og:price:amount"]',
                    '[itemprop="price"]'):
            node = tree.css_first(sel)
            if node:
                val = node.attributes.get("content") or node.text(strip=True)
                p = GenericCrawler._num(val)
                if p:
                    return p
        return None
