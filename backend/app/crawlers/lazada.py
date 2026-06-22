"""Lazada site crawler.

Site-level Lazada jobs start from a category/shop URL (for example
``/shop-makeup/``), discover PDP URLs from the rendered listing, then reuse the
existing on-demand Lazada PDP parser.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urljoin

from ..antiban import BlockedError
from ..ondemand.lazada import LazadaOnDemand
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("LAZADA_SITE_LIMIT", "60"))
_PDP_RE = re.compile(
    r'https?://[^"\']+/products/[^"\']+-i\d+(?:-s\d+)?\.html|'
    r'//[^"\']+/products/[^"\']+-i\d+(?:-s\d+)?\.html|'
    r'/products/[^"\']+-i\d+(?:-s\d+)?\.html',
    re.I,
)


class LazadaCrawler(BaseCrawler):
    platform = "lazada"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.ondemand = LazadaOnDemand()

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        entry = self.site.url or ""
        urls = self._discover_product_urls(entry)
        result.notes.append(
            f"Lazada listing 发现 {len(urls)} 个 PDP URL，本次抓取 {min(len(urls), self.limit)} 条"
        )
        if not urls:
            result.notes.append("⚠ Lazada listing 未渲染出商品 URL")
            return result

        ok = blocked = failed = 0
        seen: set[str] = set()
        for url in urls[: self.limit]:
            if url in seen:
                continue
            seen.add(url)
            try:
                item_id = self.ondemand.parse_item_id(url)
                row = self.ondemand.fetch_listing(item_id, url, proxy=self.proxy)
            except BlockedError:
                blocked += 1
                if blocked >= 3 and ok == 0:
                    raise
                continue
            except Exception as exc:
                failed += 1
                if failed <= 3:
                    result.notes.append(f"⚠ Lazada PDP 失败: {type(exc).__name__}: {exc}")
                continue
            if row:
                row["site"] = self.site.site
                row["brand"] = row.get("brand") or self.site.brand
                result.products.append(row)
                ok += 1
            self.sleep()
        result.notes.append(f"Lazada PDP 成功 {ok}/{len(seen)}，blocked={blocked}，failed={failed}")
        return result

    def _discover_product_urls(self, url: str) -> list[str]:
        html = self.ondemand._render(url, proxy=self.proxy)
        out: list[str] = []
        seen: set[str] = set()
        for raw in _PDP_RE.findall(html or ""):
            full = self._normalize_url(raw)
            if full and full not in seen:
                seen.add(full)
                out.append(full)
        return out

    def _normalize_url(self, raw: str) -> str:
        if raw.startswith("//"):
            return "https:" + raw
        return urljoin(self.site.url or "https://www.lazada.co.id/", raw)
