"""VonHaus 采集器 —— 杜木 Domu，Magento 站。

VonHaus 的 sitemap.xml 把分类页和商品页混在一起（都是 /vh_en/<slug>），
商品页没有 Product JSON-LD 但有干净的 OpenGraph 商品 meta。
策略：顺序扫描 sitemap URL，逐页判断——是商品就解析，是分类就跳过，
默认扫描完整 sitemap；VONHAUS_LIMIT / VONHAUS_SCAN_CAP 仅用于显式调试。
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import unquote, urlsplit

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
        jsonld_product = self._jsonld_product(tree)
        jsonld_offers = (jsonld_product or {}).get("offers") or {}
        if isinstance(jsonld_offers, list):
            jsonld_offers = jsonld_offers[0] if jsonld_offers else {}
        price = self._meta(tree, "product:price:amount")
        if price is None and isinstance(jsonld_offers, dict):
            price = self._price_from_text(jsonld_offers.get("price"))
        if price is None:                       # 无商品价格 meta → 分类页
            return None
        title = self._meta_raw(tree, "og:title") or (jsonld_product or {}).get("name")
        h1 = tree.css_first("h1")
        if h1 and h1.text(strip=True):
            title = h1.text(strip=True)
        if not title:
            return None

        currency = (
            self._meta_raw(tree, "product:price:currency")
            or (jsonld_offers.get("priceCurrency") if isinstance(jsonld_offers, dict) else None)
            or "GBP"
        )
        avail = (self._meta_raw(tree, "product:availability")
                 or self._meta_raw(tree, "og:availability")
                 or (jsonld_offers.get("availability") if isinstance(jsonld_offers, dict) else "")
                 or "").lower()
        image = self._meta_raw(tree, "og:image") or self._jsonld_image(jsonld_product)
        original_price = self._original_price(tree, price)
        promo_labels = self._promotion_labels(tree) + self._jsonld_promotion_labels(jsonld_product)
        has_free_shipping = bool(re.search(
            r"free\s+(?:delivery|shipping)|delivery\s+included|"
            r"shipping\s+included",
            html,
            re.I,
        )) or any(re.search(r"free\s+(?:delivery|shipping)|delivery\s+included|shipping\s+included",
                            label, re.I) for label in promo_labels)
        slug = url.rstrip("/").split("/")[-1]
        # sku：优先 data-product-id，退化为 slug
        pid = tree.css_first("[data-product-id]")
        sku = (
            (pid.attributes.get("data-product-id") if pid else None)
            or (jsonld_product or {}).get("sku")
            or (jsonld_product or {}).get("mpn")
            or slug
        )

        return {
            "sku": str(sku), "spu": str(sku),
            "title": title,
            "description": self._meta_raw(tree, "og:description")
            or (jsonld_product or {}).get("description"),
            "image_urls": [image] if image else [],
            "category_path": (
                self._jsonld_breadcrumb(tree)
                or self._breadcrumb(tree)
                or self._category_from_url(url)
            ),
            "sale_price": price,
            "original_price": original_price or price,
            "currency": currency,
            "ratings": self._jsonld_rating_value(jsonld_product),
            "review_count": self._review_count(tree, html, jsonld_product),
            "status": "out_of_stock" if ("outofstock" in avail
                                         or "out of stock" in avail)
            else "on_sale",
            "has_free_shipping": has_free_shipping,
            "attributes": {
                "promotions": promo_labels,
                "free_shipping_label": "Free delivery" if has_free_shipping else None,
            },
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

    def _original_price(self, tree: HTMLParser, sale_price: float | None):
        candidates = (
            "[class*=old-price]", "[class*=old_price]", "[class*=was-price]",
            "[class*=was_price]", "[class*=regular-price]", "[class*=rrp]",
            "[class*=strike]", "del", "s",
        )
        for selector in candidates:
            for node in tree.css(selector):
                price = self._price_from_text(node.text(separator=" ", strip=True))
                if price is not None and (sale_price is None or price >= sale_price):
                    return price
        return None

    @staticmethod
    def _price_from_text(text: str | None):
        if not text:
            return None
        m = _PRICE_RE.search(str(text).replace(",", ""))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None

    @staticmethod
    def _promotion_labels(tree: HTMLParser) -> list[str]:
        selectors = (
            "[class*=promo]", "[class*=promotion]", "[class*=coupon]",
            "[class*=discount]", "[class*=sale]", "[class*=deal]",
            "[class*=offer]", "[class*=delivery]", "[class*=shipping]",
            ".badge", ".label",
        )
        promo_re = re.compile(
            r"sale|deal|discount|coupon|promo|save|off|bundle|"
            r"free\s+(?:delivery|shipping)|delivery\s+included|"
            r"shipping\s+included",
            re.I,
        )
        labels: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            for node in tree.css(selector):
                text = re.sub(r"\s+", " ", node.text(separator=" ", strip=True))
                if not text or len(text) > 180 or not promo_re.search(text):
                    continue
                if text not in seen:
                    seen.add(text)
                    labels.append(text)
                if len(labels) >= 6:
                    return labels
        return labels

    @staticmethod
    def _breadcrumb(tree: HTMLParser) -> str | None:
        crumbs = [n.text(strip=True) for n in
                  tree.css('.breadcrumbs a, [class*=breadcrumb] a, nav[aria-label*=breadcrumb] a')]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "", "vonhaus")]
        return "/".join(crumbs[:3]) or None

    @staticmethod
    def _jsonld_blocks(tree: HTMLParser) -> list[dict]:
        blocks: list[dict] = []
        for node in tree.css('script[type="application/ld+json"]'):
            raw = node.text(strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            stack = parsed if isinstance(parsed, list) else [parsed]
            while stack:
                item = stack.pop(0)
                if not isinstance(item, dict):
                    continue
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                blocks.append(item)
        return blocks

    @classmethod
    def _jsonld_product(cls, tree: HTMLParser) -> dict | None:
        for node in cls._jsonld_blocks(tree):
            raw_type = node.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if any(str(t).lower() == "product" for t in types if t):
                return node
        return None

    @classmethod
    def _jsonld_breadcrumb(cls, tree: HTMLParser) -> str | None:
        for node in cls._jsonld_blocks(tree):
            raw_type = node.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(str(t).lower() == "breadcrumblist" for t in types if t):
                continue
            names: list[str] = []
            for elem in node.get("itemListElement") or []:
                if not isinstance(elem, dict):
                    continue
                item = elem.get("item")
                name = item.get("name") if isinstance(item, dict) else None
                name = name or elem.get("name")
                if not name:
                    continue
                text = str(name).strip()
                if not text or text.lower() in {"home", "vonhaus"}:
                    continue
                names.append(text)
            if names:
                return "/".join(names[:3])
        return None

    @staticmethod
    def _jsonld_image(product: dict | None) -> str | None:
        if not isinstance(product, dict):
            return None
        image = product.get("image")
        if isinstance(image, str):
            return image
        if isinstance(image, list):
            for item in image:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict) and item.get("url"):
                    return str(item.get("url")).strip()
        if isinstance(image, dict) and image.get("url"):
            return str(image.get("url")).strip()
        return None

    @staticmethod
    def _jsonld_rating_value(product: dict | None):
        if not isinstance(product, dict):
            return None
        rating = product.get("aggregateRating")
        if not isinstance(rating, dict):
            return None
        try:
            return float(rating.get("ratingValue"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _review_count(cls, tree: HTMLParser, html: str, product: dict | None) -> int | None:
        if isinstance(product, dict):
            rating = product.get("aggregateRating")
            if isinstance(rating, dict):
                count = rating.get("reviewCount") or rating.get("ratingCount")
                parsed = cls._count_number(count)
                if parsed is not None:
                    return parsed
        for selector in (
            "[class*=review-count]", "[class*=reviews-count]",
            "[class*=reviewCount]", "[data-review-count]",
        ):
            for node in tree.css(selector):
                text = node.attributes.get("data-review-count") or node.text(" ", strip=True)
                count = cls._count_from_text(text)
                if count is not None:
                    return count
        return cls._count_from_text(html)

    @staticmethod
    def _count_number(value) -> int | None:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _count_from_text(cls, text: str | None) -> int | None:
        if not text:
            return None
        match = re.search(r"(\d[\d,\s]*)\s*(?:reviews?|ratings?)", text, re.I)
        if not match:
            return None
        return cls._count_number(match.group(1).replace(",", "").replace(" ", ""))

    @staticmethod
    def _jsonld_promotion_labels(product: dict | None) -> list[str]:
        if not isinstance(product, dict):
            return []
        offers = product.get("offers") or {}
        if isinstance(offers, dict):
            offers = [offers]
        labels: list[str] = []
        promo_keys = (
            "name", "description", "priceSpecification", "eligibleTransactionVolume",
            "discount", "discountCode", "coupon", "category", "availability",
        )
        for offer in (offers if isinstance(offers, list) else []):
            if not isinstance(offer, dict):
                continue
            for key in promo_keys:
                value = offer.get(key)
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, dict):
                    value = value.get("name") or value.get("description") or value.get("value")
                text = re.sub(r"\s+", " ", str(value or "").strip())
                if text and re.search(
                    r"sale|deal|discount|coupon|promo|save|off|bundle|"
                    r"free\s+(?:delivery|shipping)|delivery\s+included|"
                    r"shipping\s+included",
                    text,
                    re.I,
                ):
                    labels.append(text)
        seen: set[str] = set()
        return [label for label in labels if not (label in seen or seen.add(label))]

    @staticmethod
    def _category_from_url(url: str) -> str | None:
        parts = [
            unquote(part).replace("-", " ").strip().title()
            for part in urlsplit(url).path.split("/")
            if part and part.lower() not in {"vh_en", "p"}
        ]
        if len(parts) <= 1:
            return None
        return "/".join(parts[:-1][:3]) or None
