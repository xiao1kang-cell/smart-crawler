"""VonHaus 采集器 —— 杜木 Domu，Magento 站。

VonHaus 的 sitemap.xml 把分类页和商品页混在一起（都是 /vh_en/<slug>），
商品页没有 Product JSON-LD 但有干净的 OpenGraph 商品 meta。
策略：顺序扫描 sitemap URL，逐页判断——是商品就解析，是分类就跳过，
默认扫描完整 sitemap；VONHAUS_LIMIT / VONHAUS_SCAN_CAP 仅用于显式调试。
"""
from __future__ import annotations

import os
import re

from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("VONHAUS_LIMIT", "999999"))
SCAN_CAP = int(os.environ.get("VONHAUS_SCAN_CAP", "0"))
_PRICE_RE = re.compile(r"[\d.]+")


class VonHausCrawler(BaseCrawler):
    platform = "vonhaus"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)
        config = site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw_delay = config.get("rate_interval_sec") or os.environ.get(
            "VONHAUS_RATE_INTERVAL_SEC")
        if raw_delay not in (None, ""):
            try:
                self.delay = max(0.0, min(float(raw_delay), 2.0))
            except (TypeError, ValueError):
                pass

    def _headers(self) -> dict:
        return {"User-Agent": self.ua()}

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="sitemap", source="vonhaus")
        try:
            res = fetcher.get(
                self.base + "/sitemap.xml",
                headers=self._headers(),
                timeout=30,
            )
            xml = res.text
        except Exception as exc:
            result.notes.append(f"⚠ sitemap 不可达: {exc}")
            return result

        if not (res.ok and xml):
            result.notes.append(f"⚠ sitemap 返回 {res.status or 0}")
            return result

        urls = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
                if "/vh_en/" in u and u.rstrip("/") != self.base + "/vh_en"]
        result.notes.append(f"sitemap 共 {len(urls)} 个 /vh_en/ 页面，"
                            f"扫描判别商品（上限 {SCAN_CAP}）")
        scan_urls = urls[:SCAN_CAP] if SCAN_CAP > 0 else urls
        if len(scan_urls) < len(urls):
            result.coverage_complete = False
            result.coverage_code = "incomplete_discovery"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"VonHaus sitemap 扫描被 VONHAUS_SCAN_CAP 截断："
                f"{len(scan_urls)}/{len(urls)}"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 VONHAUS_SCAN_CAP 后重跑。"

        prod_fetcher = self.make_fetcher(kind="product", source="vonhaus")
        scanned = 0
        stopped_by_limit = False
        discovered_products = 0
        self.persist_job_progress(products_count=0)
        for url in scan_urls:
            scanned += 1
            try:
                res = prod_fetcher.get(url, headers=self._headers(), timeout=30)
                html = res.text or ""
                row = self._parse_product(html, url)
                if row:
                    discovered_products += 1
                    if len(result.products) < self.limit:
                        self.snapshot(url.rstrip("/").split("/")[-1], html)
                        result.products.append(row)
                        if len(result.products) % 50 == 0:
                            self.persist_job_progress(
                                products_count=len(result.products),
                                total_product_count=discovered_products,
                            )
                    else:
                        stopped_by_limit = True
            except Exception:
                pass
            self.sleep()
        self.persist_job_progress(
            products_count=len(result.products),
            total_product_count=discovered_products,
        )

        result.notes.append(
            f"扫描 {scanned} 页，发现商品 {discovered_products} 个，"
            f"本次入库 {len(result.products)} 个")
        if stopped_by_limit:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            result.coverage_reason = (
                f"VonHaus 本次发现 {discovered_products} 个商品，"
                f"实际入库 {len(result.products)} 个，已被 VONHAUS_LIMIT 截断"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 VONHAUS_LIMIT 后重跑。"
        result.total_product_count = discovered_products
        return result

    def _parse_product(self, html: str, url: str) -> dict | None:
        tree = HTMLParser(html)
        price = self._meta(tree, "product:price:amount")
        if price is None:                       # 无商品价格 meta → 分类页
            return None
        title = self._meta_raw(tree, "og:title")
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
