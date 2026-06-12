"""Article.com 采集器 —— 独立家居 DTC 站（US/CA），自研栈 + GraphQL。

实地验证（2026-05-24）：
- ✅ `https://www.article.com/product_sitemap.xml` 公开可达，约 1437 条
  `/product/<id>/<slug>` URL（sitemap 数据为 2022 年快照，URL 多已 301）。
- ✅ 产品 PDP 服务端 SSR 渲染：HTML 内嵌 `<script type="application/ld+json">`
  Product schema（含 name / sku / mpn / description / image / brand / category /
  aggregateRating / dimensions），价格不在 JSON-LD，落在 DOM
  `<span class="newPrice">$249</span><span class="originalPrice">$299</span>`
  或 `<span class="regularPrice">$159</span>`。
- ✅ 反爬等级 1：curl_cffi 直连 200，无 challenge，无验证码，无 rate limit
  迹象。CloudFront 边缘缓存层 cache-control: max-age=36, swr=300，对爬虫友好。
- ⚠ Sitemap 含大量 stale URL：旧 `/product/<id>/<slug>` 会先 301 到新 slug，
  再 301 到 `/browse`（商品停售）。需识别终态 URL：
    - 终态仍是 `/product/<id>/...` → 在售或缺货商品（解析）
    - 终态是 `/browse` 或 `/browse/...` → discontinued（跳过）
- ✅ Cookie `currency=1` = USD（默认）, `currency=2` = CAD。本采集器走 US 站。

策略：
  1. 拉 sitemap → 1437 候选 URL
  2. 逐 URL GET（允许 30x），观察终态：
     - 若终态 `/product/` → JSON-LD 取元数据 + DOM 取价格
     - 若终态 `/browse` → 计入 discontinued 跳过统计
  3. 价格解析：优先 `.newPrice` + `.originalPrice`，退化 `.regularPrice`
  4. status：JSON-LD 含 offers.availability 时优先；无 offers 时基于价格存在性
     判断（有价格 → on_sale）；终态 /browse → discontinued 不入库

env：ARTICLE_LIMIT 默认 1000
"""
from __future__ import annotations

import json
import os
import re

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("ARTICLE_LIMIT", "1000"))
SITEMAP_URL = "https://www.article.com/product_sitemap.xml"

_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_PRICE_RE = re.compile(r"[\d,]+\.?\d*")
_PRODUCT_URL_RE = re.compile(r"/product/(\d+)/([a-z0-9-]+)")
_CURRENCY = {"US": "USD", "CA": "CAD"}


