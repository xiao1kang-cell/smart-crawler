"""通用采集器 —— 覆盖无专用采集器的站点（Flexispot / VonHaus / Woltu / Vidaxl 等）。

策略：sitemap 发现商品 URL → 逐页多策略解析：
  1. JSON-LD <script type="application/ld+json"> 的 Product schema
  2. OpenGraph + 微数据（og:title / product:price:amount / itemprop="price"）
  3. 站内 dataLayer JSON 兜底

sites.yaml 中该站点可选字段：
  sitemap:        sitemap 入口（默认 {url}/sitemap.xml）
  product_match:  商品 URL 必含子串（如 "/p/"）
  max_products:   单次抓取上限（默认近似不截断；显式配置才缩小）
"""
from __future__ import annotations

import gzip
import html as html_lib
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from ..config import get_sites
from ..crawl_diagnostics import (
    ANTI_BOT_CHALLENGE,
    FailureInfo,
    PARSE_NO_PRODUCT,
    STAGE_FETCH,
    STAGE_PARSE,
    record_url_state,
)
from ..currency import SITE_CURRENCY_BY_COUNTRY, currency_for_site
from ..db import SessionLocal
from ..fetching import CrawlerFetcher, FetchContext
from ..pipeline import to_price
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("GENERIC_LIMIT", "999999"))
REQUEST_TIMEOUT = int(os.environ.get("GENERIC_REQUEST_TIMEOUT", "12"))
MAX_ELAPSED_SEC = float(os.environ.get("GENERIC_MAX_ELAPSED_SEC", "0"))
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)\s*$")
_NOISE_RE = re.compile(
    r"(blog|/article|/news|care-center|/category/|/help|/about|/contact|"
    r"/privacy|/terms|/login|/account|/cart|/checkout|/search|/stores?|"
    r"/collections?$|/categories?$)",
    re.I,
)
_PRODUCT_HINT_RE = re.compile(
    r"(/products?/|/product/|/p/|/pd/|/pdp/|/item/|/itm/|/dp/|"
    r"/sku/|/catalog/product|product[-_]|p-[0-9]|sku[-_/]?[0-9])",
    re.I,
)
_BLOCKED_TITLE_RE = re.compile(
    r"(access\s*denied|just\s*a\s*moment|checking\s*your\s*browser|"
    r"security\s*check|verify\s*(?:you\s*are\s*)?human|captcha|blocked)",
    re.I,
)
_BLOCKED_BODY_RE = re.compile(
    r"(cf-chl-|/cdn-cgi/challenge-platform/|challenge-platform|"
    r"perimeterx|px-captcha|g-recaptcha|"
    r"please\s+enable\s+cookies|unusual\s+traffic)",
    re.I,
)
_WEAK_TITLE_RE = re.compile(
    r"^(product|item|sku|untitled|detail|details|view product|shop now)$",
    re.I,
)
_PROMO_TEXT_RE = re.compile(
    r"(\b\d{1,2}(?:\.\d+)?\s*%\s*(?:off|discount)?\b|"
    r"\b(?:coupon|deal|discount|clearance|sale|promo|promotion|save|offer)\b|"
    r"满减|优惠|折扣|券)",
    re.I,
)
_GONE_PRODUCT_RE = re.compile(
    r"(product|item|article|artikel).{0,40}(no longer available|"
    r"not available|unavailable|not found)|"
    r"(no longer available|not available|unavailable|not found).{0,40}"
    r"(product|item|article|artikel)|"
    r"dieser artikel ist leider nicht mehr verf[uü]gbar|"
    r"artikel.+nicht mehr verf[uü]gbar|"
    r"produkt.+nicht mehr verf[uü]gbar",
    re.I,
)


