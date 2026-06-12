"""Shoper 平台采集器 —— costway.pl 在用的波兰本土电商系统。

Shoper 站的几个特征跟其他平台不同：
1. **没有 sitemap.xml** —— robots.txt 不列，常见路径都返回 soft 404
   （1.3MB 的波兰语 "404 Nie znaleziono strony" HTML）。
2. **URL 结构** —— 商品页是顶层 slug，如 /3-stopniowa-skladana-drabina；
   类别页也是顶层 slug，如 /dom、/meble-do-domu。
3. **JSON-LD @type** 是完整 URL `http://schema.org/Product`，不是简写。
4. 主页 / 类别页 HTML 含全部子类别 + 全部商品 slug。

采集策略：**类别页发现**
- 从主页解析顶层 menu，拿到类别 slug 列表
- 进每个类别页，抓所有 root-level slug（非 /assets、非 /pl/、非已知类别）
- 启发式过滤：slug 中含连字符且不是已知页面类型 → 视为商品
- 抓商品页 → 解析 JSON-LD（兼容完整 schema URL）
"""
from __future__ import annotations

import json
import os
import re

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from ..config import get_sites
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("SHOPER_LIMIT", "200"))
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_PRICE_RE = re.compile(r"[\d.,]+")

# 顶层非商品 slug（系统页 / 帮助 / 营销）
_NON_PRODUCT_SLUGS = {
    "pl", "assets", "userdata", "search", "login", "register", "cart",
    "checkout", "account", "wishlist", "favorites", "contact", "kontakt",
    "help", "pomoc", "about", "o-nas", "shipping", "wysylka-i-dostawa",
    "returns", "zwroty", "privacy", "polityka-prywatnosci", "terms",
    "regulamin", "blog", "news", "promotions", "promocje",
    "program-lojalnosciowy", "loyalty", "newsletter", "rss", "sitemap",
}


