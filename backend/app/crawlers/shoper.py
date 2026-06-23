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
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from ..config import get_sites
from ..url_filters import is_obvious_non_product_url
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("SHOPER_LIMIT", "999999"))
DEFAULT_MAX_ELAPSED_SEC = int(os.environ.get("SHOPER_MAX_ELAPSED_SEC", "0"))
DEFAULT_CANDIDATE_CAP = int(os.environ.get("SHOPER_CANDIDATE_CAP", "0"))
DEFAULT_CATEGORY_PAGE_CAP = int(os.environ.get("SHOPER_CATEGORY_PAGE_CAP", "0"))
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_PRICE_RE = re.compile(r"[\d.,]+")

# 顶层非商品 slug（系统页 / 帮助 / 营销）
_NON_PRODUCT_SLUGS = {
    "pl", "assets", "environment", "userdata", "search", "login", "register",
    "cart", "checkout", "account", "wishlist", "favorites", "contact",
    "kontakt", "help", "pomoc", "about", "o-nas", "shipping",
    "wysylka-i-dostawa", "returns", "zwroty", "privacy",
    "polityka-prywatnosci", "terms", "regulamin", "blog", "news",
    "promotions", "promocje", "panel", "order",
    "program-lojalnosciowy", "loyalty", "newsletter", "rss", "sitemap",
    # costway.pl marketing/help landing pages confirmed from production
    # snapshots; they look like root-level SEO product slugs but have no PDP
    # JSON-LD and should not inflate the crawl denominator.
    "boze-narodzenie", "fit-w-nowym-roku", "home-office",
    "klasyczna-biel", "majowkowe-grillowanie", "mini-bar",
    "prawo-do-odstapienia-od-umowy", "prezenty-dla-malej-ksiezniczki",
    "prezenty-dla-malych-fanow-motoryzacji",
    "regulamin-program-lojalnosciowy", "styl-boho",
    "wakacje-w-ogrodzie", "wyprawa-za-miasto", "wyspy-kuchenne",
    "zimowy-czas",
}


