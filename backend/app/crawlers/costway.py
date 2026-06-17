"""Costway 采集器 —— 豪雅，Vue 3 SPA。

实地验证（2026-05-16）：Costway 的 JSON API 用 curl_cffi（chrome 指纹）
即可直连拿到结构化数据，无需 Playwright。若运行网络被 ASN 级封锁，
设置环境变量 RESIDENTIAL_PROXY 即可经住宅代理转发（BaseCrawler 已支持）。

API：
  GET /api/category                                  → 分类树
  GET /api/products?category_id={id}&page={N}&pagesize=48
  GET /api/home-newarrivals / /api/home-bestseller   → 新品 / 热销
  GET /api/spike_list                                → 限时闪购（促销）
"""
from __future__ import annotations

import os
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult
from ..fetching import CrawlerFetcher, FetchResult

PAGE_SIZE = 48
# 客户反馈 Costway CA 实际 >1W 商品，调高每分类页数上限
# 230 分类 × 30 页 × 48 = 33 万 cap，实际按各分类 total 提前 break
PAGES_PER_CAT = int(os.environ.get("COSTWAY_PAGES_PER_CAT", "30"))
COSTWAY_MAX_ELAPSED_SEC = int(os.environ.get("COSTWAY_MAX_ELAPSED_SEC", "240"))

_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "DE": "EUR", "IT": "EUR",
             "ES": "EUR", "FR": "EUR", "NL": "EUR", "PL": "PLN"}


class CostwayCrawler(BaseCrawler):
    platform = "vue_spa"

    def _headers(self) -> dict:
        """构造请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "application/json",
            "Referer": self.site.url,
        }

    def _api(self, fetcher: CrawlerFetcher, path: str) -> dict:
        url = self.site.url.rstrip("/") + path
        res = fetcher.get(url, headers=self._headers(), timeout=15)
        self.guard(res.status or 0, path)        # 熔断检查
        if not res.ok:
            raise RuntimeError(f"HTTP {res.status or 0} fetching {url}")
        self.snapshot(path, res.text)            # 原始响应归档
        return res.json() or {}

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        fetcher = self.make_fetcher(
            kind="product",
            source="costway",
            fail_fast_blocked=True,
            retries=0,
        )
        currency = _CURRENCY.get(self.site.country, "USD")

        # ---- 新品 / 热销标记 ----
        new_skus = self._sku_set(fetcher, "/api/home-newarrivals")
        best_skus = self._sku_set(fetcher, "/api/home-bestseller")
        result.notes.append(f"新品 {len(new_skus)} 款 / 热销 {len(best_skus)} 款")

        # ---- 分类树 ----
        cats = []
        try:
            for c in self._api(fetcher, "/api/category").get("result", []):
                cats.append({
                    "site": self.site.site,
                    "category_id": str(c.get("entity_id")),
                    "category_name": c.get("name"),
                    "category_url": self.site.url.rstrip("/") + "/"
                    + (c.get("url_path") or ""),
                    "parent_id": str(c.get("parent_id")) if c.get("parent_id") else None,
                    "level": c.get("level"),
                    "product_count": None,
                })
        except Exception as exc:
            result.notes.append(f"分类采集失败: {exc}")
        result.categories = cats

        # ---- 各分类下商品 ----
        seen: set[str] = set()
        for c in cats:
            if self._elapsed(started) >= COSTWAY_MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 Costway 总耗时上限 {COSTWAY_MAX_ELAPSED_SEC}s，"
                    f"提前停止，已采集 {len(result.products)} 个 SKU")
                break
            cid = c["category_id"]
            cname = c["category_name"]
            for page in range(1, PAGES_PER_CAT + 1):
                if self._elapsed(started) >= COSTWAY_MAX_ELAPSED_SEC:
                    result.notes.append(
                        f"达到 Costway 总耗时上限 {COSTWAY_MAX_ELAPSED_SEC}s，"
                        f"提前停止在分类 {cname} p{page}")
                    break
                try:
                    data = self._api(
                        fetcher, f"/api/products?category_id={cid}"
                        f"&page={page}&pagesize={PAGE_SIZE}")
                except BlockedError:
                    raise                          # 熔断 —— 传播到 runner
                except Exception as exc:
                    result.notes.append(f"分类 {cname} p{page} 失败: {exc}")
                    break
                items = (data.get("result") or {}).get("data") or []
                if not items:
                    break
                for it in items:
                    row = self._map(it, cname, currency, new_skus, best_skus)
                    if row and row["sku"] not in seen:
                        seen.add(row["sku"])
                        result.products.append(row)
                self.sleep()
        result.notes.append(f"采集 {len(result.products)} 个去重 SKU"
                            f"（每分类 {PAGES_PER_CAT} 页）")
        return result

    def _sku_set(self, fetcher: CrawlerFetcher, path: str) -> set[str]:
        try:
            res = self._api(fetcher, path).get("result")
            items = res if isinstance(res, list) else (res or {}).get("product", [])
            return {str(x.get("sku")) for x in items if x.get("sku")}
        except BlockedError:
            raise
        except Exception:
            return set()

    def _map(self, it: dict, cname: str, currency: str,
             new_skus: set, best_skus: set) -> dict | None:
        sku = it.get("sku")
        if not sku:
            return None
        price = it.get("price") or {}
        original = self._f(price.get("price"))
        special = self._f(price.get("special_price"))
        sale = special if special and special > 0 else original
        if original is None:
            original = sale

        images = []
        imgs = it.get("images") or {}
        for k in ("baseImage", "small_image"):
            v = imgs.get(k)
            if v and v not in images:
                images.append(v)

        rating = it.get("rating") or {}
        inv = (it.get("inventory") or {}).get("qty")
        tag = it.get("product_tag") or it.get("label") or None
        is_new = str(sku) in new_skus
        is_best = str(sku) in best_skus or (tag == "Bestseller")
        path = it.get("request_path") or it.get("url_path") or ""

        return {
            "sku": str(sku),
            "spu": str(it.get("entity_id") or it.get("product_id")),
            "title": it.get("name"),
            "image_urls": images,
            "category_path": cname,
            "sale_price": sale,
            "original_price": original,
            "currency": currency,
            "ratings": rating.get("score"),
            "review_count": rating.get("count"),
            "status": "on_sale" if (inv is None or inv > 0) else "out_of_stock",
            "inventory": str(inv) if inv is not None else None,
            "has_video": bool(it.get("has_video")),
            "label": "NEW" if is_new else (tag or None),
            "product_url": self.site.url.rstrip("/") + "/" + path,
            "product_type": it.get("type_id"),
            "site": self.site.site,
            "brand": self.site.brand,
            "is_new": is_new,
            "is_bestseller": is_best,
        }

    @staticmethod
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _elapsed(started: float) -> float:
        return time.monotonic() - started