class ShoperCrawler(BaseCrawler):
    platform = "shoper"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        # 用户可在 sites.yaml 显式指定类别 URL；否则自动从主页发现
        self.category_urls: list[str] = hints.get("category_urls") or []

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        })
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # 1. 拿类别列表
        if not self.category_urls:
            self.category_urls = self._discover_categories(sess)
            result.notes.append(
                f"自动发现 {len(self.category_urls)} 个类别（主页 menu）")
        else:
            result.notes.append(
                f"使用配置的 {len(self.category_urls)} 个类别")

        if not self.category_urls:
            raise RuntimeError(
                "未发现任何类别 URL —— 站点结构变化，需手动在 sites.yaml "
                "配置 category_urls")

        # 2. 从类别页抓商品 slug
        product_urls = self._collect_product_urls(sess, self.category_urls)
        result.notes.append(
            f"从类别页收集 {len(product_urls)} 个商品候选 URL")

        targets = product_urls[: self.limit]
        if not targets:
            raise RuntimeError(
                "类别页未发现任何商品 slug —— 检查 _NON_PRODUCT_SLUGS 是否需扩充")

        # 3. 抓商品页
        ok = 0
        for url in targets:
            try:
                row = self._parse_product(sess, url)
                if row:
                    result.products.append(row)
                    ok += 1
            except Exception as exc:
                result.notes.append(f"跳过 {url[-50:]}: {exc}")
            self.sleep()
        result.notes.append(f"成功解析 {ok}/{len(targets)} 个商品页")
        return result

    # ---------- 类别发现 ----------
    def _discover_categories(self, sess: creq.Session) -> list[str]:
        try:
            r = sess.get(self.base + "/", timeout=20)
        except Exception:
            return []
        if r.status_code != 200:
            return []
        # 找主菜单 ul 里的链接 —— Shoper 模板通常有 .menu / nav
        candidates: list[str] = []
        for menu_pat in (
            r'<ul[^>]*class="[^"]*menu[^"]*"[^>]*>(.*?)</ul>',
            r'<nav[^>]*>(.*?)</nav>',
        ):
            for m in re.finditer(menu_pat, r.text, re.S | re.I):
                hrefs = re.findall(r'href=["\'](/[^"\']+)["\']', m.group(1))
                candidates.extend(hrefs)
        # 过滤：顶层 slug、非 _NON_PRODUCT_SLUGS、非资源
        seen: set[str] = set()
        out: list[str] = []
        for h in candidates:
            slug = h.lstrip("/").split("/")[0].split("?")[0]
            if not slug or slug in _NON_PRODUCT_SLUGS:
                continue
            if slug.startswith("assets") or slug.endswith((".css", ".js",
                                                            ".svg", ".png")):
                continue
            path = "/" + slug
            if path in seen:
                continue
            seen.add(path)
            out.append(self.base + path)
        return out

    def _collect_product_urls(self, sess: creq.Session,
                               cat_urls: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        known_categories = {u.replace(self.base, "") for u in cat_urls}
        for cat_url in cat_urls:
            try:
                r = sess.get(cat_url, timeout=30)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            hrefs = re.findall(r'href=["\'](/[^"\']+)["\']', r.text)
            for h in hrefs:
                slug = h.lstrip("/").split("/")[0].split("?")[0]
                if not slug or slug in _NON_PRODUCT_SLUGS:
                    continue
                if slug.startswith("assets") or "." in slug:
                    continue
                path = "/" + slug
                if path in known_categories:
                    continue
                # 商品 slug 一般含连字符（描述性 SEO slug）
                if "-" not in slug:
                    continue
                full = self.base + path
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                if len(out) >= self.limit * 2:        # 留点缓冲
                    return out
            self.sleep()
        return out

    # ---------- 商品解析 ----------
    def _parse_product(self, sess: creq.Session, url: str) -> dict | None:
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            return None
        html = r.text
        self.snapshot(url.rstrip("/").split("/")[-1][:80], html)
        data = self._from_jsonld(html)
        if not data or not data.get("name"):
            return None
        tree = HTMLParser(html)
        return {
            "sku": data.get("sku") or url.rstrip("/").split("/")[-1][:80],
            "spu": data.get("sku"),
            "title": (data.get("name") or "").strip(),
            "description": data.get("description")
            or self._meta(tree, "og:description"),
            "image_urls": data.get("images") or [],
            "category_path": data.get("category"),
            "sale_price": data.get("price"),
            "original_price": data.get("original_price") or data.get("price"),
            "currency": data.get("currency") or "PLN",
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
        """Shoper 把一个商品拆成多个 JSON-LD block —— 都用同一个 `@id`
        但各自只携带一部分字段（block#5 只是个 @type=Product 的 stub，
        而 name/price/image/sku 分别在 #4/#6/#7/#8/#11 里）。
        所以这里先扫所有 block，按 @id 合并同一商品的字段，再返回。"""
        merged: dict = {}
        product_id: str | None = None

        # 1) 先找含 @type=Product 的 block，记录它的 @id
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            for it in (doc if isinstance(doc, list) else
                       doc.get("@graph", [doc]) if isinstance(doc, dict)
                       else []):
                if not isinstance(it, dict):
                    continue
                if _is_product_type(it.get("@type")) and it.get("@id"):
                    product_id = it["@id"]
                    break
            if product_id:
                break
        if not product_id:
            return None

        # 2) 扫所有 block，凡 @id 匹配的都合并字段（deep-merge offers / dict）
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            for it in (doc if isinstance(doc, list) else
                       doc.get("@graph", [doc]) if isinstance(doc, dict)
                       else []):
                if not isinstance(it, dict) or it.get("@id") != product_id:
                    continue
                for k, v in it.items():
                    if k in ("@context", "@id", "@type"):
                        continue
                    if v in (None, "", []):
                        continue
                    if k == "offers" and isinstance(merged.get("offers"), dict) \
                            and isinstance(v, dict):
                        merged["offers"] = {**merged["offers"], **v}
                    elif k not in merged or not merged[k]:
                        merged[k] = v

        if not merged.get("name"):
            return None

        offers = merged.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        rating = merged.get("aggregateRating") or {}
        brand = merged.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        imgs = merged.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        avail = str(offers.get("availability", "")).lower()
        price = ShoperCrawler._num(offers.get("price"))
        orig_price = ShoperCrawler._num(
            offers.get("highPrice") or offers.get("listPrice"))
        return {
            "name": merged.get("name"),
            "sku": merged.get("sku") or merged.get("mpn"),
            "description": merged.get("description"),
            "images": imgs or [],
            "price": price,
            "original_price": orig_price,
            "currency": offers.get("priceCurrency"),
            "status": "out_of_stock" if "outofstock" in avail
            or "soldout" in avail else "on_sale",
            "rating": ShoperCrawler._num(rating.get("ratingValue")),
            "review_count": ShoperCrawler._int(rating.get("reviewCount")),
            "mpn": merged.get("mpn"),
            "gtin": merged.get("gtin13") or merged.get("gtin"),
            "brand": brand,
        }

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

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


def _is_product_type(t) -> bool:
    """兼容简写 'Product' 和完整 URL 'http://schema.org/Product'。"""
    if isinstance(t, str):
        return t == "Product" or t.endswith("/Product")
    if isinstance(t, list):
        return any(_is_product_type(x) for x in t)
    return False
