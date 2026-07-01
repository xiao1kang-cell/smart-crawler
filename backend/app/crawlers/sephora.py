"""Sephora crawler.

Sephora has two very different surfaces in our current workspace:
- sephora.fr category pages are Akamai-gated; sitemap discovery is public and
  stable, so it is the default collection path.
- sephora.com is behind Akamai for category/PDP pages; sitemap discovery is
  public, but PDP fetches may still be blocked. In that case we raise
  BlockedError so the job is actionable instead of "unknown platform".
"""
from __future__ import annotations

import gzip
import html
import json
import os
import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult
from .generic import GenericCrawler

DEFAULT_LIMIT = int(os.environ.get("SEPHORA_LIMIT", "999999"))
_PRICE_RE = re.compile(r"\d[\d\s.,]*")
_REVIEW_RE = re.compile(r"(\d[\d\s.,]*)\s+avis", re.IGNORECASE)
_BLOCK_MARKS = (
    "Access Denied",
    "errors.edgesuite.net",
    "captcha",
    "sec-if-cpt-container",
    "behavioral-content",
    "akam/13/pixel",
    "You don't have permission to access",
)


class SephoraCrawler(BaseCrawler):
    platform = "sephora"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = self._base(site.url)
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": self.base + "/",
        }

    def crawl(self) -> CrawlResult:
        if ".fr" in urlparse(self.base).netloc and os.environ.get("SEPHORA_FR_HTML", "0") == "1":
            return self._crawl_fr()
        return self._crawl_us()

    def _crawl_fr(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="sephora_fr")
        res = fetcher.get(self.site.url or self.base, headers=self._headers(), timeout=35)
        if (res.status or 0) in {401, 403, 429} and (self.site.url or "").rstrip("/") != self.base:
            result.notes.append(
                f"{self.site.url} 返回 {res.status}，回退 Sephora 首页解析")
            res = fetcher.get(self.base + "/", headers=self._headers(), timeout=35)
        self.guard(res.status or 0, self.site.site)
        if self._blocked(res.text):
            raise BlockedError(f"{self.site.site} Sephora FR 页面被反爬拦截")
        self.snapshot("listing.html", res.text)

        tree = HTMLParser(res.text)
        seen: set[str] = set()
        for node in tree.css("[data-product-id]"):
            if len(result.products) >= self.limit:
                break
            row = self._parse_fr_tile(node)
            if not row or row["sku"] in seen:
                continue
            seen.add(row["sku"])
            result.products.append(row)

        result.notes.append(
            f"Sephora FR 页面卡片解析 {len(result.products)} 个商品")
        if not result.products:
            result.notes.append("⚠ 未找到 data-product-id 商品卡片，页面结构可能已变化")
        return result

    def _parse_fr_tile(self, node) -> dict | None:
        sku = node.attributes.get("data-product-id")
        if not sku:
            return None
        link = node.css_first('a[href*="/p/"]')
        href = link.attributes.get("href") if link else None
        if not href:
            return None
        product_url = urljoin(self.base, href)
        brand_node = node.css_first("h3")
        brand = self._text(brand_node) or self.site.brand
        spans = [self._text(x) for x in node.css("span")]
        spans = [x for x in spans if x and x.lower() not in {"découvrir"}]
        title = spans[0] if spans else None
        if not title:
            return None
        description = spans[1] if len(spans) > 1 else None
        variant = spans[2] if len(spans) > 2 else None

        price_node = node.css_first('[data-testid="productTile__txt__price"]')
        sale_price = self._price(self._text(price_node))
        original_price = self._original_price(node) or sale_price
        image = node.css_first("img")
        rating_node = node.css_first('[data-testid="productTile__txt__rating"]')
        rating_text = self._text(rating_node)

        return {
            "sku": sku,
            "spu": sku,
            "title": title,
            "description": description,
            "image_urls": [image.attributes.get("src")] if image and image.attributes.get("src") else [],
            "category_path": "Sephora",
            "sale_price": sale_price,
            "original_price": original_price,
            "currency": "EUR",
            "variant_id": variant,
            "attributes": {"variant": variant} if variant else {},
            "ratings": self._rating_from_tile(node),
            "review_count": self._review_count(rating_text),
            "status": "on_sale",
            "brand": brand,
            "product_url": product_url,
            "site": self.site.site,
        }

    def _crawl_us(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="sephora_us")
        urls = self._sitemap_product_urls(fetcher)
        result.total_product_count = len(urls)
        result.notes.append(f"Sephora US sitemap 发现 {len(urls)} 个商品 URL")
        if os.environ.get("SEPHORA_FETCH_PDP", "0") != "1":
            targets = urls[: self.limit]
            rows = [self._row_from_sitemap(url) for url in targets]
            result.products.extend(row for row in rows if row)
            result.notes.append(
                f"Sephora US sitemap-only 产出 {len(result.products)} 个商品"
                "（价格/评分字段后续由 PDP 增量补齐）")
            if len(targets) < len(urls):
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "sitemap"
                result.coverage_reason = (
                    f"Sephora sitemap 共 {len(urls)} 个商品 URL，"
                    f"本次只计划抓取 {len(targets)} 个"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = "调大 SEPHORA_LIMIT / max_products 后重跑。"
            return result

        blocked = 0
        for url in urls[: self.limit]:
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30)
            except Exception:
                continue
            if (res.status or 0) in {401, 403, 429} or self._blocked(res.text):
                blocked += 1
                if blocked >= 3:
                    raise BlockedError(
                        f"{self.site.site} Sephora US PDP 连续被拦截")
                continue
            row = (
                self._parse_fr_pdp(res.text, url)
                if ".fr" in urlparse(self.base).netloc
                else self._parse_us_pdp(res.text, url)
            )
            if row:
                result.products.append(row)
            self.sleep()
        if not result.products and blocked:
            raise BlockedError(f"{self.site.site} Sephora US PDP 被反爬拦截")
        return result

    def _sitemap_product_urls(self, fetcher) -> list[str]:
        sitemap_index = (
            f"{self.base}/sitemap_index.xml"
            if ".fr" in urlparse(self.base).netloc
            else f"{self.base}/sitemap.xml"
        )
        locs = self._sitemap_locs(fetcher, sitemap_index)
        product_maps = [u for u in locs if "product" in u.lower()]
        if ".com" in urlparse(self.base).netloc:
            product_maps = [
                u for u in product_maps
                if "_en-ca" not in u.lower() and "_fr-ca" not in u.lower()
            ]
        urls: list[str] = []
        for sm in product_maps:
            urls.extend(u for u in self._sitemap_locs(fetcher, sm)
                        if self._looks_like_product_url(u))
            if len(urls) >= self.limit * 3:
                break
        seen: set[str] = set()
        out: list[str] = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    def _sitemap_locs(self, fetcher, url: str) -> list[str]:
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=25)
            raw = res.content
        except Exception:
            return []
        try:
            text = gzip.decompress(raw).decode("utf-8", "ignore") if url.endswith(".gz") else raw.decode("utf-8", "ignore")
        except Exception:
            text = raw.decode("utf-8", "ignore")
        return re.findall(r"<loc>\s*(.*?)\s*</loc>", text)

    def _parse_us_pdp(self, text: str, url: str) -> dict | None:
        data = GenericCrawler._from_jsonld(text)
        if not data or not data.get("price"):
            hydrated = self._parse_us_hydration(text, url)
            if hydrated:
                return hydrated
        if not data:
            return None
        sku = data.get("sku") or self._slug(url)
        return {
            "sku": sku,
            "spu": sku,
            "title": data.get("name"),
            "description": data.get("description"),
            "image_urls": data.get("images") or [],
            "sale_price": data.get("price"),
            "original_price": data.get("price"),
            "currency": data.get("currency") or "USD",
            "ratings": data.get("rating"),
            "review_count": data.get("review_count"),
            "status": data.get("status", "on_sale"),
            "brand": data.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    def _parse_fr_pdp(self, text: str, url: str) -> dict | None:
        data = GenericCrawler._from_jsonld(text) or {}
        tree = HTMLParser(text or "")
        sku = self._fr_sku_from_url(url) or data.get("sku") or self._slug(url)
        sale_price = GenericCrawler._num(data.get("price"))
        if sale_price is None or sale_price <= 0:
            sale_price = self._price(self._first_fr_price_text(text))
        if sale_price is None or sale_price <= 0:
            return None
        original_price = GenericCrawler._num(data.get("original_price")) or sale_price
        title = (
            data.get("name")
            or self._meta(tree, "og:title")
            or self._title_from_slug(self._slug(url))
        )
        review_count = data.get("review_count")
        if review_count is None:
            review_count = self._fr_review_count(text)
        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title,
            "description": data.get("description") or self._meta(tree, "og:description"),
            "image_urls": data.get("images") or (
                [self._meta(tree, "og:image")] if self._meta(tree, "og:image") else []),
            "category_path": data.get("category") or "Sephora",
            "sale_price": sale_price,
            "original_price": original_price,
            "currency": data.get("currency") or "EUR",
            "ratings": data.get("rating"),
            "review_count": review_count if review_count is not None else 0,
            "status": data.get("status", "on_sale"),
            "brand": data.get("brand") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    def _parse_us_hydration(self, text: str, url: str) -> dict | None:
        blob = html.unescape(text or "")
        blob = (
            blob.replace('\\"', '"')
            .replace("\\/", "/")
            .replace("\\u002F", "/")
            .replace("\\u0026", "&")
        )
        sku = self._sku_from_url(url) or self._slug(url)
        windows = self._hydration_windows(blob, sku)
        for window in windows:
            sale_price = (
                self._json_price(window, "salePrice")
                or self._json_price(window, "currentPrice")
                or self._json_price(window, "valuePrice")
                or self._json_price(window, "regularPrice")
                or self._json_price(window, "listPrice")
            )
            original_price = (
                self._json_price(window, "regularPrice")
                or self._json_price(window, "listPrice")
                or sale_price
            )
            title = (
                self._json_text(window, "displayName")
                or self._json_text(window, "productName")
                or self._json_text(window, "name")
                or self._title_from_slug(self._slug(url))
            )
            if not sale_price or not title:
                continue
            sku_id = self._json_text(window, "skuId")
            image = (
                self._json_text(window, "imageUrl")
                or self._json_text(window, "heroImage")
                or self._json_text(window, "productImage")
            )
            attrs = {"source": "pdp_hydration"}
            if sku_id:
                attrs["sku_id"] = sku_id
            return {
                "sku": sku,
                "spu": sku,
                "title": title,
                "description": self._json_text(window, "description"),
                "image_urls": [image] if image else [],
                "category_path": "Sephora",
                "sale_price": sale_price,
                "original_price": original_price,
                "currency": "USD",
                "variant_id": sku_id,
                "attributes": attrs,
                "ratings": self._json_float(
                    window, "rating", "averageRating", "reviewAverageRating"),
                "review_count": self._json_int(
                    window,
                    "reviewCount",
                    "reviewsCount",
                    "numReviews",
                    "reviews",
                ) or 0,
                "status": "on_sale",
                "brand": self._json_text(window, "brandName") or self.site.brand,
                "product_url": url,
                "site": self.site.site,
            }
        return None

    @staticmethod
    def _hydration_windows(blob: str, sku: str | None) -> list[str]:
        windows: list[str] = []
        if sku:
            for match in re.finditer(re.escape(sku), blob, re.I):
                start = max(0, match.start() - 8000)
                end = min(len(blob), match.end() + 12000)
                windows.append(blob[start:end])
                if len(windows) >= 5:
                    break
        if not windows:
            windows.append(blob[:200000])
        return windows

    @staticmethod
    def _json_text(blob: str, key: str) -> str | None:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', blob)
        if not match:
            return None
        text = match.group(1)
        try:
            text = json.loads(f'"{text}"')
        except Exception:
            pass
        text = html.unescape(str(text))
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @classmethod
    def _json_price(cls, blob: str, key: str) -> float | None:
        return cls._price(cls._json_text(blob, key))

    @staticmethod
    def _json_float(blob: str, *keys: str) -> float | None:
        for key in keys:
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
                blob,
            )
            if match:
                return round(float(match.group(1)), 2)
        return None

    @staticmethod
    def _json_int(blob: str, *keys: str) -> int | None:
        for key in keys:
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*"?(\d[\d,.\s]*)"?',
                blob,
            )
            if match:
                return int(re.sub(r"\D", "", match.group(1)) or "0")
        return None

    def _row_from_sitemap(self, url: str) -> dict | None:
        parsed = urlparse(url)
        if not self._looks_like_product_url(url):
            return None
        slug = self._slug(url)
        if not slug:
            return None
        sku = self._sku_from_url(url) or slug
        title = self._title_from_slug(slug)
        brand = self._brand_from_slug(slug) or self.site.brand
        is_fr = ".fr" in urlparse(self.base).netloc
        return {
            "sku": sku,
            "spu": sku,
            "title": title,
            "description": None,
            "image_urls": [],
            "category_path": "Sephora",
            "sale_price": None,
            "original_price": None,
            "currency": "EUR" if is_fr else "USD",
            "ratings": None,
            "review_count": None,
            "status": "on_sale",
            "brand": brand,
            "product_url": url,
            "site": self.site.site,
            "attributes": {"source": "sitemap"},
            "_skip_price_history_if_no_price": True,
        }

    def _original_price(self, node):
        texts = [self._text(x) for x in node.css("p")]
        for i, text in enumerate(texts):
            if text and "prix d'origine" in text.lower() and i + 1 < len(texts):
                price = self._price(texts[i + 1])
                if price:
                    return price
        return None

    @staticmethod
    def _base(url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _text(node) -> str | None:
        if not node:
            return None
        text = html.unescape(node.text(separator=" ", strip=True))
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (
            tree.css_first(f'meta[property="{prop}"]')
            or tree.css_first(f'meta[name="{prop}"]')
        )
        return node.attributes.get("content") if node else None

    @staticmethod
    def _price(value: str | None) -> float | None:
        if not value:
            return None
        match = _PRICE_RE.search(value.replace("\xa0", " "))
        if not match:
            return None
        text = match.group().replace(" ", "").replace(",", ".")
        try:
            return round(float(text), 2)
        except ValueError:
            return None

    @staticmethod
    def _review_count(value: str | None) -> int | None:
        if not value:
            return None
        match = _REVIEW_RE.search(value.replace("\xa0", " "))
        if not match:
            return None
        return int(re.sub(r"\D", "", match.group(1)) or "0")

    @staticmethod
    def _fr_review_count(value: str | None) -> int | None:
        if not value:
            return None
        text = html.unescape(value.replace("\xa0", " "))
        for pattern in (
            r"\((\d[\d\s.]*)\s+avis sur le produit\)",
            r"(\d[\d\s.]*)\s+avis",
        ):
            match = re.search(pattern, text, re.I)
            if match:
                return int(re.sub(r"\D", "", match.group(1)) or "0")
        return None

    @staticmethod
    def _first_fr_price_text(value: str | None) -> str | None:
        if not value:
            return None
        text = html.unescape(value.replace("\xa0", " "))
        match = re.search(r"\d[\d\s.]*,\d{2}\s*€", text)
        return match.group(0) if match else None

    @staticmethod
    def _rating_from_tile(node) -> float | None:
        vals = []
        for star in node.css('[style*="--fillRatio"]'):
            style = star.attributes.get("style", "")
            match = re.search(r"--fillRatio\s*:\s*([0-9.]+)", style)
            if match:
                vals.append(float(match.group(1)))
        return round(sum(vals), 2) if vals else None

    @staticmethod
    def _slug(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0][:80]

    @staticmethod
    def _sku_from_url(url: str) -> str | None:
        match = re.search(r"-(P\d+)(?:\.html)?(?:[/?#]|$)", url, re.I)
        return match.group(1).upper() if match else None

    @staticmethod
    def _fr_sku_from_url(url: str) -> str | None:
        match = re.search(r"-(\d{4,})(?:\.html)?(?:[/?#]|$)", url)
        return match.group(1) if match else None

    @staticmethod
    def _looks_like_product_url(url: str) -> bool:
        path = urlparse(url).path
        return "/product/" in path or "/p/" in path or bool(re.search(r"-P\d+", path, re.I))

    @classmethod
    def _title_from_slug(cls, slug: str) -> str:
        text = re.sub(r"-P\d+$", "", slug, flags=re.I)
        text = text.replace("-", " ").strip()
        return text.title() if text else slug

    @classmethod
    def _brand_from_slug(cls, slug: str) -> str | None:
        text = re.sub(r"-P\d+$", "", slug, flags=re.I)
        parts = [p for p in text.split("-") if p]
        if not parts:
            return None
        stop = {
            "mini", "full", "large", "small", "the", "a", "an", "new",
            "set", "collection", "cream", "serum", "mask", "cleanser",
            "lip", "eye", "eau", "de", "parfum", "conditioner", "shampoo",
        }
        brand_parts: list[str] = []
        for part in parts[:5]:
            if brand_parts and part.lower() in stop:
                break
            brand_parts.append(part)
            if len(brand_parts) >= 3:
                break
        brand = " ".join(brand_parts).strip()
        return brand.title() if brand else None

    @staticmethod
    def _blocked(text: str | None) -> bool:
        body = text or ""
        return any(mark.lower() in body.lower() for mark in _BLOCK_MARKS)
