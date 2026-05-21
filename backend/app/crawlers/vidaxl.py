"""Vidaxl 采集器 —— 荷贝，三路径合一。

Vidaxl 站点跑在 Salesforce Commerce Cloud，反爬重，`.com` 美国站封我方网段。
本采集器按优先级自动选路：

  路径1（首选）官方 Dropshipping API：设置环境变量
      VIDAXL_API_EMAIL / VIDAXL_API_TOKEN  → 走 b2b.vidaxl.com/api_customer/products
      （合法、完整、稳定，无需对抗反爬）
  路径2 欧洲国家站爬取：无 API 凭据时，解析 sitemap_index → 商品页 JSON-LD
  路径3 美国站住宅代理：vidaxl_us 站点 proxy_tier=residential，配 proxies.txt 后
      自动经住宅代理走路径2 的逻辑

详见 docs/风控策略评估.md 与 Vidaxl 研究结论。
"""
from __future__ import annotations

import gzip
import json
import os
import re

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

API_BASE = "https://b2b.vidaxl.com/api_customer/products"
STOREFRONT_LIMIT = int(os.environ.get("VIDAXL_LIMIT", "5000"))
API_PAGE = 500
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "IE": "EUR", "DE": "EUR",
             "IT": "EUR", "ES": "EUR", "FR": "EUR", "RO": "RON", "PT": "EUR",
             "NL": "EUR", "PL": "PLN"}