class ArticleCrawler(BaseCrawler):
    platform = "article"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        # US=1 / CA=2，默认 US
        self._cur_cookie = "2" if (site.country or "").upper() == "CA" else "1"

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        s.cookies.set("currency", self._cur_cookie, domain=".article.com")
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # ---- Step 1：拉 sitemap ----
        try:
            resp = sess.get(SITEMAP_URL, timeout=30)
            self.guard(resp.status_code, "sitemap")
        except Exception as exc:
            result.notes.append(f"⚠ sitemap 不可达: {exc}")
            return result
        if resp.status_code != 200:
            result.notes.append(
                f"⚠ sitemap HTTP {resp.status_code}")
            return result

        urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", resp.text)
        product_urls = [u for u in urls if "/product/" in u]
        total = len(product_urls)
        targets = product_urls[: self.limit]
        result.notes.append(
            f"sitemap 共 {total} 个 /product/ URL，本次抓取 {len(targets)} 条"
            f"（ARTICLE_LIMIT={self.limit}）")

        # ---- Step 2：逐 URL 抓取 ----
        ok = 0
        discontinued = 0
        skipped = 0
        for url in targets:
            try:
                row, status = self._fetch_and_parse(sess, url)
                if status == "discontinued":
                    discontinued += 1
                elif row:
                    result.products.append(row)
                    ok += 1
                else:
                    skipped += 1
            except Exception as exc:                # 单页失败不影响整体
                skipped += 1
                if skipped <= 5:                    # 只记录前 5 条异常，免噪音
                    result.notes.append(f"跳过 {url[-60:]}: {exc}")
            self.sleep()

        result.notes.append(
            f"成功解析 {ok}/{len(targets)} | 停售 {discontinued} | 跳过 {skipped}")
        return result

    # ------------------------------------------------------------------
    # 单页解析
    # ------------------------------------------------------------------
    def _fetch_and_parse(
            self, sess: creq.Session, url: str) -> tuple[dict | None, str]:
        """返回 (row, status)。status: 'ok' / 'discontinued' / 'noparse'。"""
        resp = sess.get(url, timeout=30, allow_redirects=True)
        self.guard(resp.status_code, "pdp")
        final_url = str(resp.url)

        # 终态 /browse → 停售
        if "/product/" not in final_url:
            return None, "discontinued"

        html = resp.text
        m = _PRODUCT_URL_RE.search(final_url)
        slug = m.group(2) if m else final_url.rstrip("/").split("/")[-1]
        self.snapshot(slug, html)

        return self._parse(html, final_url), "ok"

    def _parse(self, html: str, url: str) -> dict | None:
        tree = HTMLParser(html)
        ld = self._from_jsonld(html) or {}

        # JSON-LD 是 Product schema 的强信号 —— 无 LD name 直接放弃
        title = (ld.get("name")
                 or self._meta(tree, "og:title")
                 or self._h1(tree))
        if not title:
            return None

        sku = ld.get("sku") or ld.get("mpn")
        # fallback：用 URL 中的数字 id
        if not sku:
            m = _PRODUCT_URL_RE.search(url)
            sku = m.group(1) if m else self._slug(url)

        sale, original = self._dom_prices(tree)
        # JSON-LD offers.price 兜底（Article 通常没填，但留逻辑）
        ld_price = self._num(ld.get("price"))
        if sale is None and ld_price is not None:
            sale = ld_price
        if original is None:
            original = sale

        # 图片：JSON-LD `image` 是单图，DOM 还有更多 cdn-images.article.com
        images = list(ld.get("images") or [])
        og_img = self._meta(tree, "og:image")
        if og_img and og_img not in images:
            images.insert(0, og_img)
        for img in tree.css("img"):
            src = (img.attributes.get("src")
                   or img.attributes.get("data-src") or "")
            if "cdn-images.article.com/products" in src and src not in images:
                images.append(src)
            if len(images) >= 10:
                break

        # 库存判断：JSON-LD availability > DOM 文案
        avail = (ld.get("availability") or "").lower()
        if "outofstock" in avail or "soldout" in avail:
            status = "out_of_stock"
        elif re.search(r"sold\s*out|out\s*of\s*stock|backorder",
                       html, re.I):
            status = "out_of_stock"
        elif sale is None:
            # 没价格也没明确缺货标签 → 视为暂时缺货
            status = "out_of_stock"
        else:
            status = "on_sale"

        # 分类：JSON-LD category 是文本（如 "Coffee & Accent Tables"），
        # 退化用 BreadcrumbList 的最后非叶节点
        category = ld.get("category") or self._breadcrumb_from_ld(html)

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title.strip(),
            "description": (ld.get("description")
                            or self._meta(tree, "og:description")),
            "image_urls": images[:10],
            "category_path": category,
            "sale_price": sale,
            "original_price": original,
            "currency": _CURRENCY.get(
                (self.site.country or "US").upper(), "USD"),
            "ratings": ld.get("rating"),
            "review_count": ld.get("review_count"),
            "status": status,
            "has_video": "<video" in html,
            "mpn": ld.get("mpn"),
            "brand": ld.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    # ------------------------------------------------------------------
    # 辅助 —— JSON-LD / DOM 价格 / 面包屑
    # ------------------------------------------------------------------
    @staticmethod
    def _from_jsonld(html: str) -> dict | None:
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            items = (doc if isinstance(doc, list)
                     else doc.get("@graph", [doc]) if isinstance(doc, dict)
                     else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type")
                is_product = (t == "Product"
                              or (isinstance(t, list) and "Product" in t))
                if not is_product:
                    continue
                offers = it.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                rating = it.get("aggregateRating") or {}
                brand = it.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                imgs = it.get("image")
                if isinstance(imgs, str):
                    imgs = [imgs]
                return {
                    "name": it.get("name"),
                    "sku": it.get("sku") or it.get("productID"),
                    "mpn": it.get("mpn"),
                    "description": it.get("description"),
                    "images": imgs or [],
                    "price": offers.get("price"),
                    "availability": offers.get("availability"),
                    "category": it.get("category"),
                    "rating": ArticleCrawler._num(rating.get("ratingValue")),
                    "review_count": ArticleCrawler._int(
                        rating.get("ratingCount") or rating.get("reviewCount")),
                    "brand": brand,
                }
        return None

    @staticmethod
    def _breadcrumb_from_ld(html: str) -> str | None:
        """从 BreadcrumbList JSON-LD 推分类路径（剥掉 Home / All Products / 商品自身）。"""
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("@type") != "BreadcrumbList":
                continue
            items = doc.get("itemListElement") or []
            names = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = it.get("name")
                if name and name.lower() not in ("home", "all products"):
                    names.append(name)
            if len(names) >= 2:
                names = names[:-1]                  # 去掉商品自身
            return "/".join(names[:3]) or None
        return None

    def _dom_prices(self, tree: HTMLParser):
        """返回 (sale, original)。

        Article 的价格 DOM 三种形态：
          1. 在售带原价：`.newPrice` + `.originalPrice`
          2. 仅原价（无折扣）：`.regularPrice`
          3. 缺货 / 停售：可能完全没有上述 DOM
        """
        sale = self._first_price(tree, ".newPrice")
        original = self._first_price(tree, ".originalPrice")
        if sale is None:
            sale = self._first_price(tree, ".regularPrice")
        return sale, (original or sale)

    @classmethod
    def _first_price(cls, tree: HTMLParser, sel: str):
        for node in tree.css(sel):
            v = cls._num(node.text(strip=True))
            if v:
                return v
        return None

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    @staticmethod
    def _h1(tree: HTMLParser) -> str | None:
        node = tree.css_first("h1")
        return node.text(strip=True) if node else None

    @staticmethod
    def _slug(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0][:80]

    @staticmethod
    def _num(v):
        if v is None:
            return None
        m = _PRICE_RE.search(str(v).replace(",", ""))
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
