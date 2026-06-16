"""Houzz.com 采集器 —— 美国高端家居社区 + 商城（Marketplace 已 Sunset）。

实地侦察（2026-05-24，本机直连 + 无代理）：
-------------------------------------------------------------------
1. **Marketplace Sunset 是核心发现**：Houzz 的 Shop（由 Cart.com 运营）
   已停止销售。所有历史 PDP URL 形如
       `/products/{slug}-prvw-vr~{vr_id}`
   一律返回 **404 + `<title>Shop Houzz - No Longer Available</title>` +
   `marketplaceSunset_*.bundle.css`**，正文有官方 banner：
       "Purchasing on Shop Houzz, operated by Cart.com, is no longer
        available. For questions regarding Shop Houzz orders please reach
        out here. Explore ways Houzz can help with your renovation, design
        and business needs: Find Professionals / Browse Photos /
        Explore Houzz Pro"
   该页面 **没有任何商品 meta**（no OpenGraph product:*、no JSON-LD、
   robots: noindex,nofollow），只剩纯 sunset 公告 + 通用 header。

2. **没有任何活跃 PDP**：实测多个 URL 模式（含 `/product/{id}`、
   `/shop`、`/buy`、`/marketplace`、`/category`、`/catalog`、`/store`）
   全部 404；`/shop` 直接被 Cloudflare 403。`shophouzz.com`（链接出现
   在 homepage 数据里）是 password-protected 的 Shopify 占位站，未开放。
   houzz.co.uk / com.au / ie / ca 等国际站同样把 `/products/...` 转向
   `Photos` 灵感库（标题如 "75 Living Room Ideas"），无活跃商城。

3. **没有公开 sitemap**：`/sitemap.xml`、`/sitemap_index.xml`、
   `/sitemap-index.xml`、`/sitemaps/sitemap.xml`、`/products-sitemap.xml`
   等 12 种常见路径全 404。`robots.txt` 也不暴露任何 Sitemap: 行。
   `/api/sitemap` 存在但要求 token（API.3 Authentication Required）。

4. **首页 / Photos 页可达**：homepage（1.6 MB SSR HTML）、photos
   分类（`-phbr0-bp~t_*`）、houzz-tv（`-stshtvvw-vt~`）都 200 直连
   curl_cffi 无反爬。但页面里没有任何"购买/价格"实体——只有
   设计灵感图 + 专业人士目录 + 文章。

5. **唯一可枚举 PDP URL 的来源 = Wayback Machine CDX**：
       https://web.archive.org/cdx/search/cdx?
         url=houzz.com/products/*&output=text&from=2023
         &filter=urlkey:.*prvw-vr.*&collapse=urlkey
   2023+ 累计 5 万 + URL（CDX 单次返回 5000，分页可拉更多）。
   这些 URL 当年是真实商品 PDP，今天点过去 100% 返回 sunset 404。

策略
-------------------------------------------------------------------
即使商城关停，pipeline 的 REQUIRED 只要 (sku, title, product_url, site)
四个字段——这些都能从 sunset URL 里解析出来：
  · sku  = URL 末段的 `vr~{vr_id}` 数字
  · title = URL slug 去 `-prvw-vr~...` 后缀，下划线/连字符 → 空格
  · product_url = 原 URL
  · site = 站点 site code
其它字段全部留空（price/image/category/desc 在 sunset 页都没有）。
status 字段填 `discontinued`，让消费方明确这是历史商品而非在售。

抓取流程：
  1) 用 Wayback CDX 拉够 HOUZZ_LIMIT * 1.3 条 PDP URL（容差去重）
  2) 顺序 curl_cffi GET 每条 → 401/403 走 stealth 兜底
  3) 解析 sunset 页（其实只为确认它真的是 Houzz 而不是别处）
  4) 落库（sale_price = None，status = "discontinued"）

抓不到价格也入库——本路径下"100% 入库 / 0% 拿到价格"是符合预期的
结论。如果未来 Houzz 重启商城或开放某个 partner API，可在 _parse_pdp
里增加 JSON-LD / OpenGraph 解析路径。
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("HOUZZ_LIMIT", "1000"))
# Wayback CDX 单次最多返回 ~5000；超过 1000 时分页查
WAYBACK_PAGE_SIZE = int(os.environ.get("HOUZZ_WAYBACK_PAGE", "5000"))
WAYBACK_FROM_YEAR = os.environ.get("HOUZZ_WAYBACK_FROM", "2023")
STEALTH_BUDGET = int(os.environ.get("HOUZZ_STEALTH_BUDGET", "3"))

_VR_RE = re.compile(r"-prvw-vr~(\d+)$")
_CDX_LINE_RE = re.compile(
    r"^com,houzz\)/products/(\S+)\s+\d+\s+(\S+)\s+\S+\s+\d+",
    re.M,
)
# sunset 页特征：HTTP 404 + 这两个标识必须同时出现
_SUNSET_TITLE = "Shop Houzz - No Longer Available"
_SUNSET_CSS = "marketplaceSunset"


class HouzzCrawler(BaseCrawler):
    platform = "houzz"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)

    # ------------------------------------------------------------------
    # headers  (replaces old _session — proxy handled by CrawlerFetcher)
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

    # ------------------------------------------------------------------
    # main
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="houzz")

        # ---- Step 1：从 Wayback CDX 拉历史 PDP URL 池 ----
        urls = self._collect_pdp_urls(fetcher, result)
        if not urls:
            result.notes.append(
                "⚠ 未能从 Wayback CDX 收集到任何 PDP URL —— 中止")
            return result

        targets = urls[: self.limit]
        result.notes.append(
            f"Wayback CDX 累计 {len(urls)} 条历史 PDP URL，"
            f"本次抓取 {len(targets)}（HOUZZ_LIMIT={self.limit}）")

        # ---- Step 2：逐条抓取，识别 sunset / 阻塞 / 真活页 ----
        sunset = blocked = ok = fail = stealth_used = with_price = 0
        for i, url in enumerate(targets):
            try:
                html, code = self._fetch(fetcher, url)

                # Cloudflare 整站 403 或 PerimeterX 短身体 → stealth 兜底
                is_block = code in (401, 403, 429, 451) or (
                    code == 200 and html and len(html) < 2000
                )
                if is_block and stealth_used < STEALTH_BUDGET:
                    html2 = self._fetch_via_stealth(url)
                    stealth_used += 1
                    if html2:
                        html = html2
                        code = 200
                        is_block = False
                    if blocked < 3:
                        result.notes.append(
                            f"⚠ 阻塞 {code} → stealth fallback "
                            f"({stealth_used}/{STEALTH_BUDGET}) @ {url[-60:]}")

                if is_block:
                    blocked += 1
                    fail += 1
                    self.sleep()
                    continue

                if not html:
                    fail += 1
                    self.sleep()
                    continue

                row = self._parse_pdp(html, url, code)
                if not row:
                    fail += 1
                    self.sleep()
                    continue

                if row.get("status") == "discontinued":
                    sunset += 1
                if row.get("sale_price") is not None:
                    with_price += 1

                self.snapshot(row["sku"], html[:200_000])
                result.products.append(row)
                ok += 1

                if ok and ok % 100 == 0:
                    result.notes.append(
                        f"  进度 ok={ok} sunset={sunset} "
                        f"price={with_price} blocked={blocked}")
            except BlockedError:
                raise
            except Exception as exc:
                fail += 1
                if fail <= 5:
                    result.notes.append(f"跳过 {url[-60:]}: {exc}")
            self.sleep()

        result.notes.append(
            f"采集完成：入库 {ok}/{len(targets)} · "
            f"sunset 占比 {sunset}/{ok} ({100*sunset/max(ok,1):.0f}%) · "
            f"拿到价格 {with_price}/{ok} ({100*with_price/max(ok,1):.0f}%) · "
            f"反爬阻塞 {blocked} · stealth fallback {stealth_used} · "
            f"失败 {fail}")
        return result

    # ------------------------------------------------------------------
    # Wayback CDX 拉 PDP URL 池
    # ------------------------------------------------------------------
    def _collect_pdp_urls(self, fetcher,
                          result: CrawlResult) -> list[str]:
        """从 Wayback Machine CDX 索引拉 Houzz 历史 PDP URL。

        Houzz Marketplace 已 sunset，全站没有公开 sitemap，唯一可枚举
        商品 URL 池的途径就是 Wayback Machine 的历史抓取索引。CDX 支持
        正则过滤（urlkey:.*prvw-vr.*）+ collapse=urlkey 去重。
        """
        need = max(self.limit * 2, 200)   # 多拉一些容差去重
        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url=houzz.com/products/*&output=text&from={WAYBACK_FROM_YEAR}"
            f"&filter=urlkey:.*prvw-vr.*&collapse=urlkey&limit={need}"
        )
        try:
            res = fetcher.get(cdx_url, headers=self._headers(), timeout=60)
            if (res.status or 0) != 200:
                result.notes.append(
                    f"⚠ Wayback CDX 返回 {res.status}")
                return []
        except Exception as exc:
            result.notes.append(f"⚠ Wayback CDX 不可达: {exc}")
            return []

        urls: list[str] = []
        seen: set[str] = set()
        for line in res.text.splitlines():
            # CDX text 行：urlkey timestamp original mimetype statuscode digest length
            parts = line.split()
            if len(parts) < 3:
                continue
            original = parts[2]
            # 去掉 wayback 残留的 :80 / 协议差异
            original = original.replace("http://", "https://", 1)
            if "houzz.com/products/" not in original:
                continue
            if not _VR_RE.search(original):
                continue
            # 用 vr_id 去重（同一商品 slug 可能多次微调）
            m = _VR_RE.search(original)
            vr_id = m.group(1)
            if vr_id in seen:
                continue
            seen.add(vr_id)
            urls.append(original)

        result.notes.append(f"Wayback CDX 解析 {len(urls)} 个去重 vr_id")
        self.snapshot("wayback_cdx", res.text[:300_000])
        return urls

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def _fetch(self, fetcher, url: str) -> tuple[str | None, int]:
        """单页抓取。Houzz sunset 页返回 HTTP 404 + 1.4 MB body —— 这是
        正常情况，body 仍要解析（拿 slug + vr_id）。"""
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=30)
        except Exception:
            return None, 0
        # 200 / 404 都返回 body —— 由上层解析决定是 sunset 还是真活页
        return (res.text if res.text else None), (res.status or 0)

    def _fetch_via_stealth(self, url: str) -> str | None:
        """Cloudflare 403 兜底 —— scrapling StealthyFetcher。

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
                country=self.site.country or "US",
                persist_profile_key=f"houzz_{self.site.site}",
                timeout_ms=60000,
                solve_cloudflare=True,
            )

            def _do_fetch():
                return StealthyFetcher.fetch(url, **kw)

            # 成功标准：houzz 原标准 — status in (200, 404) and html_content/body
            # （sunset 页返回 404 + 大 body，也是有效响应）
            def _success(page) -> bool:
                return (
                    getattr(page, "status", None) in (200, 404)
                    and bool(
                        getattr(page, "html_content", None)
                        or getattr(page, "body", None)
                    )
                )

            page = self.count_browser_fetch(_do_fetch, success=_success)
            if getattr(page, "status", None) in (200, 404):
                return page.html_content or page.body or ""
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # parse
    # ------------------------------------------------------------------
    def _parse_pdp(self, html: str, url: str, code: int) -> dict | None:
        """解析一个 PDP 页面。

        三种情况：
          A. **Sunset 页**（绝大多数）：code=404 + `marketplaceSunset` +
             `Shop Houzz - No Longer Available` 标题。
             → status="discontinued"，sale_price=None，从 URL slug
             + vr_id 提取 sku/title/product_url。
          B. **真活页**（理论上可能存在，但实地未观察到）：JSON-LD
             Product schema 或 OpenGraph product:* meta 完整。
             → 走完整字段提取（价格 / 图 / 描述）。
          C. **完全 404 无 sunset banner**：极少。fallback 到 sunset 路径。
        """
        m = _VR_RE.search(url)
        if not m:
            return None
        vr_id = m.group(1)

        # 从 slug 复原 title（去掉末尾的 -prvw-vr~{id}）
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        slug = _VR_RE.sub("", slug)
        title = unquote(slug).replace("-", " ").strip()
        # 一些 slug 以 % 开头是 URL 编码残渣，正常化空白
        title = re.sub(r"\s+", " ", title)
        if not title:
            title = f"houzz-{vr_id}"

        is_sunset = (_SUNSET_TITLE in html and _SUNSET_CSS in html)

        # 默认 sunset 路径
        sale_price: float | None = None
        original_price: float | None = None
        image_urls: list[str] = []
        description: str | None = None
        category_path: str | None = None
        status = "discontinued" if is_sunset else "out_of_stock"

        if not is_sunset:
            # B 情况：真活页（理论分支，实地未触发，留扩展点）
            sale_price, original_price = self._extract_price(html)
            image_urls = self._extract_images(html)
            description = self._extract_meta(html, "og:description")
            category_path = self._extract_breadcrumb(html)
            og_title = self._extract_meta(html, "og:title")
            if og_title:
                # Houzz og:title 形如 "Sofa Name | Houzz"，去尾巴
                clean = re.split(r"\s+\|\s+Houzz\s*$", og_title)[0].strip()
                if clean:
                    title = clean
            if sale_price is not None:
                status = "on_sale"

        return {
            "sku": str(vr_id),
            "spu": str(vr_id),
            "title": title[:512],
            "description": description,
            "image_urls": image_urls,
            "category_path": category_path,
            "sale_price": sale_price,
            "original_price": original_price or sale_price,
            "currency": "USD",
            "status": status,
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    # ------------------------------------------------------------------
    # helpers（仅在真活页时用到 —— 留下兼容未来 Houzz 重启商城）
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_meta(html: str, prop: str) -> str | None:
        m = re.search(
            rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+'
            r'content="([^"]+)"', html, re.I)
        if m:
            return m.group(1)
        m = re.search(
            rf'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)='
            rf'"{re.escape(prop)}"', html, re.I)
        return m.group(1) if m else None

    def _extract_price(self, html: str) -> tuple[float | None, float | None]:
        """JSON-LD Product offers.price + OpenGraph product:price:amount。"""
        import json
        sale = original = None
        # JSON-LD
        for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.S):
            try:
                doc = json.loads(m.group(1).strip())
            except Exception:
                continue
            for c in (doc if isinstance(doc, list) else [doc]):
                if not isinstance(c, dict):
                    continue
                t = c.get("@type")
                if t != "Product" and (
                        not isinstance(t, list) or "Product" not in t):
                    continue
                offers = c.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    sale = self._to_float(offers.get("price")) or sale
                    original = self._to_float(
                        offers.get("highPrice")
                        or offers.get("priceSpecification", {}).get(
                            "price")) or original
        if sale is None:
            v = self._extract_meta(html, "product:price:amount")
            sale = self._to_float(v)
        return sale, original

    @staticmethod
    def _to_float(v) -> float | None:
        if v is None:
            return None
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return float(m.group()) if m else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _extract_images(html: str) -> list[str]:
        imgs: list[str] = []
        og = re.search(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        if og:
            imgs.append(og.group(1))
        for m in re.finditer(
                r'<img[^>]+src="(https?://st\.hzcdn\.com/[^"]+)"', html):
            url = m.group(1)
            if url not in imgs:
                imgs.append(url)
            if len(imgs) >= 10:
                break
        return imgs

    @staticmethod
    def _extract_breadcrumb(html: str) -> str | None:
        import json
        for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.S):
            try:
                doc = json.loads(m.group(1).strip())
            except Exception:
                continue
            for c in (doc if isinstance(doc, list) else [doc]):
                if not isinstance(c, dict):
                    continue
                if c.get("@type") != "BreadcrumbList":
                    continue
                names: list[str] = []
                for item in c.get("itemListElement", []) or []:
                    if not isinstance(item, dict):
                        continue
                    inner = item.get("item")
                    n = (inner.get("name") if isinstance(inner, dict)
                         else item.get("name"))
                    if n and n.lower() not in ("home", ""):
                        names.append(n)
                if names:
                    return "/".join(names[:4])
        return None
