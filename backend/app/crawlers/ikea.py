"""IKEA 采集器 —— 家居 marketplace · 主国 US / DE / UK（含其他国家分站）。

实地验证（2026-05-24，本机直连 99.x.x.x，无代理）：
- ✅ `www.ikea.com/robots.txt` 暴露 sitemap_index：
    `https://www.ikea.com/sitemaps/sitemap.xml`
  （不是请求里贴的 `/us/en/sitemap-products-en-US.xml`，那个 404。
   IKEA 把所有国家 / 语言的 sitemap 合并在 `/sitemaps/sitemap.xml` 的 index 里，
   分片命名格式：`prod-<lang>-<COUNTRY>_<N>.xml`，例如：
     · US: `prod-en-US_{1..4}.xml`
     · DE: `prod-de-DE_{1..6}.xml`
     · UK: `prod-en-GB_{1..6}.xml`
  ）
- ✅ 子 sitemap 是 Google Image Sitemap 扩展，每条 url 节点含：
    `<loc>` PDP URL · `<image:image><image:loc>` 多张高清图（通常 5-9 张）
    `<xhtml:link hreflang="...">` 多语种镜像
  即 sitemap 已携带 SKU / slug / 图集 / 多语版本，PDP 只是来补价格 / 评分。
- ✅ PDP 是标准 SSR HTML（~300-420 KB），内含两块 JSON-LD：
    1. `@type=BreadcrumbList` → 5 级分类路径
    2. `@type=Product` → name / sku / mpn / description / image[] /
                        offers{price, priceCurrency, availability} /
                        aggregateRating{ratingValue, reviewCount} / brand / color
  价格 / 评分 / 库存全在 JSON-LD 里，**不需要解 HTML body**，干净。
- ✅ curl_cffi(impersonate=chrome) 直连 PDP 实测 5 连发全 200，单页 ~0.8-1.6s，
  无 challenge 页。IKEA 的 Cloudflare 是中级别（首屏静态资源），对单 IP
  低频访问（≥1s 间隔）容忍度高。
- ⚠ 如未来命中 403 / 503 / cf-ray-block，走 StealthyFetcher 兜底（代码已内置）。

策略：
  1. 拉 `/sitemaps/sitemap.xml` 列出全部子 sitemap；按 country 过滤
     `prod-<lang>-<country>_N.xml` 分片（US: en-US, DE: de-DE, UK: en-GB）
  2. 顺序读子 sitemap，从 `<url>` 块抽 `<loc>` + `<image:loc>` + sku（URL 末段）
  3. 累积到 limit 个 PDP URL（默认 1000）后停止读取
  4. 逐个 GET PDP，从 JSON-LD 解 Product + BreadcrumbList → row
  5. 连续 5 次 block → 进入 90s 冷却 + rotate session；连续 10 次 → 熔断
  6. IKEA_USE_STEALTH=1 显式启用 StealthyFetcher 兜底

字段对齐 VonHausCrawler._parse_product 输出 schema。

批C 收编（2026-06）：
  - curl 段改用 make_fetcher().get()，自动计 api_calls
  - stealth 段用 count_browser_fetch 包裹，成功计 browser_opens
  - 删 proxy 自管(_session 中 s.proxies)；保留 _headers() + guard / _is_blocked_body / parse
"""
from __future__ import annotations

import json
import os
import re
import time

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("IKEA_LIMIT", "1000"))
MAX_ELAPSED_SEC = float(os.environ.get("IKEA_MAX_ELAPSED_SEC", "240"))
SITEMAP_INDEX = "https://www.ikea.com/sitemaps/sitemap.xml"

