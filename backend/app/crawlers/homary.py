"""Homary 采集器 —— 爆米科技，Nuxt.js SSR 站点。

策略：
  1. 拉 sitemap 索引 → item 子 sitemap，得到全部商品 URL（即 SKU 总量）
  2. 商品页是 SSR 全渲染 HTML，从 <meta> + DOM 解析基础字段
     （__NUXT__ 是 (function(){...}) 形式无法当 JSON 解析，故走 HTML）
  3. best_sellers 子 sitemap 用于打热销标签

注：全量 4000+ 商品逐页抓约 2 小时，MVP 默认抓 HOMARY_LIMIT 条做演示，
   全量可作为定时任务夜间运行。
"""
from __future__ import annotations

import gzip
import os
import re

from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult

_ID_RE = re.compile(r"-(\d+)\.html")
_PRICE_RE = re.compile(r"[\d,]+\.?\d*")
DEFAULT_LIMIT = int(os.environ.get("HOMARY_LIMIT", "150"))

_CURRENCY = {"US": "USD", "UK": "GBP", "DE": "EUR", "ES": "EUR", "FR": "EUR"}


class HomaryCrawler(BaseCrawler):
    platform = "nuxt"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.cc = site.country.lower()

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {"User-Agent": self.ua()}

    def _sitemap_urls(self, fetcher, kind: str) -> list[str]:
        """取某类 sitemap 的全部 <loc>。kind: item / best_sellers。"""
        base = self.site.url.rstrip("/")
        url = f"{base}/sitemaps/google_sitemap_{kind}_{self.cc}.xml.gz"
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=30)
            raw = res.content
            try:
                xml = gzip.decompress(raw).decode("utf-8", "ignore")
            except (OSError, gzip.BadGzipFile):
                xml = raw.decode("utf-8", "ignore")
            return re.findall(r"<loc>(.*?)</loc>", xml)
        except Exception:
            return []

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="homary")

        item_urls = [u for u in self._sitemap_urls(fetcher, "item") if "/item/" in u]
        best_ids = {m.group(1) for u in self._sitemap_urls(fetcher, "best_sellers")
                    if (m := _ID_RE.search(u))}
        total = len(item_urls)
        targets = item_urls[: self.limit]
        result.notes.append(
            f"sitemap 共 {total} 商品，本次抓取 {len(targets)} 条"
            f"（HOMARY_LIMIT={self.limit}）；热销 {len(best_ids)} 款")

        for url in targets:
            try:
                row = self._parse_product(fetcher, url, best_ids)
                if row:
                    result.products.append(row)
            except Exception as exc:                # 单页失败不影响整体
                result.notes.append(f"跳过 {url}: {exc}")
            self.sleep()
        return result

    def _parse_product(self, fetcher, url: str, best_ids: set) -> dict | None:
        m = _ID_RE.search(url)
        if not m:
            return None
        pid = m.group(1)
        res = fetcher.get(url, headers=self._headers(), timeout=30)
        html = res.text or ""
        self.snapshot(pid, html)                   # 原始商品页归档
        tree = HTMLParser(html)

        title = self._meta(tree, "og:title") or ""
        title = re.split(r"[｜|]", title)[0].strip()
        description = self._meta(tree, "og:description")
        image = self._meta(tree, "og:image")

        prices = tree.css(".price")
        sale = self._to_price(prices[0].text(strip=True)) if prices else None
        # 原价：找带删除线 / origin 的价格元素
        original = None
        for sel in (".origin-price", ".market-price", "del", ".product-price del"):
            node = tree.css_first(sel)
            if node:
                original = self._to_price(node.text(strip=True))
                if original:
                    break
        if not original:
            original = sale

        crumbs = [n.text(strip=True) for n in tree.css('[class*=breadcrumb] a')]
        crumbs = [c for c in crumbs if c and c.lower() != "home"]
        # 去重保序
        seen, path = set(), []
        for c in crumbs:
            if c not in seen:
                seen.add(c)
                path.append(c)
        category_path = "/".join(path[:3]) or None

        images = []
        for img in tree.css("img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and "su-cdn.com" in src and src not in images:
                images.append(src)
        if image and image not in images:
            images.insert(0, image)

        out_of_stock = bool(re.search(r"out of stock|sold out", html, re.I))

        return {
            "sku": pid,
            "spu": pid,
            "title": title,
            "description": description,
            "image_urls": images[:10] or ([image] if image else []),
            "category_path": category_path,
            "sale_price": sale,
            "original_price": original,
            "currency": _CURRENCY.get(self.site.country, "USD"),
            "status": "out_of_stock" if out_of_stock else "on_sale",
            "has_video": "<video" in html,
            "label": "BEST SELLER" if pid in best_ids else None,
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
            "is_bestseller": pid in best_ids,
        }

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    @staticmethod
    def _to_price(text: str | None):
        """智能价格解析 —— 自适应欧式 / 美式数字格式。

        欧式: `94,99 €` (= €94.99) / `9.999,99 €` (= €9999.99)
        美式: `$94.99` / `$9,999.99`
        规则: 同时含 `,` 和 `.` → 取最右一个为小数点；
              仅含 `,` 且后跟 ≤ 2 位 → 视作小数点；否则千分位。
        """
        if not text:
            return None
        import re as _re
        m = _re.search(r"[\d.,]+", str(text))
        if not m:
            return None
        s = m.group()
        if "," in s and "." in s:
            # 最右的分隔符是小数点
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")    # 欧式 9.999,99
            else:
                s = s.replace(",", "")                       # 美式 9,999.99
        elif "," in s:
            tail = s.rsplit(",", 1)[-1]
            if len(tail) <= 2:
                s = s.replace(",", ".")                       # 欧式 94,99
            else:
                s = s.replace(",", "")                        # 美式 94,995
        try:
            return float(s)
        except ValueError:
            return None