class GenericCrawler(BaseCrawler):
    platform = "generic"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.base = site.url.rstrip("/")
        self.sitemap_hint = hints.get("sitemap")
        self.sitemap = self.sitemap_hint or (self.base + "/sitemap.xml")
        self.product_match = hints.get("product_match", "")
        self.exclude_match = hints.get("exclude_match", "")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua(),
                          "Accept-Language": "en-US,en;q=0.9"})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def _fetcher(self, kind: str, source: str) -> CrawlerFetcher:
        return self.make_fetcher(kind=kind, source=source,
                                 timeout=REQUEST_TIMEOUT, use_proxy=True)

    def _fetch_text(self, sess: creq.Session | None, url: str,
                    *, kind: str, source: str) -> tuple[int | None, str, bytes]:
        if sess is None:
            res = self._fetcher(kind, source).get(
                url,
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            return res.status, res.text, res.content
        try:
            resp = sess.get(url, timeout=30)
            return resp.status_code, resp.text or "", resp.content or b""
        except Exception:
            return None, "", b""

    def _discover_sitemaps(self, sess: creq.Session | None) -> list[str]:
        """从配置、robots.txt 和常见路径发现 sitemap 入口。"""
        urls: list[str] = []
        if self.sitemap_hint:
            urls.append(self.sitemap_hint)

        robots = urljoin(self.base + "/", "robots.txt")
        try:
            status, text, _ = self._fetch_text(
                sess, robots, kind="sitemap", source="robots")
            if status == 200:
                urls.extend(_SITEMAP_RE.findall(text or ""))
        except Exception:
            pass

        for path in (
            "sitemap.xml",
            "sitemap_index.xml",
            "sitemap-index.xml",
            "sitemap/sitemap.xml",
            "sitemaps/sitemap.xml",
            "product-sitemap.xml",
            "products-sitemap.xml",
            "sitemap-products.xml",
        ):
            urls.append(urljoin(self.base + "/", path))

        return self._dedupe(urls)

    def _sitemap_locs(self, sess: creq.Session | None, url: str,
                      depth: int = 0) -> list[str]:
        """递归展开 sitemap（索引 / .gz / 普通），返回全部 <loc>。"""
        if depth > 3:
            return []
        try:
            status, _, raw = self._fetch_text(
                sess, url, kind="sitemap", source="sitemap")
            if status is None or status >= 400:
                return []
        except Exception:
            return []
        try:
            text = (gzip.decompress(raw) if url.endswith(".gz")
                    else raw).decode("utf-8", "ignore")
        except (OSError, gzip.BadGzipFile):
            text = raw.decode("utf-8", "ignore")
        locs = [html_lib.unescape(x.strip())
                for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", text)]
        sub = [l for l in locs if l.endswith(".xml") or l.endswith(".xml.gz")]
        if sub and len(sub) == len(locs):            # 纯 sitemap 索引，递归
            out: list[str] = []
            for s in sub:
                out.extend(self._sitemap_locs(sess, s, depth + 1))
            return out
        return locs

    def _discover_product_urls(self, sess: creq.Session | None,
                               result: CrawlResult) -> list[str]:
        locs: list[str] = []
        sitemap_urls = self._discover_sitemaps(sess)
        for sm in sitemap_urls:
            before = len(locs)
            locs.extend(self._sitemap_locs(sess, sm))
            if len(locs) > before:
                result.notes.append(f"sitemap 命中: {sm}")

        cands = [u for u in self._dedupe(locs) if self._is_candidate_url(u)]
        products = [u for u in cands if self._is_product_url(u)]
        if not products and not self.product_match:
            products = cands

        if products:
            return products

        links = self._links_from_page(sess, self.site.url)
        if links:
            result.notes.append(f"入口页发现 {len(links)} 个候选商品链接")
        return links

    def _links_from_page(self, sess: creq.Session | None, url: str) -> list[str]:
        status, text, _ = self._fetch_text(
            sess, url, kind="category", source="homepage")
        if status is None or status >= 400:
            return []
        base_host = urlparse(self.base).netloc
        tree = HTMLParser(text or "")
        links: list[str] = []
        for node in tree.css("a[href]"):
            href = node.attributes.get("href") or ""
            full = urljoin(url, href.split("#", 1)[0])
            if urlparse(full).netloc != base_host:
                continue
            if self._is_candidate_url(full) and self._is_product_url(full):
                links.append(full)
        return self._dedupe(links)

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = None
        started = time.monotonic()

        if self.site.platform and self.site.platform != self.platform:
            result.notes.append(
                f"未注册平台 {self.site.platform}，已自动降级为 generic 通用抓取")

        products = self._discover_product_urls(sess, result)
        total = len(products)
        targets = products[: self.limit]
        result.total_product_count = total
        result.notes.append(
            f"通用发现 {total} 个候选商品 URL，本次抓取 {len(targets)} 条")
        if len(targets) < total:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"通用 sitemap/入口共发现 {total} 个候选商品 URL，"
                f"本次只计划抓取 {len(targets)} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 GENERIC_LIMIT 后重跑。"
        if not targets:
            result.notes.append("⚠ 通用发现未找到商品 URL，可为该站点配置 "
                                 "sitemap / product_match，或启用专用/浏览器策略")
            return result

        ok = 0
        for url in targets:
            elapsed = time.monotonic() - started
            if MAX_ELAPSED_SEC > 0 and elapsed >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 GENERIC_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回已解析结果（ok={ok}/{len(targets)}）")
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"达到 GENERIC_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"本次只解析 {ok}/{len(targets)} 个商品"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "放宽 GENERIC_MAX_ELAPSED_SEC 或拆分失败商品重抓。"
                )
                break
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

    def crawl_failed_products(self, urls: list[str]) -> CrawlResult:
        """Retry a known set of failed product URLs without rediscovery."""
        result = CrawlResult()
        started = time.monotonic()
        targets = self._dedupe([u for u in urls if u])
        result.total_product_count = len(targets)
        if not targets:
            result.notes.append("没有可重试的失败商品 URL")
            return result

        ok = 0
        for url in targets:
            if MAX_ELAPSED_SEC > 0 and time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"达到 GENERIC_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"失败商品重试只解析 {ok}/{len(targets)} 个商品"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = "继续失败商品重试。"
                break
            try:
                row = self._parse(None, url, source="generic_failed_product_retry")
                if row:
                    result.products.append(row)
                    ok += 1
            except Exception as exc:
                self._record_parse_failure(
                    url,
                    f"Generic 失败商品重试异常: {exc}",
                )
                result.notes.append(f"跳过 {url[:60]}: {exc}")
            self.sleep()
        result.notes.append(f"失败商品重试成功解析 {ok}/{len(targets)} 个商品页")
        return result

    def _is_candidate_url(self, url: str) -> bool:
        if not url or url.endswith((".xml", ".xml.gz")):
            return False
        if self.exclude_match and self.exclude_match in url:
            return False
        path = urlparse(url).path.lower()
        if not path or path == "/":
            return False
        if _NOISE_RE.search(path):
            return False
        return True

    def _is_product_url(self, url: str) -> bool:
        if self.product_match:
            return self.product_match in url
        path = urlparse(url).path.lower()
        return bool(_PRODUCT_HINT_RE.search(path))

    def _parse(self, sess: creq.Session | None, url: str,
               *, source: str = "candidate") -> dict | None:
        status, html, _ = self._fetch_text(
            sess, url, kind="product", source=source)
        tree = HTMLParser(html or "")
        if self._looks_gone_product_page(tree, html):
            self._record_url_skipped(
                url,
                status,
                source=source,
            )
            return None
        if status is None or status >= 400:
            return None
        self.snapshot(self._slug(url), html)       # 原始商品页归档
        if self._looks_blocked_page(tree, html):
            self._record_fetch_failure(
                url,
                ANTI_BOT_CHALLENGE,
                "Generic 商品页疑似反爬挑战或人机验证页面",
                source=source,
            )
            return None
        data = self._merge_product_data(
            self._from_jsonld(html) or {},
            self._from_hydration_json(html) or {},
        )

        title = self._best_title(
            data.get("name"),
            self._meta(tree, "og:title"),
            self._meta(tree, "twitter:title"),
            self._title_from_page(tree),
            sku=data.get("sku") or self._slug(url),
        )
        if not title:
            self._record_parse_failure(
                url,
                "Generic 商品页未能从 JSON-LD、hydration、meta、h1/title 中解析出有效商品标题",
                source=source,
            )
            return None
        sale = data.get("price") or self._meta_price(tree) or self._dom_price(tree)
        # Pipeline 已允许 price 缺失。通用抓取应优先保留 SKU/title/URL，
        # 价格缺口交给数据质量页暴露，避免无价格站点被误判为 0 商品。
        original = data.get("original_price") or sale
        attributes = self._merge_attributes(
            data.get("attributes") or {},
            self._dom_promotion_attributes(tree),
        )

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
            or currency_for_site(self.site.site)
            or SITE_CURRENCY_BY_COUNTRY.get((self.site.country or "").upper(), "USD"),
            "ratings": data.get("rating"),
            "review_count": data.get("review_count"),
            "status": data.get("status", "on_sale"),
            "has_video": "<video" in html,
            "mpn": data.get("mpn"),
            "gtin": data.get("gtin"),
            "attributes": attributes or None,
            "brand": data.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    def _record_parse_failure(self, url: str, detail: str,
                              *, source: str = "candidate") -> None:
        self._record_url_failure(
            url,
            FailureInfo(
                PARSE_NO_PRODUCT,
                STAGE_PARSE,
                detail,
                True,
                "补充该站点解析器覆盖后，只重试失败商品 URL。",
            ),
            source=source,
        )

    def _record_fetch_failure(self, url: str, code: str, detail: str,
                              *, source: str = "candidate") -> None:
        self._record_url_failure(
            url,
            FailureInfo(
                code,
                STAGE_FETCH,
                detail,
                True,
                "检查目标页是否为反爬/跳转页面，必要时换代理或启用专用解析器。",
            ),
            source=source,
        )

    def _record_url_skipped(self, url: str, http_status: int | None,
                            *, source: str) -> None:
        if not self.job_id:
            return
        db = SessionLocal()
        try:
            record_url_state(
                db,
                site=self.site.site,
                url=url,
                kind="product",
                source=source,
                status="skipped",
                http_status=http_status,
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _record_url_failure(self, url: str, failure: FailureInfo,
                            *, source: str) -> None:
        if not self.job_id:
            return
        db = SessionLocal()
        try:
            record_url_state(
                db,
                site=self.site.site,
                url=url,
                kind="product",
                source=source,
                status="failed",
                failure=failure,
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    @staticmethod
    def _from_jsonld(html: str) -> dict | None:
        """解析 JSON-LD 的 Product schema。"""
        best: tuple[int, dict] | None = None
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            for it in GenericCrawler._jsonld_nodes(doc):
                if not isinstance(it, dict):
                    continue
                types = GenericCrawler._jsonld_types(it.get("@type"))
                if not ({"product", "productgroup"} & types):
                    continue
                candidate = GenericCrawler._product_from_jsonld_node(it)
                if not candidate:
                    continue
                score = GenericCrawler._candidate_score(candidate)
                if best is None or score > best[0]:
                    best = (score, candidate)
        return best[1] if best else None

    @staticmethod
    def _product_from_jsonld_node(it: dict) -> dict | None:
        offer_items = GenericCrawler._offer_items(it.get("offers"))
        offers = offer_items[0] if offer_items else {}
        rating = it.get("aggregateRating") or {}
        avail = str(offers.get("availability", "")).lower()
        price = GenericCrawler._price_from_offer(offers)
        original_price = GenericCrawler._num(offers.get("highPrice")) or price
        name = it.get("name")
        if not name and price is None and not it.get("sku") and not it.get("productID"):
            return None
        return {
            "name": name,
            "sku": it.get("sku") or it.get("mpn") or it.get("productID"),
            "description": it.get("description"),
            "images": GenericCrawler._image_urls(it.get("image")),
            "price": price,
            "original_price": original_price,
            "currency": offers.get("priceCurrency")
            or GenericCrawler._currency_from_price_spec(offers),
            "status": "out_of_stock" if "outofstock" in avail
            or "soldout" in avail
            or "out of stock" in avail else "on_sale",
            "rating": GenericCrawler._num(rating.get("ratingValue")),
            "review_count": GenericCrawler._int(
                rating.get("reviewCount") or rating.get("ratingCount")
            ),
            "mpn": it.get("mpn"),
            "gtin": it.get("gtin13") or it.get("gtin"),
            "brand": GenericCrawler._named_value(it.get("brand")),
            "category": GenericCrawler._named_value(it.get("category")),
            "attributes": GenericCrawler._jsonld_attributes(it, offer_items),
        }

    @staticmethod
    def _candidate_score(candidate: dict) -> int:
        name = str(candidate.get("name") or "")
        return (
            min(len(name), 80) // 8
            + (5 if candidate.get("price") is not None else 0)
            + (2 if candidate.get("original_price") is not None else 0)
            + (2 if candidate.get("sku") else 0)
            + (2 if candidate.get("images") else 0)
            + (1 if candidate.get("brand") else 0)
            + (1 if candidate.get("category") else 0)
            + (1 if candidate.get("rating") is not None else 0)
        )

    @staticmethod
    def _from_hydration_json(html: str) -> dict | None:
        """从 Next/Nuxt/前端 hydration JSON 中兜底提取商品字段。"""
        best: tuple[int, dict] | None = None
        for attrs, block in re.findall(
                r"<script([^>]*)>(.*?)</script>", html, re.S):
            if "application/ld+json" in (attrs or "").lower():
                continue
            text = html_lib.unescape((block or "").strip())
            if not text or len(text) > 5_000_000:
                continue
            if not (text.startswith("{") or text.startswith("[")):
                continue
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                continue
            for node in GenericCrawler._walk_json(doc):
                candidate = GenericCrawler._product_from_json_node(node)
                if not candidate:
                    continue
                score = (
                    (3 if candidate.get("name") else 0) +
                    (2 if candidate.get("price") is not None else 0) +
                    (1 if candidate.get("sku") else 0) +
                    (1 if candidate.get("images") else 0)
                )
                if best is None or score > best[0]:
                    best = (score, candidate)
        return best[1] if best else None

    @staticmethod
    def _walk_json(value, depth: int = 0):
        if depth > 8:
            return
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from GenericCrawler._walk_json(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from GenericCrawler._walk_json(child, depth + 1)

    @staticmethod
    def _product_from_json_node(node) -> dict | None:
        if not isinstance(node, dict):
            return None
        title = GenericCrawler._first_json_value(
            node, ("name", "title", "productName", "displayName"))
        price = GenericCrawler._json_price(
            node, ("salePrice", "currentPrice", "finalPrice", "price",
                   "discountPrice", "promoPrice", "priceRange",
                   "price_range", "minVariantPrice", "lowPrice"))
        original = GenericCrawler._json_price(
            node, ("originalPrice", "regularPrice", "listPrice", "wasPrice",
                   "compareAtPrice", "msrp", "rrp", "maxVariantPrice",
                   "highPrice"))
        sku = GenericCrawler._first_json_value(
            node, ("sku", "skuCode", "productSku", "productID", "productId",
                   "itemNumber", "mpn", "id"))
        images = GenericCrawler._image_urls(
            GenericCrawler._first_json_value(
                node, ("images", "image", "imageUrl", "image_url", "media",
                       "gallery")))
        if not title or (price is None and not sku and not images):
            return None
        rating = GenericCrawler._json_price(
            node, ("ratingValue", "rating", "averageRating", "avgRating"))
        reviews = GenericCrawler._json_int(
            GenericCrawler._first_json_value(
                node, ("reviewCount", "ratingCount", "reviewsCount",
                       "reviews")))
        attributes = GenericCrawler._promotion_attributes_from_mapping(node)
        return {
            "name": title,
            "sku": str(sku) if sku is not None else None,
            "description": GenericCrawler._first_json_value(
                node, ("description", "shortDescription")),
            "images": images,
            "price": price,
            "original_price": original or price,
            "currency": GenericCrawler._first_json_value(
                node, ("priceCurrency", "currency", "currencyCode")),
            "status": GenericCrawler._status_from_availability(
                GenericCrawler._first_json_value(
                    node, ("availability", "stockStatus", "status"))),
            "rating": rating,
            "review_count": reviews,
            "mpn": GenericCrawler._first_json_value(node, ("mpn", "model")),
            "gtin": GenericCrawler._first_json_value(
                node, ("gtin", "gtin13", "ean", "barcode")),
            "brand": GenericCrawler._named_value(
                GenericCrawler._first_json_value(node, ("brand", "manufacturer"))),
            "category": GenericCrawler._named_value(
                GenericCrawler._first_json_value(
                    node, ("category", "categoryName", "breadcrumb"))),
            "attributes": attributes,
        }

    @staticmethod
    def _first_json_value(node: dict, keys: tuple[str, ...]):
        lower = {str(k).lower(): v for k, v in node.items()}
        for key in keys:
            if key in node and node[key] not in (None, "", [], {}):
                return node[key]
            value = lower.get(key.lower())
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _json_price(node: dict, keys: tuple[str, ...]):
        value = GenericCrawler._first_json_value(node, keys)
        return GenericCrawler._price_from_json_value(value)

    @staticmethod
    def _price_from_json_value(value, depth: int = 0, source_key: str = ""):
        if value in (None, "", [], {}) or depth > 5:
            return None
        if isinstance(value, dict):
            lower = {str(k).lower(): k for k in value}
            priority = (
                "value", "amount", "price", "centamount",
                "saleprice", "currentprice", "finalprice", "discountprice",
                "promoPrice", "current", "sale", "final", "minvariantprice",
                "minprice", "lowprice", "minimumprice", "regularprice",
                "listprice", "wasprice", "compareatprice",
            )
            for key in priority:
                actual = key if key in value else lower.get(key.lower())
                if not actual:
                    continue
                price = GenericCrawler._price_from_json_value(
                    value.get(actual), depth + 1, actual)
                if price is not None:
                    return price
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    price = GenericCrawler._price_from_json_value(
                        child, depth + 1, str(key))
                    if price is not None:
                        return price
            return None
        if isinstance(value, list):
            for child in value:
                price = GenericCrawler._price_from_json_value(
                    child, depth + 1, source_key)
                if price is not None:
                    return price
            return None
        price = GenericCrawler._num(value)
        if price is not None and source_key.lower() == "centamount" and price > 999:
            return price / 100
        return price

    @staticmethod
    def _json_int(value):
        if isinstance(value, dict):
            value = value.get("value") or value.get("count")
        return GenericCrawler._int(value)

    @staticmethod
    def _status_from_availability(value) -> str | None:
        if not value:
            return None
        raw = str(value).lower()
        if any(token in raw for token in ("outofstock", "out of stock",
                                          "soldout", "sold out",
                                          "unavailable")):
            return "out_of_stock"
        return "on_sale"

    @staticmethod
    def _looks_blocked_page(tree: HTMLParser, html: str) -> bool:
        signals = []
        for selector in ("title", "h1"):
            node = tree.css_first(selector)
            if node:
                signals.append(node.text(strip=True))
        for prop in ("og:title", "twitter:title"):
            node = (tree.css_first(f'meta[property="{prop}"]')
                    or tree.css_first(f'meta[name="{prop}"]'))
            if node:
                signals.append(node.attributes.get("content") or "")
        if any(_BLOCKED_TITLE_RE.search(text or "") for text in signals):
            return True
        return bool(_BLOCKED_BODY_RE.search((html or "")[:12000]))

    @staticmethod
    def _looks_gone_product_page(tree: HTMLParser, html: str) -> bool:
        signals = []
        for selector in ("h1", "title"):
            node = tree.css_first(selector)
            if node:
                signals.append(node.text(separator=" ", strip=True))
        for prop in ("og:title", "twitter:title"):
            node = (tree.css_first(f'meta[property="{prop}"]')
                    or tree.css_first(f'meta[name="{prop}"]'))
            if node:
                signals.append(node.attributes.get("content") or "")
        head = " ".join(text for text in signals if text)
        body = (html or "")[:20000]
        return bool(_GONE_PRODUCT_RE.search(head) or _GONE_PRODUCT_RE.search(body))

    @staticmethod
    def _merge_product_data(primary: dict, fallback: dict) -> dict:
        merged = dict(primary or {})
        for key, value in (fallback or {}).items():
            if value in (None, "", [], {}):
                continue
            current = merged.get(key)
            if key == "name" and current:
                if len(str(value)) > len(str(current)) + 8:
                    merged[key] = value
                continue
            if key == "attributes":
                merged[key] = GenericCrawler._merge_attributes(current, value)
                continue
            if current in (None, "", [], {}):
                merged[key] = value
        return merged

    @staticmethod
    def _jsonld_nodes(value, depth: int = 0):
        if depth > 5:
            return
        if isinstance(value, list):
            for item in value:
                yield from GenericCrawler._jsonld_nodes(item, depth + 1)
            return
        if not isinstance(value, dict):
            return

        yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            child = value.get(key)
            if key == "itemListElement" and isinstance(child, list):
                for item in child:
                    if isinstance(item, dict) and "item" in item:
                        yield from GenericCrawler._jsonld_nodes(
                            item.get("item"), depth + 1
                        )
                    yield from GenericCrawler._jsonld_nodes(item, depth + 1)
                continue
            yield from GenericCrawler._jsonld_nodes(child, depth + 1)

    @staticmethod
    def _jsonld_types(value) -> set[str]:
        raw = value if isinstance(value, list) else [value]
        out: set[str] = set()
        for item in raw:
            if not item:
                continue
            name = str(item).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            if name:
                out.add(name.lower())
        return out

    @staticmethod
    def _first_dict(value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    return item
        return {}

    @staticmethod
    def _offer_items(value) -> list[dict]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _jsonld_attributes(product: dict, offers: list[dict]) -> dict:
        attrs = {}
        if offers:
            attrs["offers"] = offers
        for key in (
            "offers",
            "promotions",
            "promotion",
            "coupons",
            "coupon",
            "deals",
            "deal",
            "discounts",
            "badges",
            "labels",
        ):
            value = product.get(key)
            if value not in (None, "", [], {}) and key != "offers":
                attrs[key] = value
        return GenericCrawler._compact_mapping(attrs)

    @staticmethod
    def _promotion_attributes_from_mapping(node: dict) -> dict:
        attrs = {}
        lower = {str(k).lower(): k for k in node}
        for key in (
            "offers",
            "offer",
            "promotions",
            "promotion",
            "coupons",
            "coupon",
            "deals",
            "deal",
            "discounts",
            "discount",
            "badges",
            "badge",
            "labels",
            "label",
            "campaigns",
            "campaign",
        ):
            actual = key if key in node else lower.get(key.lower())
            if not actual:
                continue
            value = node.get(actual)
            if value not in (None, "", [], {}):
                attrs[key] = value
        return GenericCrawler._compact_mapping(attrs)

    @staticmethod
    def _merge_attributes(primary, fallback) -> dict:
        merged = {}
        if isinstance(primary, dict):
            merged.update(primary)
        if isinstance(fallback, dict):
            for key, value in fallback.items():
                if value not in (None, "", [], {}):
                    merged.setdefault(key, value)
        return GenericCrawler._compact_mapping(merged)

    @staticmethod
    def _compact_mapping(value: dict) -> dict:
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if v not in (None, "", [], {})}

    @staticmethod
    def _price_from_offer(offer: dict):
        for key in ("price", "lowPrice", "highPrice"):
            price = GenericCrawler._num(offer.get(key))
            if price is not None:
                return price
        spec = offer.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for item in specs:
            if not isinstance(item, dict):
                continue
            for key in ("price", "minPrice", "maxPrice"):
                price = GenericCrawler._num(item.get(key))
                if price is not None:
                    return price
        return None

    @staticmethod
    def _currency_from_price_spec(offer: dict) -> str | None:
        spec = offer.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for item in specs:
            if isinstance(item, dict) and item.get("priceCurrency"):
                return item.get("priceCurrency")
        return None

    @staticmethod
    def _named_value(value) -> str | None:
        if isinstance(value, dict):
            return value.get("name")
        if isinstance(value, str):
            return value
        return None

    @staticmethod
    def _image_urls(value) -> list[str]:
        items = value if isinstance(value, list) else [value]
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            if isinstance(item, str):
                url = item
            elif isinstance(item, dict):
                url = item.get("url") or item.get("contentUrl")
            else:
                url = None
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    @staticmethod
    def _best_title(*values, sku: str | None = None) -> str | None:
        best: tuple[int, str] | None = None
        for value in values:
            text = html_lib.unescape(str(value or "")).strip()
            if not text:
                continue
            score = GenericCrawler._title_score(text, sku)
            if best is None or score > best[0]:
                best = (score, text)
        return best[1] if best else None

    @staticmethod
    def _title_score(value: str, sku: str | None = None) -> int:
        text = re.sub(r"\s+", " ", value or "").strip()
        if not text:
            return -1000
        lowered = text.lower()
        sku_text = str(sku or "").strip().lower()
        weak = (
            len(text) < 6
            or bool(_WEAK_TITLE_RE.match(text))
            or (bool(sku_text) and lowered == sku_text)
            or re.fullmatch(r"[A-Z0-9._-]{4,}", text or "") is not None
        )
        score = min(len(text), 140)
        if weak:
            score -= 160
        if "|" in text or " - " in text:
            score -= 8
        return score

    @staticmethod
    def _title_from_page(tree: HTMLParser) -> str | None:
        selectors = (
            '[itemprop="name"]',
            '[data-testid*=product-title]',
            '[data-testid*=product_name]',
            '[data-testid*=title]',
            '[data-test*=product-title]',
            '[data-test*=product_name]',
            '[data-test*=title]',
            '[class*=product-title]',
            '[class*=product_name]',
            '[class*=ProductTitle]',
            '[class*=productTitle]',
            '[class*=product-name]',
            '[class*=product_name]',
            '[id*=product-title]',
            '[id*=product_name]',
            'h1',
            'title',
        )
        values: list[str] = []
        for selector in selectors:
            for node in tree.css(selector)[:8]:
                for attr in ("content", "aria-label", "title", "data-title"):
                    value = (node.attributes.get(attr) or "").strip()
                    if value:
                        values.append(html_lib.unescape(value))
                text = (node.text(separator=" ", strip=True) or "").strip()
                if text:
                    values.append(html_lib.unescape(text))
        return GenericCrawler._best_title(*values)

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

    def _dom_price(self, tree: HTMLParser):
        """保守 DOM 兜底：很多普通站只把价格放在 data-* 或 .price 文本里。"""
        selectors = (
            "[data-price]",
            "[data-sale-price]",
            "[data-current-price]",
            "[data-product-price]",
            "[data-testid*=price]",
            "[data-test*=price]",
            "[class*=sale-price]",
            "[class*=current-price]",
            "[class*=product-price]",
            "[class*=price]",
            "[id*=price]",
        )
        attrs = (
            "content",
            "data-price",
            "data-sale-price",
            "data-current-price",
            "data-product-price",
            "aria-label",
            "value",
        )
        for selector in selectors:
            for node in tree.css(selector)[:20]:
                values = [node.attributes.get(attr) for attr in attrs]
                values.append(node.text(separator=" ", strip=True))
                for value in values:
                    price = self._num(value)
                    if price is not None and 0 < price < 1_000_000:
                        return price
        return None

    @staticmethod
    def _dom_promotion_attributes(tree: HTMLParser) -> dict:
        selectors = (
            "[data-testid*=promo]",
            "[data-testid*=coupon]",
            "[data-testid*=deal]",
            "[data-testid*=discount]",
            "[data-testid*=badge]",
            "[data-test*=promo]",
            "[data-test*=coupon]",
            "[data-test*=deal]",
            "[data-test*=discount]",
            "[data-test*=badge]",
            "[class*=promo]",
            "[class*=coupon]",
            "[class*=deal]",
            "[class*=discount]",
            "[class*=badge]",
            "[id*=promo]",
            "[id*=coupon]",
            "[id*=deal]",
            "[id*=discount]",
        )
        labels: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            for node in tree.css(selector)[:20]:
                values = [
                    node.attributes.get("aria-label"),
                    node.attributes.get("title"),
                    node.attributes.get("data-label"),
                    node.attributes.get("data-promo"),
                    node.attributes.get("data-coupon"),
                    node.text(separator=" ", strip=True),
                ]
                for value in values:
                    text = re.sub(r"\s+", " ", str(value or "")).strip()
                    if not text or len(text) > 180:
                        continue
                    if not _PROMO_TEXT_RE.search(text):
                        continue
                    key = text.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    labels.append(text[:160])
                    if len(labels) >= 12:
                        return {"promotions": labels}
        return {"promotions": labels} if labels else {}

    @staticmethod
    def _slug(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0][:80]

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    @staticmethod
    def _num(v):
        return to_price(v)

    @staticmethod
    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