# country code → sitemap 分片前缀（lang 部分按 IKEA 主语种走）
_COUNTRY_SHARD = {
    "US": "prod-en-US",
    "DE": "prod-de-DE",
    "UK": "prod-en-GB",
    "GB": "prod-en-GB",
    "FR": "prod-fr-FR",
    "IT": "prod-it-IT",
    "ES": "prod-es-ES",
    "NL": "prod-nl-NL",
    "PL": "prod-pl-PL",
    "PT": "prod-pt-PT",
    "JP": "prod-ja-JP",
    "CA": "prod-en-CA",
    "AU": "prod-en-AU",
}
_COUNTRY_CURRENCY = {
    "US": "USD", "DE": "EUR", "UK": "GBP", "GB": "GBP",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "PL": "PLN", "PT": "EUR", "JP": "JPY",
    "CA": "CAD", "AU": "AUD",
}

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
# 整条 <url>...</url> 块（用于抓 PDP URL + image: 内联图集）
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_IMG_LOC_RE = re.compile(r"<image:loc>\s*(.*?)\s*</image:loc>")
# /us/en/p/<slug>-<sku>/  · sku 形如 10580070 或 s89516665
_SKU_RE = re.compile(r"-(s?\d{6,12})/?$")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class IkeaCrawler(BaseCrawler):
    platform = "ikea"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.country = (site.country or "US").upper()
        # IKEA Cloudflare 中级：1.5s 间隔实测稳定，保守 2s
        self.delay = float(os.environ.get("IKEA_DELAY", "2.0"))

    # ------------------------------------------------------------------
    # headers  (replaces old _session — proxy handled by CrawlerFetcher)
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self._accept_language(),
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"{self.base}/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _accept_language(self) -> str:
        c = self.country
        return {
            "DE": "de-DE,de;q=0.9,en;q=0.7",
            "FR": "fr-FR,fr;q=0.9,en;q=0.7",
            "IT": "it-IT,it;q=0.9,en;q=0.7",
            "ES": "es-ES,es;q=0.9,en;q=0.7",
            "JP": "ja-JP,ja;q=0.9,en;q=0.7",
            "PL": "pl-PL,pl;q=0.9,en;q=0.7",
            "PT": "pt-PT,pt;q=0.9,en;q=0.7",
            "NL": "nl-NL,nl;q=0.9,en;q=0.7",
        }.get(c, "en-US,en;q=0.9")

    def _country_segment(self) -> str:
        """`/<country>/<lang>` URL 段，用于 warmup referer。"""
        c = self.country.lower()
        mapping = {
            "us": "us/en", "de": "de/de", "uk": "gb/en", "gb": "gb/en",
            "fr": "fr/fr", "it": "it/it", "es": "es/es", "nl": "nl/nl",
            "pl": "pl/pl", "pt": "pt/pt", "jp": "jp/ja",
            "ca": "ca/en", "au": "au/en",
        }
        return mapping.get(c, "us/en")

    # ------------------------------------------------------------------
    # main
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="ikea")
        started = time.monotonic()

        # Warmup：访问首页建立会话 / 预热 Cloudflare cookie（计入 api_calls）
        try:
            fetcher.get(
                f"{self.base}/{self._country_segment()}/",
                headers=self._headers(),
                timeout=30,
            )
        except Exception:
            pass

        urls = self._collect_pdp_urls(fetcher, result)
        if not urls:
            result.notes.append("⚠ 未能收集到任何 PDP URL —— 中止")
            return result

        targets = urls[: self.limit]
        result.notes.append(
            f"sitemap 累计 {len(urls)} PDP URL，本次抓取 {len(targets)}")

        import time as _t

        ok = fail = blocked = missing = stealth_used = 0
        consecutive_block = 0
        BLOCK_BREAK = 10
        BLOCK_COOLDOWN_S = 90
        SESSION_ROTATE = 200
        STEALTH_USE = (os.environ.get("IKEA_USE_STEALTH", "0") == "1")
        STEALTH_BUDGET = 5

        for i, entry in enumerate(targets):
            if time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 IKEA_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回已解析结果（ok={ok}, fail={fail}, blocked={blocked}）")
                break
            url = entry["url"]
            sitemap_images: list[str] = entry.get("images") or []

            # 周期性 fetcher rotate（make_fetcher 每次创建新 CrawlerFetcher 实例）
            if i > 0 and i % SESSION_ROTATE == 0:
                fetcher = self.make_fetcher(kind="product", source="ikea")
                result.notes.append(
                    f"… 第 {i} 条，主动 rotate fetcher（已抓 {ok}）")

            try:
                html, code = self._fetch_pdp(fetcher, url)

                if code == 404 or code == 410:
                    missing += 1
                    consecutive_block = 0
                    self.sleep()
                    continue

                is_block = (code in (401, 403, 429, 451, 503)
                            or (html is not None and self._is_blocked_body(html)))
                if is_block:
                    blocked += 1
                    consecutive_block += 1
                    if blocked <= 3 or consecutive_block in (1, 5):
                        result.notes.append(
                            f"⚠ {code or 'body-block'} (连击 {consecutive_block}) "
                            f"@ ok={ok}/{i} {url[-50:]}")
                    if consecutive_block == 1:
                        result.notes.append(
                            f"  → sleep {BLOCK_COOLDOWN_S}s + 重建 fetcher")
                        _t.sleep(BLOCK_COOLDOWN_S)
                        fetcher = self.make_fetcher(kind="product", source="ikea")
                        fail += 1
                        continue
                    if consecutive_block == 2:
                        result.notes.append(
                            f"  → 连续 block，sleep {BLOCK_COOLDOWN_S*2}s")
                        _t.sleep(BLOCK_COOLDOWN_S * 2)
                        fetcher = self.make_fetcher(kind="product", source="ikea")
                        fail += 1
                        continue
                    if STEALTH_USE and stealth_used < STEALTH_BUDGET:
                        html2 = self._fetch_via_stealth(url)
                        stealth_used += 1
                        if html2 and not self._is_blocked_body(html2):
                            html = html2
                            consecutive_block = 0
                        else:
                            fail += 1
                            if consecutive_block >= BLOCK_BREAK:
                                raise BlockedError(
                                    f"ikea 连续 {consecutive_block} 次封锁"
                                    f"（含 stealth 失败），熔断")
                            self.sleep()
                            continue
                    else:
                        fail += 1
                        if consecutive_block >= BLOCK_BREAK:
                            raise BlockedError(
                                f"ikea 连续 {consecutive_block} 次封锁，熔断"
                                f"（已抓 {ok}）")
                        _t.sleep(BLOCK_COOLDOWN_S * consecutive_block)
                        fetcher = self.make_fetcher(kind="product", source="ikea")
                        continue
                elif code == 200:
                    consecutive_block = 0

                if not html:
                    fail += 1
                    self.sleep()
                    continue

                row = self._parse_product(html, url, sitemap_images)
                if row:
                    self.snapshot(row["sku"], html)
                    result.products.append(row)
                    ok += 1
                    if ok and ok % 50 == 0:
                        result.notes.append(
                            f"  进度 ok={ok} blocked={blocked} 404={missing}")
                else:
                    fail += 1
            except BlockedError:
                raise
            except Exception as exc:
                fail += 1
                if fail <= 5:
                    result.notes.append(f"跳过 {url[-60:]}: {exc}")
            self.sleep()

        result.notes.append(
            f"成功 {ok}/{len(targets)} · 失败 {fail} · 已下架(404) {missing} · "
            f"反爬命中 {blocked} · stealth fallback {stealth_used}")
        return result

    # ------------------------------------------------------------------
    # sitemap
    # ------------------------------------------------------------------
    def _collect_pdp_urls(self, fetcher,
                          result: CrawlResult) -> list[dict]:
        """读 sitemap_index → 筛 country 分片 → 累积 (url, images) 字典直到 limit。"""
        try:
            res = fetcher.get(
                SITEMAP_INDEX,
                headers=self._headers(),
                timeout=30,
            )
            self.guard(res.status or 0, "sitemap_index")
            if (res.status or 0) != 200:
                result.notes.append(
                    f"⚠ sitemap_index 返回 {res.status}")
                return []
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ sitemap_index 不可达: {exc}")
            return []

        all_subs = _LOC_RE.findall(res.text)
        prefix = _COUNTRY_SHARD.get(self.country, _COUNTRY_SHARD["US"])
        subs = [u for u in all_subs if f"/sitemaps/{prefix}_" in u]
        result.notes.append(
            f"sitemap_index 共 {len(all_subs)} 分片，"
            f"country={self.country} 命中 {len(subs)} 个（前缀 {prefix}）")
        self.snapshot("sitemap_index", res.text[:500_000])

        out: list[dict] = []
        seen: set[str] = set()
        for sm in subs:
            if len(out) >= self.limit:
                break
            try:
                # 子 sitemap 体积大（US shard 1 ~ 50MB），加大超时
                r = fetcher.get(sm, headers=self._headers(), timeout=120)
                if (r.status or 0) != 200:
                    result.notes.append(
                        f"⚠ {sm.rsplit('/',1)[-1]} {r.status}")
                    continue
            except Exception as exc:
                result.notes.append(
                    f"⚠ {sm.rsplit('/',1)[-1]} 异常: {exc}")
                continue

            self.snapshot(sm.rsplit("/", 1)[-1], r.text[:300_000])

            picked = 0
            for blk in _URL_BLOCK_RE.finditer(r.text):
                if len(out) >= self.limit:
                    break
                body = blk.group(1)
                m_loc = re.search(r"<loc>\s*(.*?)\s*</loc>", body)
                if not m_loc:
                    continue
                pdp_url = m_loc.group(1)
                # 只要 /<seg>/<lang>/p/... 形态
                if "/p/" not in pdp_url:
                    continue
                if pdp_url in seen:
                    continue
                seen.add(pdp_url)
                images = _IMG_LOC_RE.findall(body)
                out.append({"url": pdp_url, "images": images})
                picked += 1

            result.notes.append(
                f"{sm.rsplit('/',1)[-1]}: +{picked} PDP（累计 {len(out)}）")
            self.sleep()
        return out

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def _fetch_pdp(self, fetcher, url: str) -> tuple[str | None, int]:
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=30)
        except Exception:
            return None, 0
        if (res.status or 0) == 200:
            return res.text, 200
        return None, res.status or 0

    def _fetch_via_stealth(self, url: str) -> str | None:
        """curl_cffi 触发反爬时走 StealthyFetcher（Camoufox）。

        批C：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，
        成功时自动 browser_opens += 1。stealth kw 参数 / persist_profile /
        profile 目录逻辑全部原样保留，只在最外层套计数。
        """
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.country,
                persist_profile_key=f"ikea_{self.site.site}",
                timeout_ms=60000,
            )

            def _do_fetch():
                return StealthyFetcher.fetch(url, **kw)

            # 成功标准：status == 200 且有 html_content 或 body（ikea 原判断）
            def _success(page) -> bool:
                return (
                    getattr(page, "status", None) == 200
                    and bool(
                        getattr(page, "html_content", None)
                        or getattr(page, "body", None)
                    )
                )

            page = self.count_browser_fetch(_do_fetch, success=_success)
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            return None
        return None

    @staticmethod
    def _is_blocked_body(html: str) -> bool:
        """Cloudflare challenge / Access denied 等 body 特征识别。
        IKEA PDP 正常 ~300-420KB；challenge 页 < 50KB。

        坑：IKEA 每个正常 PDP 都嵌入 `/cdn-cgi/challenge-platform/scripts/jsd/main.js`
        的 Cloudflare bot beacon —— 不能作为 challenge 判据。
        只识别明确的 "正在挑战 / 已拒绝" 标志，且要求页面很小（< 30KB）。"""
        if not html:
            return True
        if len(html) < 30_000:
            return True
        # 仅识别 active challenge / hard-block 标志
        markers = (
            "Just a moment...",                # CF JS challenge title
            "cf-browser-verification",          # 老版 CF challenge body class
            "cf-challenge-running",             # CF challenge runtime
            "Pardon Our Interruption",          # PerimeterX
            "Sorry, you have been blocked",     # CF block page
            "Attention Required! | Cloudflare", # CF firewall page
            "captcha-bypass",                   # captcha pages
        )
        return any(m in html for m in markers)

    # ------------------------------------------------------------------
    # parse —— JSON-LD 是主源
    # ------------------------------------------------------------------
    def _parse_product(self, html: str, url: str,
                       sitemap_images: list[str]) -> dict | None:
        ld_product, ld_breadcrumb = self._collect_jsonld(html)
        # 没拿到 Product JSON-LD → 不是 PDP，或被改版了
        if not ld_product:
            return None

        # SKU：优先 JSON-LD.sku；退化为 URL 末段
        sku = ld_product.get("sku") or ld_product.get("mpn")
        if not sku:
            m = _SKU_RE.search(url)
            if m:
                sku = m.group(1)
        if not sku:
            return None
        sku = str(sku).strip()

        # 标题
        title = ld_product.get("name")
        if not title:
            tree = HTMLParser(html)
            h1 = tree.css_first("h1")
            title = h1.text(strip=True) if h1 else None
        if not title:
            return None

        # offers
        offers = ld_product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        sale_price = self._num(offers.get("price"))
        currency = (offers.get("priceCurrency")
                    or _COUNTRY_CURRENCY.get(self.country, "USD"))
        avail = str(offers.get("availability", "")).lower()
        status = ("out_of_stock"
                  if ("outofstock" in avail or "out of stock" in avail
                      or "soldout" in avail)
                  else "on_sale")

        # 评分
        rating = None
        review_count = None
        ar = ld_product.get("aggregateRating") or {}
        if isinstance(ar, dict):
            rating = self._num(ar.get("ratingValue"))
            rc = ar.get("reviewCount") or ar.get("ratingCount")
            if rc is not None:
                try:
                    review_count = int(str(rc).replace(",", ""))
                except (TypeError, ValueError):
                    pass

        # 图片：JSON-LD.image 优先，回退到 sitemap 携带的图集
        images = self._extract_images(ld_product.get("image"))
        if not images:
            images = sitemap_images[:]
        # 去重 + 限 10
        seen_img: set[str] = set()
        clean_images: list[str] = []
        for u in images:
            if u and u not in seen_img:
                seen_img.add(u)
                clean_images.append(u)
        clean_images = clean_images[:10]

        # 分类路径：BreadcrumbList JSON-LD（剔除 "Products" 根）
        category_path = self._breadcrumb_path(ld_breadcrumb)
        if not category_path:
            cat = ld_product.get("category")
            if isinstance(cat, str):
                category_path = cat

        description = ld_product.get("description")
        brand = self._brand(ld_product) or self.site.brand or "IKEA"

        return {
            "sku": sku,
            "spu": sku,
            "title": title.strip(),
            "description": description,
            "image_urls": clean_images,
            "category_path": category_path,
            "sale_price": sale_price,
            "original_price": sale_price,
            "currency": currency,
            "ratings": rating,
            "review_count": review_count,
            "status": status,
            "product_url": url,
            "site": self.site.site,
            "brand": brand,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_jsonld(html: str) -> tuple[dict | None, dict | None]:
        product = None
        breadcrumb = None
        for block in _LD_RE.findall(html):
            try:
                data = json.loads(block.strip())
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                t = c.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    product = c
                elif t == "BreadcrumbList":
                    breadcrumb = c
            if product and breadcrumb:
                break
        return product, breadcrumb

    @staticmethod
    def _breadcrumb_path(ld: dict | None) -> str | None:
        if not ld:
            return None
        names: list[str] = []
        for item in ld.get("itemListElement", []):
            if not isinstance(item, dict):
                continue
            n = item.get("name")
            if not n:
                inner = item.get("item")
                if isinstance(inner, dict):
                    n = inner.get("name")
            if n and n.lower() not in ("home", "products", ""):
                names.append(n)
        return "/".join(names[:4]) or None

    @staticmethod
    def _extract_images(raw) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, str):
            return [raw]
        out: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    u = item.get("contentUrl") or item.get("url")
                    if u:
                        out.append(u)
        elif isinstance(raw, dict):
            u = raw.get("contentUrl") or raw.get("url")
            if u:
                out.append(u)
        return out

    @staticmethod
    def _brand(ld: dict) -> str | None:
        b = ld.get("brand")
        if isinstance(b, str):
            return b
        if isinstance(b, dict):
            return b.get("name")
        return None

    @staticmethod
    def _num(v):
        if v is None:
            return None
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None
