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
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlparse

from ..antiban import BlockedError
from ..crawl_diagnostics import FailureInfo, PARSE_NO_JSONLD, STAGE_PARSE
from ..db import session_scope
from ..frontier import mark_failed, mark_parsed
from .base import BaseCrawler, CrawlResult
from ..fetching import CrawlerFetcher

PAGE_SIZE = 48
# 默认不截断；COSTWAY_PAGES_PER_CAT / COSTWAY_MAX_ELAPSED_SEC 仅用于调试。
PAGES_PER_CAT = int(os.environ.get("COSTWAY_PAGES_PER_CAT", "999999"))
if PAGES_PER_CAT <= 0 or PAGES_PER_CAT == 50:
    PAGES_PER_CAT = 999999
COSTWAY_MAX_ELAPSED_SEC = int(os.environ.get("COSTWAY_MAX_ELAPSED_SEC", "0"))

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
            proxy_lease_ttl_sec=self._proxy_lease_ttl_sec(default=0),
            rate_interval_sec=self._rate_interval_sec(),
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

        # ---- 各分类下商品：分类之间可并发，单分类内仍按页顺序遇空停止 ----
        seen: set[str] = set()
        discovered: set[str] = set()
        page_failures: list[str] = []
        stopped_by_elapsed = False
        concurrency = self._listing_concurrency()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    self._crawl_category_products,
                    c,
                    currency,
                    new_skus,
                    best_skus,
                    started,
                ): c
                for c in cats
            }
            for future in as_completed(futures):
                c = futures[future]
                try:
                    rows, notes, complete, found = future.result()
                except BlockedError:
                    raise
                except Exception as exc:
                    msg = f"分类 {c.get('category_name')} 并发失败: {exc}"
                    page_failures.append(msg)
                    result.notes.append(msg)
                    continue
                result.notes.extend(notes)
                if not complete:
                    page_failures.extend(notes or [f"分类 {c.get('category_name')} 未完整"])
                    stopped_by_elapsed = stopped_by_elapsed or any(
                        "耗时上限" in note for note in notes
                    )
                discovered.update(found)
                for row in rows:
                    sku = row.get("sku")
                    if sku and sku not in seen:
                        seen.add(sku)
                        result.products.append(row)
                _persist_job_progress(
                    self.job_id,
                    products_count=len(result.products),
                    total_product_count=max(len(discovered), len(result.products)),
                )
        result.total_product_count = len(discovered)
        _persist_job_progress(
            self.job_id,
            products_count=len(result.products),
            total_product_count=result.total_product_count,
        )
        if page_failures or stopped_by_elapsed:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            detail = page_failures[0] if page_failures else "达到总耗时上限提前停止"
            result.coverage_reason = (
                f"Costway API 未完整翻完所有分类页：{detail}"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "重跑该站点，直到所有分类页完整返回。"
        result.notes.append(f"发现 {len(discovered)} 个去重商品，"
                            f"采集 {len(result.products)} 个去重 SKU"
                            f"（每分类 {PAGES_PER_CAT} 页，并发 {concurrency}）")
        return result

    def _crawl_category_products(
        self,
        category: dict,
        currency: str,
        new_skus: set,
        best_skus: set,
        started: float,
    ) -> tuple[list[dict], list[str], bool, set[str]]:
        fetcher = self.make_fetcher(
            kind="product",
            source="costway",
            fail_fast_blocked=True,
            retries=0,
            proxy_lease_ttl_sec=self._proxy_lease_ttl_sec(default=0),
            rate_interval_sec=self._rate_interval_sec(),
        )
        rows: list[dict] = []
        notes: list[str] = []
        discovered: set[str] = set()
        cid = category["category_id"]
        cname = category["category_name"]
        complete = True
        for page in range(1, PAGES_PER_CAT + 1):
            if (COSTWAY_MAX_ELAPSED_SEC > 0
                    and self._elapsed(started) >= COSTWAY_MAX_ELAPSED_SEC):
                notes.append(
                    f"达到 Costway 总耗时上限 {COSTWAY_MAX_ELAPSED_SEC}s，"
                    f"提前停止在分类 {cname} p{page}")
                complete = False
                break
            try:
                data = self._api(
                    fetcher, f"/api/products?category_id={cid}"
                    f"&page={page}&pagesize={PAGE_SIZE}")
            except BlockedError:
                raise
            except Exception as exc:
                notes.append(f"分类 {cname} p{page} 失败: {exc}")
                complete = False
                break
            items = self._items_from_payload(data)
            if not items:
                break
            for it in items:
                key = self._item_identity(it)
                if key:
                    discovered.add(key)
                row = self._map(it, cname, currency, new_skus, best_skus)
                if row:
                    rows.append(row)
            self.sleep()
        return rows, notes, complete, discovered

    def crawl_failed_products(self, urls: list[str]) -> CrawlResult:
        """Retry failed Costway product API URLs without rediscovering the site."""
        result = CrawlResult()
        urls = [u for u in urls if u]
        if not urls:
            result.notes.append("没有可重抓的 Costway URL")
            return result
        currency = _CURRENCY.get(self.site.country, "USD")
        concurrency = self._detail_concurrency()
        seen: set[str] = set()

        def fetch_one(url: str) -> tuple[str, list[dict], str | None]:
            fetcher = self.make_fetcher(
                kind="product",
                source="costway_failed_product_retry",
                fail_fast_blocked=True,
                retries=1,
                proxy_lease_ttl_sec=self._proxy_lease_ttl_sec(default=0),
                rate_interval_sec=self._rate_interval_sec(),
            )
            parsed = urlparse(url)
            if "/api/products" not in parsed.path and "/api/home-" not in parsed.path:
                info = FailureInfo(
                    PARSE_NO_JSONLD,
                    STAGE_PARSE,
                    "Costway 第一阶段失败商品重抓仅支持已记录的商品 API URL，暂不支持 PDP HTML 解析",
                    False,
                    "先重跑该商品所属 API 列表 URL，或补充 Costway PDP 详情解析",
                )
                with session_scope() as s:
                    mark_failed(s, site=self.site.site, url=url, failure=info,
                                retry_delay_sec=0)
                return url, [], info.detail
            res = fetcher.get(url, headers=self._headers(), timeout=20)
            self.guard(res.status or 0, url)
            if not res.ok:
                raise RuntimeError(f"HTTP {res.status or 0} fetching {url}")
            data = res.json() or {}
            items = self._items_from_payload(data)
            category_name = self._retry_category_name(url)
            rows = []
            for item in items:
                row = self._map(item, category_name, currency, set(), set())
                if row:
                    rows.append(row)
            with session_scope() as s:
                mark_parsed(s, site=self.site.site, url=url)
            return url, rows, None

        failures = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(fetch_one, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    _, rows, note = future.result()
                    if note:
                        failures += 1
                        result.notes.append(f"{url} 未完成: {note}")
                        continue
                    for row in rows:
                        sku = row.get("sku")
                        if sku and sku not in seen:
                            seen.add(sku)
                            result.products.append(row)
                except BlockedError:
                    raise
                except Exception as exc:
                    failures += 1
                    result.notes.append(f"{url} 重抓失败: {exc}")
        result.total_product_count = len(urls)
        result.notes.append(
            f"失败商品重抓 URL {len(urls)} 个，并发 {concurrency}，"
            f"产出 {len(result.products)} 个 SKU，失败 {failures} 个")
        return result

    def _items_from_payload(self, data: dict) -> list[dict]:
        result = data.get("result")
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
        if isinstance(result, dict):
            items = result.get("data") or result.get("product") or result.get("products")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    def _retry_category_name(self, url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        cid = (params.get("category_id") or [""])[0]
        return f"retry:{cid}" if cid else "failed_product_retry"

    def _item_identity(self, it: dict) -> str | None:
        for key in ("sku", "request_path", "url_path", "entity_id", "product_id"):
            value = it.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        return None

    def _detail_concurrency(self) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        if self._proxy_lease_ttl_sec(default=0) <= 0:
            return 1
        raw = config.get("detail_concurrency")
        try:
            return max(1, min(int(raw or 3), 8))
        except (TypeError, ValueError):
            return 3

    def _proxy_lease_ttl_sec(self, *, default: int = 300) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = config.get("proxy_lease_ttl_sec") or os.environ.get("COSTWAY_PROXY_LEASE_TTL_SEC")
        if raw in (None, "") and default <= 0:
            return 0
        try:
            return max(30, min(int(raw or default), 1800))
        except (TypeError, ValueError):
            return default

    def _listing_concurrency(self) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = (
            config.get("listing_concurrency")
            or config.get("detail_concurrency")
            or os.environ.get("COSTWAY_CONCURRENCY")
        )
        if self._proxy_lease_ttl_sec(default=0) <= 0:
            return 1
        if raw in (None, ""):
            return 8
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 1
        return max(1, min(value, 20))

    def _rate_interval_sec(self) -> float | None:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = config.get("rate_interval_sec") or os.environ.get("COSTWAY_RATE_INTERVAL_SEC")
        if raw in (None, "") and self._proxy_lease_ttl_sec(default=0) <= 0:
            return None
        try:
            value = float(raw if raw not in (None, "") else 0.2)
        except (TypeError, ValueError):
            value = 0.2
        return max(0.05, min(value, 2.0))

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


def _persist_job_progress(
    job_id: int | None,
    *,
    products_count: int | None = None,
    total_product_count: int | None = None,
) -> None:
    """Expose long Costway crawl progress without waiting for final upsert."""
    if not job_id:
        return
    try:
        from ..db import SessionLocal
        from ..models import CrawlJob
    except Exception:
        return
    db = SessionLocal()
    try:
        job = db.get(CrawlJob, job_id)
        if job is not None:
            if products_count is not None:
                job.products_count = max(0, int(products_count))
            if total_product_count is not None and total_product_count >= 0:
                job.total_product_count = int(total_product_count)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
