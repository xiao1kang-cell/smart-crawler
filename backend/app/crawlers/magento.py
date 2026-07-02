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
import json
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
    r"/catalogsearch|/privacy|/terms|newsletter|subscribe|recall|"
    r"right-of-withdrawal|privacy-policy|terms-and-conditions|"
    r"\.(jpg|png|webp|pdf|css|js)(\?|$))", re.I)
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_IMG_LOC_RE = re.compile(r"<image:loc>\s*(.*?)\s*</image:loc>", re.S)
_IMG_TITLE_RE = re.compile(r"<image:title>\s*(.*?)\s*</image:title>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)
_COSTWAY_CATEGORY_BASENAMES = {
    "animalerie",
    "appliances",
    "arredamento",
    "articoli-per-animali",
    "baby-kind",
    "ba-o",
    "badezimmer",
    "bagno",
    "bambini-e-neonati",
    "baby-kids",
    "bath",
    "canopies-gazebos",
    "bebes-et-tout-petits",
    "cuisine-et-salle-a-manger",
    "cocina",
    "decor",
    "decoracion",
    "decorations",
    "deportes-y-aire-libre",
    "dekoration",
    "decorazione",
    "electromenagers",
    "electrodomesticos",
    "elettrodomestici",
    "garten",
    "giochi-e-giocattoli",
    "furniture",
    "haushaltsgerate",
    "haustierbedarf",
    "health-beauty",
    "jardin-et-pelouses",
    "jardin",
    "jeux-et-jouets",
    "juguetes-y-aficiones",
    "kitchen",
    "kuche",
    "infantil",
    "mascotas",
    "meubles",
    "mobel",
    "muebles",
    "muebles-exteriores",
    "oficina",
    "others",
    "outdoor-e-giardino",
    "outdoor",
    "pets",
    "pflege-kosmetik",
    "sala-da-pranzo-e-cucina",
    "salle-de-bain",
    "sante-et-beaute",
    "salud-y-belleza",
    "salute-e-bellezza",
    "spielzeuge-hobbys",
    "sports",
    "toys-hobbies",
    "sport-e-tempo-libero",
    "sport-freizeit",
    "sports-et-plein-air",
    "terraza-y-jardin",
}


class MagentoCrawler(BaseCrawler):
    platform = "magento"

    def __init__(self, site):
        super().__init__(site)
        hints = next((c for c in get_sites() if c["site"] == site.site), {})
        crawler_config = site.crawler_config if isinstance(site.crawler_config, dict) else {}
        self.base = site.url.rstrip("/")
        self.sitemap_hint = crawler_config.get("sitemap") or hints.get("sitemap")
        self.product_match = crawler_config.get("product_match") or hints.get("product_match", "")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, honor_persisted=False)
        self.scan_cap = int(crawler_config.get("scan_cap") or hints.get("scan_cap", DEFAULT_SCAN_CAP))
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
            total_product_rows = 0
            for row in (self._row_from_sitemap(u) for u in cands):
                if not row:
                    continue
                key = (str(row.get("site") or ""), str(row.get("sku") or ""))
                if key in seen_skus:
                    continue
                seen_skus.add(key)
                total_product_rows += 1
                if len(rows) < self.limit:
                    rows.append(row)
            result.products.extend(rows)
            result.total_product_count = total_product_rows
            if scan_truncated or len(rows) < total_product_rows:
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "sitemap"
                reasons = []
                if scan_truncated:
                    reasons.append(f"候选页被 MAGENTO_SCAN_CAP 截断 {len(cands)}/{total}")
                if len(rows) < total_product_rows:
                    reasons.append(
                        f"发现 {total_product_rows} 个商品，实际入库 {len(rows)} 个")
                result.coverage_reason = (
                    "；".join(reasons)
                    or "Magento sitemap-only 本次未能证明商品全量覆盖"
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
        final_url = (res.final_url or url).split("#", 1)[0]
        if final_url.rstrip("/") != url.split("#", 1)[0].rstrip("/") and not data.get("sku"):
            return None
        tree = HTMLParser(html)

        title = data.get("name") or self._meta(tree, "og:title")
        sale = GenericCrawler._num(data.get("price"))
        if sale is None or sale <= 0:
            sale = self._dom_price(tree) or self._og_price(tree)
        if not title or sale is None:        # 无商品价格 → 分类/内容页
            return None

        self.snapshot(url.rstrip("/").split("/")[-1][:80], html)
        original = GenericCrawler._num(data.get("original_price")) or sale
        if original is None or original <= 0:
            original = sale
        imgs = data.get("images") or (
            [self._meta(tree, "og:image")] if self._meta(tree, "og:image") else [])
        slug = url.rstrip("/").split("/")[-1].split("?")[0][:80]
        category_path = _first_valid_category(
            data.get("category"),
            _jsonld_breadcrumb(tree),
            _dom_breadcrumb(tree),
            _category_from_title_fallback(title),
        )
        review_count = data.get("review_count")
        rating = data.get("rating")
        dom_review_count, dom_rating = _dom_review_details(tree, html)
        if review_count is None:
            review_count = dom_review_count
        if review_count is None:
            review_count = 0
        if rating is None:
            rating = dom_rating
        return {
            "sku": data.get("sku") or slug,
            "spu": data.get("sku") or slug,
            "title": title.strip(),
            "description": data.get("description")
            or self._meta(tree, "og:description"),
            "image_urls": imgs,
            "category_path": category_path,
            "sale_price": sale,
            "original_price": original,
            "currency": data.get("currency")
            or _CURRENCY.get(self.site.country, "USD"),
            "ratings": rating,
            "review_count": review_count,
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
                    row = future.result()
                    if row is None and self._prefer_sitemap_only():
                        row = self._row_from_sitemap(url)
                    _add_row(url, row)
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

    def _dom_price(self, tree: HTMLParser):
        for sel in (
            '[data-price-amount]',
            'meta[x-itemprop="price"]',
            'meta[itemprop="price"]',
            '[content][x-itemprop="price"]',
            '[content][itemprop="price"]',
        ):
            for node in tree.css(sel):
                val = (
                    node.attributes.get("data-price-amount")
                    or node.attributes.get("content")
                    or node.text(strip=True)
                )
                price = GenericCrawler._num(val)
                if price and price > 0:
                    return price
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
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = config.get("sitemap_only") or os.environ.get("MAGENTO_SITEMAP_ONLY")
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def _row_from_sitemap(self, url: str) -> dict | None:
        if _looks_like_category_url(url):
            return None
        meta = self._sitemap_meta.get(url) or {}
        title = meta.get("title") or _title_from_url(url)
        if not title:
            return None
        slug = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if slug.endswith(".html"):
            slug = slug[:-5]
        if not slug:
            return None
        if _slug_title_equal(slug, title):
            return None
        return {
            "sku": slug[:180],
            "spu": slug[:180],
            "title": title,
            "image_urls": meta.get("images") or [],
            "category_path": _category_from_url(url),
            "currency": _CURRENCY.get(self.site.country, "USD"),
            "status": "discovered",
            "brand": self.site.brand,
            "product_url": url,
            "site": self.site.site,
            "published_at": meta.get("lastmod"),
            "_skip_price_history_if_no_price": True,
        }


def _candidate_priority(url: str) -> tuple[int, int, str]:
    path = re.sub(r"https?://[^/]+", "", url).lower()
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    depth = path.count("/")
    if _looks_like_non_product_url(url):
        return (9, -depth, url)
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


def _slug_title_equal(slug: str | None, title: str | None) -> bool:
    if not slug or not title:
        return False
    slug_text = re.sub(r"[-_]+", " ", slug)
    norm_slug = re.sub(r"[^a-z0-9]+", " ", slug_text.lower()).strip()
    norm_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return bool(norm_slug and norm_slug == norm_title)


def _category_from_url(url: str) -> str | None:
    path = re.sub(r"https?://[^/]+", "", url).strip("/")
    parts = path.split("/")[:-1]
    if not parts:
        return None
    return " / ".join(re.sub(r"[-_]+", " ", part).strip() for part in parts if part)


def _looks_like_category_url(url: str) -> bool:
    path = re.sub(r"https?://[^/]+", "", url).lower()
    return path.startswith("/c/") or "/category/" in path or "/catalog/category/" in path


def _looks_like_non_product_url(url: str) -> bool:
    path = re.sub(r"https?://[^/]+", "", url).strip("/").lower()
    if not path:
        return True
    if _SKIP_RE.search("/" + path):
        return True
    basename = path.rsplit("/", 1)[-1]
    stem = basename[:-5] if basename.endswith(".html") else basename
    if stem in _COSTWAY_CATEGORY_BASENAMES:
        return True
    if not basename.endswith(".html") and re.search(
        r"(agb|impressum|kontakt|privacy|terms|conditions|withdrawal|"
        r"shipping|shipments|track-your-order|site-map|reward|loyalty|"
        r"aw[-_]?reward[-_]?points|myrewardszone|loyalty[-_]?cashback|"
        r"cashback|dropshipping|black[-_]?friday|flash[-_]?(deal|sale)|"
        r"bundle[-_]?sale|outlet|offer|offers|deals|promo|sale|"
        r"back[-_]?to[-_]?school|bfdealstoroyalusers|freetrials|"
        r"costway[-_]?aniversario|costway[-_]?day|costway[-_]?home|monthly[-_]?deal|"
        r"mothers[-_]?day|singles[-_]?day|nuevaoferta|populares|"
        r"recall|test|ceshi|affiliate|agrupados|bf-|carbono|christmas|"
        r"climate[-_]?action|colorfulautumn|coupon|cyber|descuento|diadel|dia-del|"
        r"diadesanvalentin|"
        r"disfrutadelairelibre|dropship|ecodiseno|garden-list|get-time|"
        r"happy[-_]?womens[-_]?day|holiday|juguetes-infantiles|kids-list|kitchen-list|labor-day|"
        r"liquidacion|location-working-hours|lxy|m-|mas-vendidos|mega-semana|"
        r"memory[-_]?of[-_]?love|milestone|month|monthly|new-arrival|newin|novedad|oferta|offer|"
        r"outlet|pascua|payment|point|policy|primavera|programa-de-afiliados|"
        r"rebajas|recomendado|regalo|return|rosa|shipping|shipments|"
        r"singleday|site-map|subscribe|summer|test|prueba|top-|"
        r"track-your-order|ventadeverano|vuelta|vuletaalcole|weekly|"
        r"weee[-_]?policy|welcome[-_]?2022|whatsappvip|whattobuy|"
        r"wholesale|why-costway|feliznavidad|"
        r"fin-de-ano|nationalday|newyear|our-guarantee|lieferorte|zahlungsarten|garantie|"
        r"widerrufsbelehrung|ueber-costway|warum-costway|partnerprogramm|"
        r"payment[-_]?methods|return[-_]?policy|location[-_]?working[-_]?hours|"
        r"affiliate[-_]?programme|why[-_]?costway|winkelgids|winkelwagen|"
        r"room|bathroom|bedroom|dining[-_]?room|fitness[-_]?room|kids[-_]?room|"
        r"living[-_]?room|office|patio|bestseller|frische[-_]?auswahl|"
        r"geschenke[-_]?fuer|kategorie2023|nationalfeiertag|recommended[-_]?may[-_]?like|"
        r"reduziert|sns|winter[-_]?sale|"
        r"top[-_]?categorie|wintercollectie|schoolseizoen|wooninspiratie|"
        r"nieuwe[-_]?aankomst|nieuwjaar|opruiming|carnaval|earthday|"
        r"eco[-_]?design[-_]?inspiratie|flash[-_]?verkoop|herfst|"
        r"kantoor[-_]?collectie|vaderdag|valentijnsdag|vrouwendag|"
        r"wereldbeker|winter[-_]?sale|ip[-_]?security|load|deal|voetbal)",
        basename,
        re.I,
    ):
        return True
    if "." not in basename and "-" in basename:
        return basename.startswith(("subscribe-", "recall-"))
    if basename.endswith(".html") and re.search(r"(^|[-_])(test|ceshi)\d*", basename, re.I):
        return True
    return False


def _dom_breadcrumb(tree: HTMLParser) -> str | None:
    crumbs = [
        n.text(separator=" ", strip=True)
        for n in tree.css(
            ".breadcrumbs a, .breadcrumb a, [class*=breadcrumb] a, "
            "nav[aria-label*=breadcrumb] a, [itemtype*=BreadcrumbList] [itemprop=name]"
        )
    ]
    cleaned = []
    for crumb in crumbs:
        text = re.sub(r"\s+", " ", html.unescape(crumb or "")).strip()
        if not text or _is_placeholder_category(text):
            continue
        if text in cleaned:
            continue
        cleaned.append(text)
    if len(cleaned) <= 1:
        return None
    return _valid_category_path("/".join(cleaned[:-1][:4]))


def _jsonld_breadcrumb(tree: HTMLParser) -> str | None:
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
            raw_type = item.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(str(t).lower() == "breadcrumblist" for t in types if t):
                continue
            names = []
            for elem in item.get("itemListElement") or []:
                if not isinstance(elem, dict):
                    continue
                nested = elem.get("item")
                name = nested.get("name") if isinstance(nested, dict) else None
                name = name or elem.get("name")
                text = re.sub(r"\s+", " ", str(name or "").strip())
                if not text or _is_placeholder_category(text):
                    continue
                if text in names:
                    continue
                names.append(text)
            if len(names) > 1:
                return _valid_category_path("/".join(names[:-1][:4]))
    return None


def _first_valid_category(*values: object) -> str | None:
    for value in values:
        category = _valid_category_path(value)
        if category:
            return category
    return None


def _valid_category_path(value: object) -> str | None:
    if isinstance(value, (list, tuple)):
        text = "/".join(str(item) for item in value if item)
    else:
        text = str(value or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip(" /")
    if not text:
        return None
    parts = [part.strip() for part in text.split("/") if part.strip()]
    parts = [part for part in parts if not _is_placeholder_category(part)]
    if not parts:
        return None
    return "/".join(parts)


def _is_placeholder_category(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized in {
        "home",
        "startseite",
        "accueil",
        "inicio",
        "site pages",
        "default category",
    }


def _dom_review_details(tree: HTMLParser, html: str) -> tuple[int | None, float | None]:
    review_count = None
    rating = None
    for selector in (
        "[data-rating-count]", "[data-review-count]", "[data-reviews-count]",
        "[class*=review]", "[id*=review]", "[class*=rating]", "[id*=rating]",
    ):
        for node in tree.css(selector)[:60]:
            for attr in (
                "data-rating-count", "data-review-count", "data-reviews-count",
                "data-count", "aria-label", "title",
            ):
                value = node.attributes.get(attr)
                if value and review_count is None:
                    review_count = _review_count_from_text(value)
            text = node.text(separator=" ", strip=True)
            if text and review_count is None:
                review_count = _review_count_from_text(text)
            if text and rating is None:
                rating = _rating_from_text(text)
            if review_count is not None and rating is not None:
                return review_count, rating
    if review_count is None:
        review_count = _review_count_from_text(html[:200000])
    if rating is None:
        rating = _rating_from_text(html[:200000])
    return review_count, rating


def _review_count_from_text(value: str) -> int | None:
    text = str(value or "")
    for pattern in (
        r"([\d,\s]+)\s*(?:reviews?|ratings?|avis|bewertungen|reseñas|recensioni)",
        r"(?:reviews?|ratings?|avis|bewertungen|reseñas|recensioni)\D{0,20}([\d,\s]+)",
    ):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        parsed = GenericCrawler._int(match.group(1))
        if parsed is not None:
            return parsed
    return None


def _rating_from_text(value: str) -> float | None:
    match = re.search(r"([0-5](?:[.,]\d+)?)\s*(?:/|out of)\s*5", str(value or ""), re.I)
    if not match:
        return None
    return GenericCrawler._num(match.group(1).replace(",", "."))


def _category_from_title_fallback(title: str | None) -> str | None:
    text = html.unescape(title or "").strip().lower()
    if not text:
        return None
    rules = (
        (r"schlauchaufroller|druckluftschlauch|schlauchtrommel",
         "Tools & Home Improvement"),
        (r"airbrush.*spray booth|paint spray booth",
         "Tools & Home Improvement"),
        (r"rc\b|r/c|ferngesteuert|monstertruck|spielzeugauto|toy car|coche.*control|"
         r"voiture.*t[ée]l[ée]command[ée]|auto.*telecomand",
         "Toys & Games/Remote Control Toys"),
        (r"keyboard|klavier|digitalpiano|rollpiano|tasteninstrument|piano|"
         r"teclado|clavier|tastiera|klokkenspel|xylofoon|slagwerkinstrument|"
         r"musikinstrument",
         "Toys & Games/Musical Instruments"),
        (r"gitarre|guitar|drum|trommel|boxen\b|music|musique|musica|"
         r"viol[ií]n|viool|ukelele|bajo el[eé]ctrico|basso elettrico|"
         r"electric bass|pickup|bass guitar|xylophone|bell lyre|vibraphone|"
         r"marching band|musical|trombone|tenor slide|brass|ukulele",
         "Toys & Games/Musical Instruments"),
        (r"kratzbaum|katzen|cat tree|cat tower|kitty tower|scratching|cat condo|"
         r"cat bed|arbre.*chat|rascador.*gato|"
         r"albero.*gatti|krabpaal|katten",
         "Pet Supplies/Cat Furniture"),
        (r"fuzzy.*blanket|fur.*blanket|throw blanket",
         "Furniture/Bedroom"),
        (r"hund|dog|chien|perro|cane|kaninchen|rabbit|konijn|cat house|"
         r"konijnenhok|pet|\btier\b|haustier|kleine dieren|puppyren|hondenhek|"
         r"hondenkrat|hondenhok|huisdierhek|hondenren|hondenwagen|hondenbuggy|"
         r"dierenhek|huisdierhuis|hondenkennel|hondendeken|hondenhelling|"
         r"hondentrap|dierentrap|kippenhok|pluimvee|kippenvoerbak|"
         r"hondenkooi|honden",
         "Pet Supplies"),
        (r"schaukelstuhl|schwingstuhl|wippsessel",
         "Furniture/Chairs & Seating"),
        (r"gartenstuhl|gartenst[üu]hle|gartensessel|sonnenliege|pflanzenregal",
         "Garden & Outdoor"),
        (r"rollator|loopwagen|loophulp|mobiliteitshulpmiddel|reisrollator",
         "Health & Beauty/Mobility Aids"),
        (r"warmwaterboiler|elektrische boiler|\bboiler\b",
         "Home Appliances"),
        (r"spiel|speelgoed|trein|bouwblokken|schuimblokken|kids|kid |kinder|children|"
         r"enfant|niñ|bambin|beb[eé]|baby|rutsche|schaukel|lauflern|laufrad|"
         r"maze|science kit|swing set|hula|ride on|push car|toddler|"
         r"climbing toys|"
         r"construction blocks|magnetic construction|bouncy water castle|bounce house|"
         r"toy set|wooden toy|doctor playset|dollhouse|dolls house|pretend|activity center|"
         r"jumping castle|ice cream cart toy|balance beam|stepping stones|"
         r"train tracks|dinosaurs|ball pit|waffle block|teepee|rocking horse|"
         r"kindersofa|kindertisch|infantil|juguetes|andador|gatear|zje[żz]d[żz]al|"
         r"kojec|niemowl|dzieci|babyfoon|flessenwarmer|babyvoeding|"
         r"speelkleed|kruipmat|speelmat|borstkolf|candy grabber|grabber game|"
         r"bouwstenen|poppenhuis|elektrische auto|klimkoepel|schommel|"
         r"4 op een rij|vier op een rij|spelreeks|coin pusher|arcade|"
         r"doktersspeelset|waterglijbaan|springgedeelte|joggingbuggy|"
         r"speelkeuken|pogostick|springstok",
         "Kids & Baby"),
        (r"canopy bed|bed frame|platform bed|bunk bed|loft bed|house bed",
         "Furniture/Bedroom"),
        (r"air conditioner|airconditioner|portable ac|dehumidifier|humidifier|"
         r"refrigerator|mini beverage refrigerator|undercounter|ceiling fan|"
         r"pedestal fan|wall mount fan|electric heater|space heater",
         "Home Appliances"),
        (r"garten|garden|jardin|giardino|outdoor|terrace|terrasse|patio|pavillon|"
         r"sonnenschirm|hochbeet|pflanz|rankgitter|gew[aä]chshaus|rasen|balkon|"
         r"gazebo|canopy|hammock|umbrella|planter|flower pot|topiary|plant stand|"
         r"windmill|wind mill|wood chipper|tendone|laghetto|depuratore|stagni|"
         r"lawn aerator|soil loosening|fire pit|fireplace log|firewood log|"
         r"compost|tumbler|privacy fence|privacy screen|willow frame|"
         r"firewood rack|log rack|fireplace companion|bird feeder|wild birds|"
         r"green house|greenhouse|growth tent|hydroponic|awning|"
         r"landscape.*rake|weed.*lake.*rake|grass sweeper|feeding trough|"
         r"artificial.*(tree|plant|flower)|fake.*(tree|plant)|faux.*(tree|plant)|"
         r"garden cart|outdoor.*bench|wicker|acacia wood (bench|table|chair|sofa|loveseat|ottoman|set)|tiki|sun shelter|"
         r"tuin|grasmaaier|gazon|vijver|parasol|zonnescherm|partytent|"
         r"plantenbak|bloempot|bloemenplank|plantenrek|kweektent|broeikas|"
         r"foliekas|paviljoen|luifel|voordak|overkapping|terrasverwarmer|"
         r"buitenverwarmer|pergola|buitenapparatuur|schuur|poortsteun|"
         r"buitendouche|tuintent|buiten tent|windscherm|verticuteermachine|"
         r"sneeuwschuiver|sneeuwschop|gras verticuteer|aangebouwde kas|"
         r"tomatenkas|kas |mini kas|bloembak|bloemenwagen|vuurkorf|"
         r"insectenverdelger|insectenvanger|plantenstandaard|grastrimmer|"
         r"kantensnijder|thermisch verzinkt gaas|vogelvoeder|voederhouder|"
         r"voederstation|vogelzaden",
         "Garden & Outdoor"),
        (r"tisch|\btable\b|mesa|bureau|scrivania|tafel|desk|workbench|workstation|"
         r"computer desk|office desk|writing desk|executive desk|"
         r"coffee table|console table|side table|dining table|"
         r"couchtisch|beistelltisch|"
         r"konsolentisch|laptoptisch|bistrotisch|bartisch|nachttisch|"
         r"eettafel|bistrogroep",
         "Furniture/Tables"),
        (r"\bbett\b|bed|lit |cama|letto|matras|mattress|colch[oó]n|colch[aã]o|"
         r"nightstand|bedside|bed bench|blanket|heated throw|seat cushion|"
         r"leg elevation pillow|"
         r"schlafsofa|sofa|couch|ledikant|slaapkamer|beenhefkussen|"
         r"leeskussen|wigkussen|memory foam kussen|oprijplaat.*honden",
         "Furniture/Bedroom"),
        (r"stuhl|chair|silla|chaise|sedia|stoel|bank|bench|hocker|sessel|"
         r"seat|stool|loveseat|ottoman|mannequin|stadium seat|swing seat|"
         r"floor cushion|rocker|footrest|"
         r"sgabello|poggiapiedi|"
         r"barhocker|klappstuhl|liegestuhl|kruk|zitkubus|zitkist|zitset|"
         r"fauteuil|relaxfauteuil|poef",
         "Furniture/Chairs & Seating"),
        (r"regal|shelf|shelving|[ée]tag[èe]re|estante|scaffale|kast|"
         r"schrank|kommode|rollwagen|aufbewahrung|storage|cabinet|"
         r"bookcase|cupboard|drawer|closet|organizer|garment|clothing rack|"
         r"coat rack|wine rack|laundry hamper|push cart|dolly|drying rack|"
         r"scarpiera|scatola|custodia|cassetti|porta.*oli|wall divider|"
         r"tv stand|media console|sideboard|buffet|trash can|garbage can|"
         r"trash bin|cargo carrier|roof bag|roof rack|record player stand|"
         r"dry erase board|white board|blackboard|easel|utility cart|"
         r"shopping cart|food prep cart|home bar|entertainment center|"
         r"universal stand|flat screen console|"
         r"pantry rack|trash bag holder|"
         r"opberg|opslag|kledingrek|kapstok|wandplank|wandrek|boekenplank|"
         r"boekenkast|kubusrek|tv meubel|tv plank|schoenenrek|wijnrek|"
         r"flessenhouder|rek\b|plank\b|trolley|tv-standaard|nichewagen|"
         r"posterstandaard|informatiehouder|wasmand|waslijn|afvalemmer|afvalbak|"
         r"afvalcontainer|commode|laden|kaartenhouder|lessenaar|tv standaard|"
         r"entertainment centrum|dakmand|winkelwagen",
         "Furniture/Storage & Shelving"),
        (r"lampe|lamp|l[aá]mpara|lampadaire|lampada|leuchte|ceiling light|led light",
         "Lighting"),
        (r"küche|kitchen|cocina|cuisine|cucina|kochtopf|kaffee|mikrowelle|"
         r"fryer|griddle|food warmer|cooking|stainless steel.*warmer|"
         r"rebanador|cortador de hoja|bacon|jam[oó]n|alimento|"
         r"ice maker|ice cube maker|ice making machine|ice cube making|countertop ice|"
         r"water dispenser|essiccatore|conservazione|coffee maker|espresso|"
         r"snow cone|food dehydrator|dehydrator|"
         r"eiscreme|mixer|grill|waffel|dampfkochtopf|freidora|exprimidor|"
         r"batidora|asador|barbacoa|olla|espumador|aufschnittmaschine|"
         r"snijmachine|capsuledispenser|koffiecapsules|servies|dinerset|"
         r"sandwichtoaster|slow cooker|magnetron|spoelbak|afdruiprek|"
         r"snelkookpan|steelpan|friteuse|hetelucht|koffiezetapparaat|"
         r"espressomachine|sapcentrifuge|staafmixer|serveerwagen|"
         r"keukentrolley|keukenwagen|keukenlade|barbecue|kookset|"
         r"keukengerei|kookbestek|voedselverwarmer|warmhoud|worstenmaker|"
         r"vleesvuller|vleesmolen|worstenvuller|soepwarmer|ijsblokjesmachine|"
         r"ijsmachine|broodbakmachine|afvalverwijdering|voedselmolen|fondue|"
         r"voedselwarmtemat|pizzaoven|hamburger|melkopschuimer|"
         r"keukenmessenslijper|popcorn|ijsbreker|stoomkoker|suikerspin|"
         r"suiker spin|vleesvermalser",
         "Kitchen & Dining"),
        (r"\bbad\b|bath|toilet|dusch|shower|ablauf|handtuch|badezimmer|"
         r"towel warmer|ironing board|"
         r"baño|bano|ducha|munddusche|irrigador|badkamer|doekwarmer|"
         r"handdoekverwarmer|handdoekdroger|monddouche|doucheafvoer|afvoerputje",
         "Bathroom"),
        (r"dampfreiniger|steam cleaner|cleaner|reiniger|staubsauger|vacuum|"
         r"fregona|limpia piso|dampfglätter|dampfglatter|mop|vloerwisser|"
         r"stoomreiniger|dweil|stofzuiger|tapijtstofzuiger|behangverwijderaar|"
         r"reinigingssysteem",
         "Home Cleaning"),
        (r"weihnacht|christmas|xmas|halloween|deko|decoration|decor|spiegel|mirror|teppich|"
         r"navidad|espejo|alfombra|alfombrilla|fu[ßs]matte|bodenmatte|discokugel|beamer|projektor|"
         r"wall art|room divider|world globe|antique globe|interactive globe|skeleton model|nutcracker|"
         r"pantalla|proyecci[oó]n|proyeccion|projection screen|"
         r"acoustic panel|sound absorbing|fireplace screen|fire panel|spinning wheel|"
         r"prize wheel|wall privacy|privacy screen|room separator|"
         r"paneles decorativos|fotograf[ií]a|fotobox|caja de luz|aroma|duft|"
         r"kosmetik|maquillaje|cosm[eé]ticos|nagellack|kamin|chimenea|"
         r"kerst|sneeuwpop|fotodoos|fotostudio|projectiescherm|lichtbord|"
         r"nagellak|brievenbus|postbox|haard|haardhout|brandhout|"
         r"ruimteverdeler|scheidingswand|inkijkbescherming|haardscherm|"
         r"brandscherm|schoorsteen|kunstmatige|kunstplant|kunstboom|"
         r"kunstbloem|nepplant|potplant|decoratie|plantenwand|kunstgras|"
         r"wandpanelen|bloemenboom|geluidsisolerende panelen|prijzenwiel|"
         r"cadeaus|gelukswiel|whiteboard|schoolbord|kamerscherm|binnenwand|"
         r"disco|lichteffect|party|kunstbloesemboom|room divider|"
         r"paraplubak|ijdelheid|rolgordijn|gordijn|verduister",
         "Home Decor"),
        (r"werkzeug|\btool\b|s[aä]ge|drill|bohr|garage|radstopper|vordach|"
         r"paint tank|spray gun|socket set|caliper|engine|hub|button maker|"
         r"amoladora|smerigliatrice|smontagomma|pneumatici|strumento|oscillante|"
         r"macchina rotativa|multiuso|cricchetto|chiavi|"
         r"puertas correderas|guias de puertas|gu[ií]as de puertas|steel kit|"
         r"computing scale|platform scale|motorcycle lift|lift jack|snow pusher|"
         r"roof rake|snow rake|air blower|air pump|work platform|pressure washer|"
         r"spray nozzles|trailer ramp|loading ramp|stair railing|"
         r"oil spill|parking mat|tile cutter|sneeze guard|reverse osmosis|"
         r"filter replacement|platform truck|"
         r"herramienta|taladro|perforaci[oó]n|escalera|leiter|klapptritt|"
         r"spritzpistole|pistola de pulverizaci[oó]n|lackierpistole|"
         r"lenkrad|abzieher|spurverbreiterung|spurplatten|pumpe|kompressor|"
         r"schwei[ßs]|soldador|soldadura|seilwinde|seilzug|generator|"
         r"bomba diesel|bomba de combustible|manguera|boquilla de combustible|"
         r"generador|metalldetektor|laser|magnetleiste|werkstattwagen|"
         r"rampe|rampas|kfz|coches|filterkartuschen|purificador|hepa filter|"
         r"waterfilter|wasserfilter|packband|klebeband|stretchfolie|"
         r"wickelfolie|palettenfolie|tresor|waffenkoffer|gewehrkoffer|"
         r"gereedschap|slangklemtang|tangen|kolomboormachine|boormachine|"
         r"stuurwiel|trekkerset|plakband|verpakkingstape|steigerbok|"
         r"werkbok|veiligheidskluis|kluis|pomp|werkplaats|rolbord|"
         r"tegenhoudsleutel|voertuig|batterijstarter|luchtcompressor|"
         r"bandenopblazer|hark\b|ladder|trapladder|opstapje|lasmachine|"
         r"lasapparaat|ijskrabber|trapleuning|leuning|paneelwagen|"
         r"wielstopper|wielafstandhouder|spoorplaat|waterdichte doos|"
         r"oprit|motorhelling|camerarail|zaagbok|zaagsteun|drempeloploop|"
         r"metaaldetector|motorkrik|motorstandaard|krik|tegelsnijder|"
         r"messenslijpmachine|cirkelzaag|invalzaag|zaagblad|stuurslot|wapenreiniging|cameratas|"
         r"bewakingscamera|houtskoolaansteker|verwarmingsplaat|laminaatsnijder",
         "Tools & Home Improvement"),
        (r"camping|feldbett|strand|surf|boot|boat|pool|piscina|spa|"
         r"trampolin|trampoline|skateboard|scooter|paddelbrett|sup-board|"
         r"gymnastics mat|gymnastic mat|hula|beach|cooler|hard cooler|hunting blind|"
         r"soccer goal|football net|football rebounder|body board|boogie board|"
         r"snow tube|sleeping bag|bike cargo|punching bag|water punching bag|rowing machine|"
         r"fishing rod|net system|floating water|camping cot|camping mattress|"
         r"camping sink|sports tent|basketball hoop|basketball stand|badminton net|"
         r"pickle.*net|hunting blind|wood burning stove|solar pool heater|"
         r"golf|bicicleta|fahrrad|e-bike|elektrofahrrad|fútbol|futbol|"
         r"bici elettrica|ruota posteriore|"
         r"baloncesto|rubberboot|vissersboot|badminton|kickstep|fiets|"
         r"kampeertent|koepeltent|drijvende|algen|slaapzak|hangmat|sup |"
         r"sup board|paddle leash|tennis|basketbal|biljart|koelbox|koeler|"
         r"kajak|kano|bodyboard|boogieboard|stand up paddle|ijsvissen|"
         r"step\b|stepper|sneeuwschoen|sneeuwboei|sneeuwslee",
         "Sports & Outdoor Recreation"),
        (r"casino|poker|chess|throwing target|4 in a row|connect game|"
         r"giant connect|dart board|shuffleboard|art set",
         "Toys & Games"),
        (r"spiel|speelgoed|trein|bouwblokken|schuimblokken|kids|kinder|children|"
         r"enfant|niñ|bambin|beb[eé]|baby|rutsche|schaukel|lauflern|laufrad|"
         r"maze|science kit|swing set|hula|ride on|push car|toddler|"
         r"climbing toys|"
         r"construction blocks|magnetic construction|bouncy water castle|bounce house|"
         r"toy set|wooden toy|doctor playset|dollhouse|dolls house|pretend|activity center|"
         r"jumping castle|ice cream cart toy|balance beam|stepping stones|"
         r"train tracks|dinosaurs|ball pit|waffle block|teepee|rocking horse|"
         r"kindersofa|kindertisch|infantil|juguetes|andador|gatear|zje[żz]d[żz]al|"
         r"kojec|niemowl|dzieci|babyfoon|flessenwarmer|babyvoeding|"
         r"speelkleed|kruipmat|speelmat|borstkolf|candy grabber|grabber game|"
         r"bouwstenen|poppenhuis|elektrische auto|klimkoepel|schommel|"
         r"4 op een rij|vier op een rij|spelreeks|coin pusher|arcade|"
         r"doktersspeelset|waterglijbaan|springgedeelte|joggingbuggy|"
         r"speelkeuken|pogostick|springstok",
         "Kids & Baby"),
        (r"fitness|exercise|training|sport|laufband|walking pad|gymnastik|"
         r"yoga|massage|heimtrainer|ruder|musculaci[oó]n|pesas|hantel|"
         r"klimmzug|tanzstange|gimnasio|entrenamiento|oefenmat|optrekstang|"
         r"stepper|climber|workout|medicijnbal|gymnastiek|beschermmat|"
         r"puzzelmat|fitnessmat|balletbarre|bokszak|loopband|vloermat|"
         r"tapis roulant|allenamento cardio|attrezzo per allenamento|sandbag|"
         r"abdominal cruncher|cruncher|treadmill|lat pulldown|cable machine|"
         r"olympic|triceps bar|push[- ]?up|crash mat|"
         r"zachte mat|halterset|rekstang|roeimachine|halterstang|barbell|"
         r"halterschijven|body workout|stimulator|voetbalrebounder|reboundwand",
         "Sports & Fitness"),
        (r"haushaltsgerate|home appliances|nevera|termoel[eé]ctrica|"
         r"aire acondicionado|humidificador|calentamiento|lavadora|pralka|"
         r"dehumidifier|fan speed|wall mount fan|heater|massaggiatore|"
         r"refrigerator|undercounter|ceiling fan|pedestal fan|"
         r"manta el[eé]ctrica|luchtbevochtiger|luchtzuiveringsfilter|"
         r"hepa-filter|hepa filter|air purifier|verwarmde dekens|"
         r"elektrisch verwarmd|vriezer|verwarmingsniveaus|elektrische deken|"
         r"wasdroger|droger|ventilator|luchtkoeler|airconditioner|wasmachine|"
         r"huishoudelijke apparaten|stijltang|wax heater|wax warmer",
         "Home Appliances"),
        (r"manicure|nagelbord|studiobord",
         "Beauty & Personal Care"),
        (r"koffer|trolleytasche|reisetasche|reisekoffer|suitcase|luggage|"
         r"maleta|valise|valigia|bolsa de deporte",
         "Luggage"),
        (r"parag[uü]ero|soporte de paraguas|sombrilla|maceta|macetero|gallinero|"
         r"jaula.*(ave|p[aá]jaro)|rankhilfe|regenfass|wassertank|"
         r"chicken feeder|chicken coop|pen fence|flower pots?|topiary|"
         r"rattan|vimini|bistro|porch|balcony|outdoor furniture|"
         r"sedie.*esterno|tavolo.*sedie|trespolo|amaca sospesa|"
         r"bollerwagen|handwagen|sichtschutz|paravent|raumteiler|"
         r"feuerschale|heckenschere|vogelfutter|futterstation|"
         r"carpa|tienda de campa|toldo|seto artificial|fender.*barcos|"
         r"guardabarros.*barcos",
         "Garden & Outdoor"),
        (r"armario|gabinete|szafa|szafka|komoda|kredens|regał|regal|p[oó]łka|"
         r"polka|archivador|cajon|caj[oó]n|zapatero|zapatos|ropero|"
         r"wyspa kuchenna|w[oó]zek barowy|servierwagen|barwagen|weinwagen",
         "Furniture/Storage & Shelving"),
        (r"bettgestell|bettrahmen|polsterbett|metallbett|doppelbett|lattenrost|"
         r"kopfteil|bed frame|bedstead|headboard|funda n[oó]rdica|almohada|"
         r"nocny|sypial",
         "Furniture/Bedroom"),
    )
    for pattern, category in rules:
        if re.search(pattern, text, re.I):
            return category
    broad_rules = (
        (r"caballete|cabestrante|bloqueo del volante|maniobra|cambiador|"
         r"compresor|aerografo|aer[oó]grafo|trinquete|tornillo de banco|"
         r"rampa|generador|inversor|detector de metales|panel publicitario|"
         r"cartel|expositor|buz[oó]n|radiador|centrifugadora|laboratorio|"
         r"pistola|pulverizaci[oó]n|spritzpistole|lackierpistole|"
         r"bomba diesel|bomba de combustible|manguera|boquilla de combustible|"
         r"seguridad reflectante|alta visibilidad|chaleco reflectivo|"
         r"giacca di sicurezza|alta visibilit|safety jacket|reflective",
         "Tools & Home Improvement"),
        (r"carrito|cajonera|caj[oó]n|zapatero|perchero|ropero|cesto de ropa|"
         r"cesta de ropa|bolsa de almacenamiento|almacenamiento|"
         r"mesa|mesita|soporte elevador|plataforma regulable|estuche|malet[ií]n|"
         r"bolsa de viaje|bolsa de deporte|parag[uü]ero",
         "Furniture/Storage & Shelving"),
        (r"freidora|microondas|picadora|exprimidor|deshidratador|barbacoa|"
         r"parrilla|asador|cortador de verdura|coctelera|maquina de caramelo|"
         r"algodon de azucar|algod[oó]n de az[uú]car|termoel[eé]ctrica",
         "Kitchen & Dining"),
        (r"medicinal|musculaci[oó]n|pesas|entrenamiento|gimnasio|remo|"
         r"masajeador|irrigador|pelo|plancha de pelo|sup|kayak|canoa|bicicleta|"
         r"paddle|trampol[ií]n",
         "Sports & Fitness"),
        (r"gato|mascota|p[aá]jaro|jaula|perro|animal",
         "Pet Supplies"),
        (r"persiana|estores|funda n[oó]rdica|manta|el[eé]ctrica|calefacci[oó]n|"
         r"secadora|aire acondicionado|purificador de aire",
         "Home Appliances"),
        (r"bamb[uú]|ba[ñn]o|ducha|espejo|alfombrilla|trampa galvanizada|"
         r"tendedero|reloj|decoraci[oó]n|video timbre|timbre",
         "Home Decor"),
        (r"bajo electrico|viol[ií]n|guitarra|teclado",
         "Toys & Games/Musical Instruments"),
        (r"etiquetas pistola|juego de etiquetas|juguete|arcade|ni[ñn]os",
         "Toys & Games"),
    )
    for pattern, category in broad_rules:
        if re.search(pattern, text, re.I):
            return category
    return None
