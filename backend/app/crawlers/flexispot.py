"""Flexispot 采集器 —— 乐歌，React SPA。

Flexispot 的商品 API `POST /sapi/mall-item/item/detail` 需要浏览器会话
（直接 POST 返回 401）。故用 Playwright 先打开站点建立会话，再用
浏览器上下文的 request 调 API —— 拿到结构化 JSON。

商品 URL（urlKey）来自 sitemap 的根级单段 slug。
"""
from __future__ import annotations

import json
import os
import re
import time

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("FLEXISPOT_LIMIT", "999999"))
MAX_ELAPSED_SEC = float(os.environ.get("FLEXISPOT_MAX_ELAPSED_SEC", "0"))
REQUEST_TIMEOUT = int(os.environ.get("FLEXISPOT_REQUEST_TIMEOUT", "12"))
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "DE": "EUR", "IT": "EUR",
             "ES": "EUR", "FR": "EUR", "NL": "EUR", "PL": "PLN"}
_EXCLUDE = ("spine-care-center", "/category", "/blog", "undefined", "/cart",
            "/account", "/search", "/compare", "/login")


class FlexispotCrawler(BaseCrawler):
    platform = "flexispot"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)
        self.currency = _CURRENCY.get(site.country, "USD")

    def _product_slugs(self) -> list[str]:
        """从 sitemap 取根级单段商品 slug（通过统一 fetcher，计 api_calls）。"""
        fetcher = self.make_fetcher(kind="sitemap", source="flexispot")
        sitemap_url = self.base + "/sitemap.xml"
        try:
            res = fetcher.get(sitemap_url)
            xml = res.text if res.ok else ""
        except Exception:
            return []
        slugs, seen = [], set()
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml):
            if any(x in loc for x in _EXCLUDE):
                continue
            m = re.match(r"https?://[^/]+/([^/?#]+)/?$", loc)
            if not m:
                continue
            slug = m.group(1)
            # 商品 slug 通常是多词连字符
            if slug.count("-") >= 2 and slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        return slugs

    def _do_bootstrap(self) -> tuple[str | None, str | None]:
        """用 Playwright 打开首页，截获 SPA 的 /sapi/ 请求拿 Bearer token。

        逻辑原样保留，抽为独立方法供 count_browser_fetch 包裹。
        """
        from playwright.sync_api import sync_playwright

        captured: dict = {}
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                user_agent=self.ua(),
                proxy={"server": self.proxy} if self.proxy else None)
            page = ctx.new_page()

            def on_req(req):
                if "/sapi/mall-item/" in req.url and "authorization" not in captured:
                    h = req.headers
                    if h.get("authorization"):
                        captured["authorization"] = h["authorization"]
                        captured["appid"] = h.get("appid", "10001")

            page.on("request", on_req)
            try:
                page.goto(self.base + "/standing-desks",
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
            browser.close()
        return captured.get("authorization"), captured.get("appid")

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        slugs = self._product_slugs()
        targets = slugs[: self.limit]
        result.total_product_count = len(slugs)
        result.notes.append(
            f"sitemap 发现 {len(slugs)} 个商品 slug，本次抓取 {len(targets)} 个")
        if len(targets) < len(slugs):
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            result.coverage_reason = (
                f"Flexispot 本次全量分母 {len(slugs)}，"
                f"实际计划抓取 {len(targets)}，已被 limit 截断"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 FLEXISPOT_LIMIT 后重跑。"
        if not targets:
            result.notes.append("⚠ 未发现商品 slug")
            return result

        # 用 count_browser_fetch 包裹 _do_bootstrap：成功拿到 token 计 1 次 browser_open
        token, appid = self.count_browser_fetch(
            self._do_bootstrap,
            success=lambda r: bool(r and r[0]),
        )
        if not token:
            result.notes.append("⚠ 未能获取 Flexispot API token")
            return result

        # 拿到 token 后用统一 fetcher 批量调 API（计 api_calls）
        fetcher = self.make_fetcher(kind="product", source="flexispot")
        headers = {
            "appid": appid or "10001", "authorization": token,
            "site": self.site.country, "role": "0",
            "x-requested-with": "XMLHttpRequest",
            "content-type": "application/json;charset=UTF-8",
            "referer": self.base + "/",
        }
        api = self.base + "/sapi/mall-item/item/detail"
        ok = 0
        for slug in targets:
            if MAX_ELAPSED_SEC > 0 and time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 FLEXISPOT_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回已解析结果（ok={ok}/{len(targets)}）")
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"Flexispot 商品详情解析达到耗时上限，已解析 {ok}/{len(targets)}"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "放宽 FLEXISPOT_MAX_ELAPSED_SEC 或拆分失败商品重抓。"
                )
                break
            try:
                res = fetcher.post(api, data=json.dumps({"urlKey": slug}),
                                   headers=headers, timeout=REQUEST_TIMEOUT)
                if not res.ok:
                    continue
                self.snapshot(slug, res.text)
                data = (res.json() or {}).get("data") or {}
                rows = self._parse(data, slug)
                if rows:
                    result.products.extend(rows)
                    ok += 1
            except Exception as exc:
                result.notes.append(f"跳过 {slug}: {exc}")
            self.sleep()

        result.notes.append(f"成功解析 {ok}/{len(targets)} 个商品")
        return result

    def _parse(self, data: dict, slug: str) -> list[dict]:
        ir = data.get("itemRenderTO") or {}
        item_name = ir.get("itemName")
        spu = str(ir.get("id") or ir.get("itemId") or slug)
        main_img = ir.get("mainImage")
        cats = data.get("frontCategoryList") or []
        cat_path = "/".join(c.get("name") for c in cats
                            if isinstance(c, dict) and c.get("name")) or None

        rows = []
        for sku in data.get("shopSkuList") or []:
            if not isinstance(sku, dict):
                continue
            sale = (sku.get("salePrc") or {}).get("value")
            orig = (sku.get("originalPrc") or {}).get("value") or sale
            code = sku.get("skuCode") or str(sku.get("skuId") or "")
            if not code or sale is None:
                continue
            out = sku.get("outOfStock") or sku.get("skuStatusDict") != "ENABLED"
            rows.append({
                "sku": code,
                "spu": spu,
                "title": sku.get("name") or item_name,
                "image_urls": [i for i in (sku.get("image"), main_img) if i],
                "category_path": cat_path,
                "sale_price": sale,
                "original_price": orig,
                "currency": self.currency,
                "status": "out_of_stock" if out else "on_sale",
                "product_url": f"{self.base}/{slug}",
                "product_type": ir.get("itemCode"),
                "site": self.site.site,
                "brand": self.site.brand,
            })
        return rows
