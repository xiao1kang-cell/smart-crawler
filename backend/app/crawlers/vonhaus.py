"""VonHaus 采集器 —— 杜木 Domu，Magento 站。

VonHaus 的 sitemap.xml 把分类页和商品页混在一起（都是 /vh_en/<slug>），
商品页没有 Product JSON-LD 但有干净的 OpenGraph 商品 meta。
策略：顺序扫描 sitemap URL，逐页判断——是商品就解析，是分类就跳过，
直到收集够 limit 个商品（带扫描上限保护）。
"""
from __future__ import annotations

import os
import re

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("VONHAUS_LIMIT", "150"))
SCAN_CAP = int(os.environ.get("VONHAUS_SCAN_CAP", "900"))
_PRICE_RE = re.compile(r"[\d.]+")


class VonHausCrawler(BaseCrawler):
    platform = "vonhaus"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua()})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()
        try:
            xml = sess.get(self.base + "/sitemap.xml", timeout=30).text
        except Exception as exc:
            result.notes.append(f"⚠ sitemap 不可达: {exc}")
            return result

        urls = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
                if "/vh_en/" in u and u.rstrip("/") != self.base + "/vh_en"]
        result.notes.append(f"sitemap 共 {len(urls)} 个 /vh_en/ 页面，"
                            f"扫描判别商品（上限 {SCAN_CAP}）")

        scanned = 0
        for url in urls[:SCAN_CAP]:
            if len(result.products) >= self.limit:
                break
            scanned += 1
            try:
                html = sess.get(url, timeout=30).text
                row = self._parse_product(html, url)
                if row:
                    self.snapshot(url.rstrip("/").split("/")[-1], html)
                    result.products.append(row)
            except Exception:
                pass
            self.sleep()

        result.notes.append(
            f"扫描 {scanned} 页，命中商品 {len(result.products)} 个")
        return result

    def _parse_product(self, html: str, url: str) -> dict | None:
        tree = HTMLParser(html)
        price = self._meta(tree, "product:price:amount")
        if price is None:                       # 无商品价格 meta → 分类页
            return None
        title = self._meta(tree, "og:title")
        h1 = tree.css_first("h1")
        if h1 and h1.text(strip=True):
            title = h1.text(strip=True)
        if not title:
            return None

        currency = self._meta_raw(tree, "product:price:currency") or "GBP"
        avail = (self._meta_raw(tree, "product:availability")
                 or self._meta_raw(tree, "og:availability") or "").lower()
        image = self._meta_raw(tree, "og:image")
        slug = url.rstrip("/").split("/")[-1]
        # sku：优先 data-product-id，退化为 slug
        pid = tree.css_first("[data-product-id]")
        sku = (pid.attributes.get("data-product-id") if pid else None) or slug

        return {
            "sku": str(sku), "spu": str(sku),
            "title": title,
            "description": self._meta_raw(tree, "og:description"),
            "image_urls": [image] if image else [],
            "category_path": self._breadcrumb(tree),
            "sale_price": price,
            "original_price": price,
            "currency": currency,
            "status": "out_of_stock" if ("outofstock" in avail
                                         or "out of stock" in avail)
            else "on_sale",
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    @staticmethod
    def _meta_raw(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    def _meta(self, tree: HTMLParser, prop: str):
        v = self._meta_raw(tree, prop)
        if not v:
            return None
        m = _PRICE_RE.search(v.replace(",", ""))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None

    @staticmethod
    def _breadcrumb(tree: HTMLParser) -> str | None:
        crumbs = [n.text(strip=True) for n in
                  tree.css('.breadcrumbs a, [class*=breadcrumb] a')]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "")]
        return "/".join(crumbs[:3]) or None