class ShoperCrawler(BaseCrawler):
    platform = "shoper"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)
        self.max_elapsed_sec = DEFAULT_MAX_ELAPSED_SEC
        self.candidate_cap = (
            DEFAULT_CANDIDATE_CAP if DEFAULT_CANDIDATE_CAP > 0 else 0
        )
        self.category_page_cap = (
            DEFAULT_CATEGORY_PAGE_CAP if DEFAULT_CATEGORY_PAGE_CAP > 0 else 0
        )
        self._last_collect_stats: dict[str, object] = {}
        # 用户可在 sites.yaml 显式指定类别 URL；否则自动从主页发现
        self.category_urls: list[str] = hints.get("category_urls") or []

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        }

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        fetcher = self.make_fetcher(
            kind="product",
            source="shoper",
            fail_fast_blocked=True,
            retries=0,
        )

        # 1. 拿类别列表
        if not self.category_urls:
            self.category_urls = self._discover_categories(fetcher)
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
        collect_budget = int(self.max_elapsed_sec) if self.max_elapsed_sec > 0 else None
        product_urls = self._collect_product_urls(
            fetcher, self.category_urls, started, collect_budget)
        collect_stats = self._last_collect_stats or {}
        result.notes.append(
            f"从类别页收集 {len(product_urls)} 个商品候选 URL"
            f"（收集预算 {collect_budget or '不限'}s / "
            f"候选上限 {self.candidate_cap or '不限'} / "
            f"页面上限 {self.category_page_cap or '不限'} / "
            f"已访问页 {collect_stats.get('visited_pages', 0)} / "
            f"剩余队列 {collect_stats.get('queued_pages', 0)}）")
        if collect_stats.get("stopped_reason"):
            result.coverage_complete = False
            result.coverage_code = "incomplete_discovery"
            result.coverage_stage = "discovery"
            result.coverage_reason = (
                f"Shoper 商品 URL 发现提前停止："
                f"{collect_stats.get('stopped_reason')}"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = (
                "放宽 SHOPER_MAX_ELAPSED_SEC / SHOPER_CANDIDATE_CAP / "
                "SHOPER_CATEGORY_PAGE_CAP 后重跑。"
            )

        targets = product_urls[: self.limit]
        result.total_product_count = len(product_urls)
        if len(targets) < len(product_urls):
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            result.coverage_reason = (
                f"Shoper 本次全量分母 {len(product_urls)}，"
                f"实际计划抓取 {len(targets)}，已被 limit 截断"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 SHOPER_LIMIT 后重跑。"
        if not targets:
            raise RuntimeError(
                "类别页未发现任何商品 slug —— 检查 _NON_PRODUCT_SLUGS 是否需扩充")

        # 3. 抓商品页
        ok = 0
        for url in targets:
            if (self.max_elapsed_sec > 0
                    and self._elapsed(started) >= self.max_elapsed_sec):
                result.notes.append(
                    f"达到 Shoper 总耗时上限 {self.max_elapsed_sec}s，"
                    f"提前停止，已解析 {ok}/{len(targets)}")
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"Shoper 商品详情解析达到耗时上限，已解析 {ok}/{len(targets)}"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "放宽 SHOPER_MAX_ELAPSED_SEC 或拆分失败商品重抓。"
                )
                break
            try:
                row = self._parse_product(fetcher, url)
                if row:
                    result.products.append(row)
                    ok += 1
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(f"跳过 {url[-50:]}: {exc}")
            self.sleep()
        result.notes.append(f"成功解析 {ok}/{len(targets)} 个商品页")
        return result

    # ---------- 类别发现 ----------
    def _discover_categories(self, fetcher) -> list[str]:
        try:
            res = fetcher.get(self.base + "/", headers=self._headers(), timeout=12)
        except BlockedError:
            raise
        except Exception:
            return []
        if (res.status or 0) != 200:
            return []
        # costway.pl 首页会把完整类目树直接渲染出来。只看第一层 menu 会漏掉
        # 大量末级类目，进而只能拿到第一页的少量商品。
        candidates: list[str] = []
        for path in self._href_paths(res.text):
            if path.count("/") == 1:
                candidates.append(path)

        # 过滤：顶层 slug、非 _NON_PRODUCT_SLUGS、非资源。
        seen: set[str] = set()
        out: list[str] = []
        for h in candidates:
            slug = h.lstrip("/").split("/")[0]
            path = "/" + slug
            full = self.base + path
            if (not slug or slug in _NON_PRODUCT_SLUGS
                    or is_obvious_non_product_url(full)):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(full)
        return out

    def _collect_product_urls(self, fetcher,
                               cat_urls: list[str],
                               started: float,
                               collect_budget_sec: int | None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        known_categories = {
            self._category_base_path(u.replace(self.base, ""))
            for u in cat_urls
        }
        queue: deque[str] = deque(cat_urls)
        queued_page_keys = {
            self._pagination_key(self._normalize_path(u))
            for u in cat_urls
        }
        visited_page_keys: set[str] = set()
        stopped_reason: str | None = None
        while queue:
            cat_url = queue.popleft()
            if (collect_budget_sec is not None
                    and self._elapsed(started) >= collect_budget_sec):
                stopped_reason = f"达到收集预算 {collect_budget_sec}s"
                break
            page_path = self._normalize_path(cat_url)
            page_key = self._pagination_key(page_path)
            if not page_path or page_key in visited_page_keys:
                continue
            if (
                self.category_page_cap
                and len(visited_page_keys) >= self.category_page_cap
            ):
                stopped_reason = f"达到类别页上限 {self.category_page_cap}"
                break
            visited_page_keys.add(page_key)
            try:
                res = fetcher.get(cat_url, headers=self._headers(), timeout=12)
            except BlockedError:
                raise
            except Exception:
                continue
            if (res.status or 0) != 200:
                continue
            hrefs = self._href_paths(res.text)
            for path in hrefs:
                if self._is_pagination_path(path, known_categories):
                    full = self.base + path
                    key = self._pagination_key(path)
                    if (
                        key not in visited_page_keys
                        and key not in queued_page_keys
                    ):
                        queued_page_keys.add(key)
                        queue.append(full)

            product_paths = self._listing_product_paths(res.text)
            if not product_paths:
                product_paths = self._fallback_product_paths(hrefs, known_categories)
            for path in product_paths:
                full = self.base + path
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                if self.candidate_cap and len(out) >= self.candidate_cap:
                    self._last_collect_stats = {
                        "visited_pages": len(visited_page_keys),
                        "queued_pages": len(queue),
                        "stopped_reason": f"达到候选 URL 上限 {self.candidate_cap}",
                    }
                    return out
            self.sleep()
        self._last_collect_stats = {
            "visited_pages": len(visited_page_keys),
            "queued_pages": len(queue),
            "stopped_reason": stopped_reason,
        }
        return out

    def _listing_product_paths(self, html: str) -> list[str]:
        paths: list[str] = []
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict) or doc.get("@type") != "ItemList":
                continue
            for item in doc.get("itemListElement") or []:
                product = item.get("item") if isinstance(item, dict) else None
                offers = product.get("offers") if isinstance(product, dict) else None
                url = offers.get("url") if isinstance(offers, dict) else None
                path = self._product_path_from_url(url)
                if path:
                    paths.append(path)

        for match in re.finditer(r"<product-link\b.*?</product-link>", html, re.S):
            for href in re.findall(r'href=["\']([^"\']+)["\']', match.group(0)):
                path = self._product_path_from_url(href)
                if path:
                    paths.append(path)
        return list(dict.fromkeys(paths))

    def _product_path_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        path = self._normalize_path(urljoin(self.base + "/", url))
        if path.count("/") != 1:
            return None
        slug = path.lstrip("/")
        full = self.base + path
        if (not slug or slug in _NON_PRODUCT_SLUGS
                or is_obvious_non_product_url(full)):
            return None
        if slug.startswith("assets") or "." in slug or "-" not in slug:
            return None
        return path

    def _fallback_product_paths(
        self,
        hrefs: list[str],
        known_categories: set[str],
    ) -> list[str]:
        out: list[str] = []
        for path in hrefs:
            path = self._product_path_from_url(path)
            if not path or path in known_categories:
                continue
            out.append(path)
        return list(dict.fromkeys(out))

    def _href_paths(self, html: str) -> list[str]:
        out: list[str] = []
        base_host = urlsplit(self.base).netloc
        for href in re.findall(r'href=["\']([^"\']+)["\']', html):
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url = urljoin(self.base + "/", href)
            parsed = urlsplit(url)
            if parsed.netloc and parsed.netloc != base_host:
                continue
            path = parsed.path.rstrip("/") or "/"
            if not path.startswith("/"):
                continue
            out.append(path)
        return out

    def _normalize_path(self, url_or_path: str) -> str:
        if url_or_path.startswith("http"):
            return urlsplit(url_or_path).path.rstrip("/") or "/"
        return url_or_path.rstrip("/") or "/"

    @staticmethod
    def _category_base_path(path: str) -> str:
        parts = path.strip("/").split("/")
        return "/" + parts[0] if parts and parts[0] else "/"

    def _is_pagination_path(
        self,
        path: str,
        known_categories: set[str],
    ) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) < 2:
            return False
        base = "/" + parts[0]
        if base not in known_categories:
            return False
        if len(parts) == 2 and parts[1].isdigit():
            return True
        return (
            len(parts) == 4
            and parts[1].isdigit()
            and parts[2]
            and parts[3].isdigit()
        )

    @staticmethod
    def _pagination_key(path: str) -> str:
        parts = path.strip("/").split("/")
        if not parts or not parts[0]:
            return "/:1"
        base = "/" + parts[0]
        if len(parts) == 2 and parts[1].isdigit():
            return f"{base}:{int(parts[1])}"
        if len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit():
            return f"{base}:{int(parts[3])}"
        return f"{base}:1"

    # ---------- 商品解析 ----------
    def _parse_product(self, fetcher, url: str) -> dict | None:
        res = fetcher.get(url, headers=self._headers(), timeout=12)
        if not res.ok:
            return None
        html = res.text
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

    @staticmethod
    def _elapsed(started: float) -> float:
        return time.monotonic() - started


def _is_product_type(t) -> bool:
    """兼容简写 'Product' 和完整 URL 'http://schema.org/Product'。"""
    if isinstance(t, str):
        return t == "Product" or t.endswith("/Product")
    if isinstance(t, list):
        return any(_is_product_type(x) for x in t)
    return False