class VidaxlCrawler(BaseCrawler):
    platform = "vidaxl"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.currency = _CURRENCY.get(site.country, "EUR")
        self.api_email = os.environ.get("VIDAXL_API_EMAIL")
        self.api_token = os.environ.get("VIDAXL_API_TOKEN")
        self.limit = STOREFRONT_LIMIT

    def crawl(self) -> CrawlResult:
        if self.api_email and self.api_token:
            return self._crawl_api()
        return self._crawl_storefront()

    # ---------- 路径1：官方 Dropshipping API ----------
    def _crawl_api(self) -> CrawlResult:
        result = CrawlResult()
        sess = creq.Session(impersonate="chrome")
        sess.auth = (self.api_email, self.api_token)      # email + token
        offset, total = 0, 0
        while True:
            try:
                resp = sess.get(API_BASE, params={"limit": API_PAGE,
                                "offset": offset}, timeout=60)
                resp.raise_for_status()
                self.snapshot(f"api_offset{offset}", resp.text)
                items = resp.json()
            except Exception as exc:
                result.notes.append(f"API 调用失败 offset={offset}: {exc}")
                break
            if isinstance(items, dict):
                items = items.get("data") or items.get("products") or []
            if not items:
                break
            for it in items:
                row = self._map_api(it)
                if row:
                    result.products.append(row)
            total += len(items)
            offset += API_PAGE
            if len(items) < API_PAGE:
                break
            self.sleep()
        result.notes.append(f"路径1 官方 API：拉取 {total} 个商品")
        return result

    def _map_api(self, it: dict) -> dict | None:
        sku = it.get("sku") or it.get("code") or it.get("ean")
        if not sku:
            return None
        return {
            "sku": str(sku), "spu": str(it.get("sku") or sku),
            "title": it.get("title") or it.get("name"),
            "description": it.get("description"),
            "image_urls": it.get("images") or (
                [it.get("main_image")] if it.get("main_image") else []),
            "category_path": it.get("category"),
            "sale_price": _num(it.get("price") or it.get("b2b_price")),
            "original_price": _num(it.get("srp") or it.get("retail_price")
                                   or it.get("price")),
            "currency": it.get("currency") or self.currency,
            "gtin": it.get("ean") or it.get("gtin"),
            "inventory": it.get("stock"),
            "status": "on_sale" if (it.get("stock") or 0) else "out_of_stock",
            "brand": it.get("brand") or self.site.brand,
            "product_url": it.get("url"),
            "site": self.site.site,
        }

    # ---------- 路径2/3：storefront 爬取 ----------
    def _crawl_storefront(self) -> CrawlResult:
        result = CrawlResult()
        sess = creq.Session(impersonate="chrome")
        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}

        try:
            idx = sess.get(self.base + "/sitemap_index.xml", timeout=30)
            self.guard(idx.status_code, self.base)    # 熔断检查
            if idx.status_code != 200:
                result.notes.append(
                    f"⚠ sitemap_index 不可达（{idx.status_code}）—— "
                    f"{'美国站需住宅代理（路径3）' if self.site.country=='US' else '站点封锁'}")
                return result
            subs = re.findall(r"<loc>\s*(.*?)\s*</loc>", idx.text)
        except BlockedError:
            raise                              # 熔断 —— 传播到 runner
        except Exception as exc:
            result.notes.append(f"⚠ 站点不可达: {exc} —— 建议走路径1 官方 API")
            return result

        prod_sitemaps = [u for u in subs if "custom-product" in u]
        if not prod_sitemaps:
            # vidaxl_ca：sitemap_index 返回 200 但 body 是空 <sitemapindex/>。
            # 实测（2026-05-19）确认根因：VidaXL 已暂停加拿大站运营，
            # 页面显示 "We're pausing orders until further notice."，
            # 类别页 0 商品，Search-FAQ 替代 Search-Show —— 不是技术问题。
            # 等 VidaXL 重开加拿大站后，sitemap 会自动填充，此处代码无需改动。
            raise RuntimeError(
                f"sitemap_index 返回 200 但无 custom-product 子 sitemap "
                f"（{len(subs)} 个 <loc>，0 个匹配）。"
                f"已知原因（vidaxl_ca）：VidaXL 已暂停该市场运营，"
                f"类别页显示 'pausing orders until further notice'，"
                f"无商品可采集，需等业务重开。")
        urls: list[str] = []
        for sm in prod_sitemaps:
            if len(urls) >= self.limit:
                break
            try:
                raw = sess.get(sm, timeout=40).content
                xml = (gzip.decompress(raw) if sm.endswith(".gz")
                       else raw).decode("utf-8", "ignore")
                urls.extend(re.findall(r"<loc>\s*(.*?)\s*</loc>", xml))
            except Exception:
                continue
        targets = urls[: self.limit]
        result.notes.append(
            f"路径2 storefront：{len(prod_sitemaps)} 个商品 sitemap，"
            f"本次抓取 {len(targets)} 个商品")

        ok = 0
        for url in targets:
            try:
                html = sess.get(url, timeout=30).text
                self.snapshot(url.rstrip("/").split("/")[-1], html)
                row = self._parse_jsonld(html, url)
                if row:
                    result.products.append(row)
                    ok += 1
            except Exception as exc:
                result.notes.append(f"跳过 {url[:50]}: {exc}")
            self.sleep()
        result.notes.append(f"成功解析 {ok}/{len(targets)} 个商品")
        return result

    def _parse_jsonld(self, html: str, url: str) -> dict | None:
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            graph = (doc if isinstance(doc, list)
                     else doc.get("@graph", [doc]) if isinstance(doc, dict)
                     else [])
            # Vidaxl JSON-LD 用 ItemPage 包裹，商品在 mainEntity 里
            expanded = []
            for node in graph:
                expanded.append(node)
                if isinstance(node, dict) and isinstance(
                        node.get("mainEntity"), dict):
                    expanded.append(node["mainEntity"])
            for it in expanded:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type")
                if t != "Product" and not (isinstance(t, list) and "Product" in t):
                    continue
                offers = it.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                brand = it.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                rating = it.get("aggregateRating") or {}
                imgs = it.get("image")
                imgs = [imgs] if isinstance(imgs, str) else (imgs or [])
                avail = str(offers.get("availability", "")).lower()
                price = _num(offers.get("price"))
                return {
                    "sku": it.get("sku") or it.get("mpn")
                    or url.rstrip("/").split("/")[-1].replace(".html", ""),
                    "spu": it.get("sku") or it.get("mpn"),
                    "title": it.get("name"),
                    "description": it.get("description"),
                    "image_urls": imgs,
                    "sale_price": price, "original_price": price,
                    "currency": offers.get("priceCurrency") or self.currency,
                    "gtin": it.get("gtin13") or it.get("gtin"),
                    "mpn": it.get("mpn"),
                    "ratings": _num(rating.get("ratingValue")),
                    "review_count": _int(rating.get("reviewCount")),
                    "status": "out_of_stock" if "outofstock" in avail
                    else "on_sale",
                    "brand": brand or self.site.brand,
                    "product_url": url,
                    "site": self.site.site,
                }
        return None


def _num(v):
    if v is None:
        return None
    m = re.search(r"[\d.]+", str(v).replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
