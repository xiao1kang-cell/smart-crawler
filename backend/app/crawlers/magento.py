"""Magento 采集器 —— 覆盖 Costway 欧洲站、VonHaus 等 Magento 架构站点。

Magento 站特征：sitemap 把分类页和商品页混在一起，商品页带 JSON-LD Product
（或 OpenGraph product meta）。本采集器：
  1. 从 robots.txt 发现 sitemap（Magento 常在 /media/sitemap/ 下，非默认路径）
  2. 递归展开 sitemap 索引，收集全部页面 URL
  3. 并发抓取页面、按 JSON-LD/OG 判别商品 —— 并发是关键，因为要扫大量分类页才
     凑够商品数；顺序逐页（旧 VonHaus 做法）会慢到生产任务超时

sites.yaml 可选字段：
  sitemap:        指定 sitemap 入口（跳过自动发现）
  product_match:  商品 URL 必含子串（配了能大幅减少要扫的页数）
  max_products:   显式调试上限（默认不截断）
  scan_cap:       显式调试扫描上限（默认不截断）
"""
from __future__ import annotations

import gzip
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from ..config import get_sites
from ..crawl_diagnostics import (
    FailureInfo,
    PARSE_NO_PRODUCT,
    STAGE_PARSE,
    classify_exception,
)
from ..db import session_scope
from ..frontier import mark_failed, mark_parsed
from .base import BaseCrawler, CrawlResult
from .generic import GenericCrawler

DEFAULT_LIMIT = int(os.environ.get("MAGENTO_LIMIT", "999999"))
DEFAULT_SCAN_CAP = int(os.environ.get("MAGENTO_SCAN_CAP", "0"))
MAX_ELAPSED_SEC = float(os.environ.get("MAGENTO_MAX_ELAPSED_SEC", "0"))
WORKERS = int(os.environ.get("MAGENTO_WORKERS", "8"))
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "IE": "EUR", "DE": "EUR",
             "IT": "EUR", "ES": "EUR", "FR": "EUR", "RO": "RON", "PT": "EUR",
             "NL": "EUR", "PL": "PLN"}
_SKIP_RE = re.compile(
    r"(blog|/article|/news|/help|/about|/contact|/customer|/checkout|"
    r"/catalogsearch|/privacy|/terms|\.(jpg|png|webp|pdf|css|js)(\?|$))", re.I)
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_IMG_LOC_RE = re.compile(r"<image:loc>\s*(.*?)\s*</image:loc>", re.S)
_IMG_TITLE_RE = re.compile(r"<image:title>\s*(.*?)\s*</image:title>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)


class MagentoCrawler(BaseCrawler):
    platform = "magento"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        self.base = site.url.rstrip("/")
        self.sitemap_hint = hints.get("sitemap")
        self.product_match = hints.get("product_match", "")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)
        self.scan_cap = int(hints.get("scan_cap", DEFAULT_SCAN_CAP))
        self._sitemap_meta: dict[str, dict] = {}

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _discover_sitemap(self, fetcher) -> str | None:
        """从 robots.txt 找 Sitemap，再退化到 Magento 常见路径。"""
        try:
            res = fetcher.get(self.base + "/robots.txt",
                              headers=self._headers(), timeout=20)
            m = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", res.text or "")
            if m:
                return m[0].strip()
        except BlockedError:
            raise
        except Exception:
            pass
        for p in ("/media/sitemap/sitemap.xml", "/sitemap.xml",
                  "/pub/media/sitemap.xml", "/sitemap/sitemap.xml"):
            try:
                res = fetcher.get(self.base + p,
                                  headers=self._headers(), timeout=20)
                if (res.status or 0) == 200 and "<loc>" in (res.text or ""):
                    return self.base + p
            except BlockedError:
                raise
            except Exception:
                continue
        return None

    def _sitemap_locs(self, fetcher, url: str, depth: int = 0) -> list[str]:
        """递归展开 sitemap（索引 / .gz / 普通），返回全部 <loc>。"""
        if depth > 3:
            return []
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=30)
            raw = res.content or b""
        except BlockedError:
            raise
        except Exception:
            return []
        try:
            text = (gzip.decompress(raw) if url.endswith(".gz")
                    else raw).decode("utf-8", "ignore")
        except (OSError, gzip.BadGzipFile):
            text = raw.decode("utf-8", "ignore")
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
        sub = [l for l in locs if l.endswith(".xml") or l.endswith(".xml.gz")]
        if sub and len(sub) == len(locs):            # 纯 sitemap 索引，递归
            out: list[str] = []
            for s in sub:
                out.extend(self._sitemap_locs(fetcher, s, depth + 1))
            return out
        self._remember_sitemap_meta(text)
        return locs

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        fetcher = self.make_fetcher(
            kind="product",
            source="magento",
            fail_fast_blocked=False,
            retries=2,
        )

        sitemap = self.sitemap_hint or self._discover_sitemap(fetcher)
        if not sitemap:
            result.notes.append("⚠ 未发现 sitemap —— 无法采集")
            return result
        result.notes.append(f"sitemap: {sitemap}")

        locs = self._sitemap_locs(fetcher, sitemap)
        cands = [u for u in locs
                 if not u.endswith((".xml", ".xml.gz"))
                 and not _SKIP_RE.search(u)
                 and (not self.product_match or self.product_match in u)]
        # 去重；再按商品 URL 特征排序。Costway 欧洲站的 sitemap-1-1
        # 以分类页开头，sitemap-1-2 起多为 `costway-*.html` 商品页；
        # 旧的随机打散在大 sitemap 上命中不稳定，且容易把 worker 拖很久。
        seen_u: set[str] = set()
        cands = [u for u in cands if not (u in seen_u or seen_u.add(u))]
        total = len(cands)
        cands.sort(key=_candidate_priority)
        scan_truncated = False
        if self.scan_cap > 0 and len(cands) > self.scan_cap:
            cands = cands[: self.scan_cap]
            scan_truncated = True
        result.notes.append(
            f"sitemap 候选页 {total} 个，排序后扫描上限 {len(cands)}，"
            f"目标商品 {self.limit}")
        if not cands:
            result.notes.append("⚠ 候选页为空")
            return result

        if self._prefer_sitemap_only():
            rows = []
            seen_skus: set[tuple[str, str]] = set()
            for row in (self._row_from_sitemap(u) for u in cands[: self.limit]):
                if not row:
                    continue
                key = (str(row.get("site") or ""), str(row.get("sku") or ""))
                if key in seen_skus:
                    continue
                seen_skus.add(key)
                rows.append(row)
            result.products.extend(rows)
            result.total_product_count = total
            coverage_pct = (len(rows) / total * 100) if total else 100.0
            if len(rows) < total and coverage_pct < 99.9:
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "sitemap"
                result.coverage_reason = (
                    f"Magento sitemap-only 本次被 limit/scan_cap 截断："
                    f"{len(rows)}/{total}"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "移除 MAGENTO_LIMIT / MAGENTO_SCAN_CAP 后重跑。"
                )
            result.notes.append(
                f"Costway Magento sitemap-only 产出 {len(rows)} 个商品"
                "（价格字段后续由 PDP/增量任务补齐）")
            return result

        # 并发抓取 + 判别。非 sitemap-only 站点的 sitemap 里常混有分类页，
        # 因此总量必须以实际判别出的商品页为准，不能直接用候选 URL 数。
        hit = 0
        scanned = 0
        elapsed_stop = False
        batch = WORKERS * 6
        # 把 fetcher 存为实例变量，供线程池内 _fetch_one 使用
        self._fetcher = fetcher
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i in range(0, len(cands), batch):
                if MAX_ELAPSED_SEC > 0 and time.monotonic() - started >= MAX_ELAPSED_SEC:
                    elapsed_stop = True
                    result.notes.append(
                        f"达到 MAGENTO_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                        f"提前返回（已扫描 {scanned}，命中 {hit}）")
                    break
                for row in pool.map(self._fetch_one, cands[i:i + batch]):
                    scanned += 1
                    if row:
                        hit += 1
                        if len(result.products) < self.limit:
                            result.products.append(row)
        result.total_product_count = hit
        if scan_truncated or elapsed_stop or len(result.products) < hit:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            reasons = []
            if scan_truncated:
                reasons.append(f"候选页被 MAGENTO_SCAN_CAP 截断 {len(cands)}/{total}")
            if elapsed_stop:
                reasons.append(
                    f"达到 MAGENTO_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s")
            if len(result.products) < hit:
                reasons.append(
                    f"发现 {hit} 个商品，实际入库 {len(result.products)} 个")
            result.coverage_reason = "；".join(reasons)
            result.coverage_retryable = True
            result.coverage_suggested_action = (
                "移除 MAGENTO_LIMIT / MAGENTO_SCAN_CAP 或放宽 "
                "MAGENTO_MAX_ELAPSED_SEC 后重跑。"
            )
        result.notes.append(
            f"扫描 {scanned} 页，命中商品 {hit} 个，本次入库 {len(result.products)} 个")
        return result

    def _fetch_one(self, url: str) -> dict | None:
        """抓单页并判别 —— 是商品返回 row，否则 None。"""
        try:
            res = self._fetcher.get(url, headers=self._headers(), timeout=25)
        except BlockedError:
            raise
        except Exception:
            return None
        if (res.status or 0) != 200:
            return None
        html = res.text or ""
        data = GenericCrawler._from_jsonld(html) or {}
        tree = HTMLParser(html)

        title = data.get("name") or self._meta(tree, "og:title")
        sale = data.get("price")
        if sale is None:
            sale = self._og_price(tree)
        if not title or sale is None:        # 无商品价格 → 分类/内容页
            return None

        self.snapshot(url.rstrip("/").split("/")[-1][:80], html)
        original = data.get("original_price") or sale
        imgs = data.get("images") or (
            [self._meta(tree, "og:image")] if self._meta(tree, "og:image") else [])
        slug = url.rstrip("/").split("/")[-1].split("?")[0][:80]
        return {
            "sku": data.get("sku") or slug,
            "spu": data.get("sku") or slug,
            "title": title.strip(),
            "description": data.get("description")
            or self._meta(tree, "og:description"),
            "image_urls": imgs,
            "category_path": data.get("category"),
            "sale_price": sale,
            "original_price": original,
            "currency": data.get("currency")
            or _CURRENCY.get(self.site.country, "USD"),
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

    def crawl_failed_products(self, urls: list[str]) -> CrawlResult:
        """Retry selected Magento product URLs without rediscovering sitemap."""
        result = CrawlResult()
        urls = [u for u in urls if u]
        if not urls:
            result.notes.append("没有可重抓的 Magento URL")
            return result

        seen: set[str] = set()
        failures = 0

        def _add_row(url: str, row: dict | None) -> None:
            nonlocal failures
            if not row:
                failures += 1
                info = FailureInfo(
                    PARSE_NO_PRODUCT,
                    STAGE_PARSE,
                    "Magento 失败 URL 重抓未解析到商品",
                    True,
                    "保留 URL 进入下一轮失败商品补抓，或检查该 URL 是否仍为商品页",
                )
                with session_scope() as s:
                    mark_failed(s, site=self.site.site, url=url, failure=info)
                return
            sku = str(row.get("sku") or "")
            if sku and sku not in seen:
                seen.add(sku)
                result.products.append(row)
            with session_scope() as s:
                mark_parsed(s, site=self.site.site, url=url)

        if self._prefer_sitemap_only():
            for url in urls:
                _add_row(url, self._row_from_sitemap(url))
        else:
            fetcher = self.make_fetcher(
                kind="product",
                source="magento_failed_product_retry",
                fail_fast_blocked=False,
                retries=1,
            )
            self._fetcher = fetcher
            with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, 8))) as pool:
                future_by_url = {pool.submit(self._fetch_one, url): url for url in urls}
                for future, url in future_by_url.items():
                    try:
                        _add_row(url, future.result())
                    except BlockedError:
                        raise
                    except Exception as exc:
                        failures += 1
                        info = classify_exception(exc)
                        with session_scope() as s:
                            mark_failed(s, site=self.site.site, url=url,
                                        failure=info)
                        result.notes.append(f"{url} 重抓失败: {exc}")

        result.total_product_count = len(urls)
        if failures:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "retry"
            result.coverage_reason = (
                f"Magento 失败 URL 补抓 {len(urls)} 个，仍失败 {failures} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "等待退避后继续失败商品补抓。"
        result.notes.append(
            f"Magento 失败 URL 补抓 {len(urls)} 个，"
            f"产出 {len(result.products)} 个 SKU，失败 {failures} 个"
        )
        return result

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    def _og_price(self, tree: HTMLParser):
        for sel in ('meta[property="product:price:amount"]',
                    'meta[property="og:price:amount"]',
                    '[itemprop="price"]'):
            node = tree.css_first(sel)
            if node:
                val = node.attributes.get("content") or node.text(strip=True)
                p = GenericCrawler._num(val)
                if p:
                    return p
        return None

    def _remember_sitemap_meta(self, text: str) -> None:
        if not hasattr(self, "_sitemap_meta"):
            self._sitemap_meta = {}
        for block in _URL_BLOCK_RE.findall(text or ""):
            loc_match = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S)
            if not loc_match:
                continue
            url = html.unescape(loc_match.group(1).strip())
            images = [html.unescape(x.strip()) for x in _IMG_LOC_RE.findall(block)]
            titles = [html.unescape(x.strip()) for x in _IMG_TITLE_RE.findall(block)]
            lastmod = _LASTMOD_RE.search(block)
            self._sitemap_meta[url] = {
                "images": images,
                "title": titles[0] if titles else None,
                "lastmod": html.unescape(lastmod.group(1).strip()) if lastmod else None,
            }

    def _prefer_sitemap_only(self) -> bool:
        return (self.site.site or "").startswith("costway_")

    def _row_from_sitemap(self, url: str) -> dict | None:
        meta = self._sitemap_meta.get(url) or {}
        title = meta.get("title") or _title_from_url(url)
        if not title:
            return None
        slug = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if slug.endswith(".html"):
            slug = slug[:-5]
        if not slug:
            return None
        return {
            "sku": slug[:180],
            "spu": slug[:180],
            "title": title,
            "image_urls": meta.get("images") or [],
            "category_path": _category_from_url(url),
            "currency": _CURRENCY.get(self.site.country, "USD"),
            "status": "on_sale",
            "brand": self.site.brand,
            "product_url": url,
            "site": self.site.site,
            "published_at": meta.get("lastmod"),
        }


def _candidate_priority(url: str) -> tuple[int, int, str]:
    path = re.sub(r"https?://[^/]+", "", url).lower()
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    depth = path.count("/")
    if basename.startswith("costway-") and basename.endswith(".html"):
        return (0, -depth, url)
    if basename.endswith(".html") and depth >= 3:
        return (1, -depth, url)
    if basename.endswith(".html"):
        return (2, -depth, url)
    return (3, -depth, url)


def _title_from_url(url: str) -> str | None:
    slug = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if slug.endswith(".html"):
        slug = slug[:-5]
    text = re.sub(r"[-_]+", " ", slug).strip()
    return text or None


def _category_from_url(url: str) -> str | None:
    path = re.sub(r"https?://[^/]+", "", url).strip("/")
    parts = path.split("/")[:-1]
    if not parts:
        return None
    return " / ".join(re.sub(r"[-_]+", " ", part).strip() for part in parts if part)
