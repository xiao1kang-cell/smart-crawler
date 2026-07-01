"""Crate&Barrel 采集器 —— 美国中高端家居电商，Salesforce Commerce Cloud
（Demandware）站，Akamai Bot Manager (sec-if-cpt / akam-sw.js) 防护。

实地验证（2026-05-24，本机直连 99.x.x.x，无代理）：

公开可达（curl_cffi 直连 200 OK）：
  - https://www.crateandbarrel.com/robots.txt → 暴露
    `Sitemap: https://www.crateandbarrel.com/assets/sitemap-index.xml`
  - sitemap-index.xml 内含 28 个子 sitemap，PDP 相关 3 个：
      · sitemap-pdp.xml      20,000 条在售 SKU（首选）
      · sitemap-pdp1.xml      6,079 条 SKU
      · sitemap-nla-pdp.xml  11,092 条 NLA（No Longer Available）SKU，跳过
  - 子 sitemap 是 Google Image Sitemap 扩展格式（image:image），每条 url 节点内置：
      · <loc>            PDP URL（`/<slug>/s<digits>`）
      · <image:loc>      高清主图（cb.scene7.com/is/image/Crate/<assetKey>）
      · <image:title>    完整商品名 + 图片角标（'XXX - image 0 of 12'）
  - 即 sitemap 本身就给出 SKU + 标题 + 主图 + slug → 不进 PDP 也能产出 1000 SKU。

被 Akamai 拦截（curl_cffi）：
  - 任何 PDP（`/<slug>/sNNNNN`）请求返回 200 + ~6.5 KB 的 akam-sw.js 挑战页
    （内容：`<div id="sec-if-cpt-container">` + akamai service worker 注册脚本 +
    XHR 拦截 `location.reload(true)`）。无 product JSON-LD / og:meta / 价格。
    全部 curl_cffi impersonate（chrome/chrome120/chrome131/safari17_0）相同结果。
  - 完整 Sec-Fetch-* + Referer + Sec-Ch-Ua + homepage warmup 也照旧吃挑战页。
  - 同 IP 反复触发后，StealthyFetcher (Camoufox) 进 PDP 直接 403 (~340 字节)。
  - 结论：本机直连 100% 无法拿到 PDP 真实正文；需住宅代理池 +
    StealthyFetcher（headless Camoufox）才能突破，单页成本 30-60s。

策略（与 overstock.py 对齐）：
  1. 顺序读 sitemap-pdp.xml → sitemap-pdp1.xml（跳过 nla-pdp），累积去重
     直到 sitemap 耗尽（环境变量/站点配置仍可显式缩小做 smoke test）。
  2. 单条 sitemap 节点即产出一行 product dict —— 字段够齐用于「商品标杆库」。
  3. CRATEBARREL_TRY_PDP=1 时尝试 PDP 兜底丰富（价格 / 评分 / 描述 /
     完整图组），用 StealthyFetcher 解 JSON-LD。默认关。
  4. CRATEBARREL_LIMIT=N 控制目标 SKU 数（默认近似不截断）。

可拿字段（sitemap-only 路径）：
  sku / spu       → URL 末段 'sNNNNN' 去前缀
  title           → image:title 截断 ' - image X of Y' 后部分
  image_urls      → [image:loc]（scene7 资产 URL，可拼 ?wid=2000 取高分辨率）
  product_url     → loc
  slug            → URL 第一段（用于二次定位）
  site / brand    → 站点配置

价格 / 描述 / 分类路径 / 库存：sitemap 不带，PDP 兜底打开时才有。

批C 收编（2026-06）：
  - curl 段改用 make_fetcher().get()，自动计 api_calls
  - stealth 段用 count_browser_fetch 包裹，成功计 browser_opens
  - 删 proxy 自管(_session 中 s.proxies)；_session() → _headers()
  - 删 creq import
"""
from __future__ import annotations

import json
import os
import re

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("CRATEBARREL_LIMIT", "999999"))
SITEMAP_INDEX = "https://www.crateandbarrel.com/assets/sitemap-index.xml"
TRY_BROWSE_MODEL_ENRICH = os.environ.get("CRATEBARREL_TRY_BROWSE_MODEL", "1") == "1"
TRY_PDP_ENRICH = os.environ.get("CRATEBARREL_TRY_PDP", "0") == "1"
PDP_ENRICH_BUDGET = int(os.environ.get("CRATEBARREL_PDP_BUDGET", "50"))

# 子 sitemap 命名 —— 在售优先，跳过 NLA（No Longer Available）
_PDP_SITEMAP_PREFER = ("sitemap-pdp.xml", "sitemap-pdp1.xml")
_PDP_SITEMAP_SKIP = ("sitemap-nla-pdp.xml",)

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_IMG_LOC_RE = re.compile(r"<image:loc>\s*(.*?)\s*</image:loc>")
_IMG_TITLE_RE = re.compile(r"<image:title>\s*(.*?)\s*</image:title>", re.S)
# PDP URL 末段：/sNNNNN（数字 SKU）
_PROD_URL_RE = re.compile(r"^https?://www\.crateandbarrel\.com/([^/?#]+)/s(\d+)/?$")
# image:title 形如 'Anneli Upholstered King Bed - image 0 of 12'
_IMG_TITLE_TAIL_RE = re.compile(r"\s*-\s*image\s+\d+\s+of\s+\d+\s*$", re.I)
# Akamai 挑战页标志（PDP 触发反爬时返回 200 但正文 <10KB 含这些 token）
_AKAMAI_MARKERS = (
    "sec-if-cpt-container",
    "sec-bc-tile-container",
    "akam-sw.js",
    "akamServiceWorkerInvoked",
    "Pardon Our Interruption",
    "Access Denied",
)


class CrateBarrelCrawler(BaseCrawler):
    platform = "cratebarrel"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(
            DEFAULT_LIMIT, limit, honor_persisted=False)

    # ------------------------------------------------------------------
    # headers  (replaces old _session — proxy handled by CrawlerFetcher)
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。

        Akamai BMP 只放行带浏览器特征的请求。sitemap 请求走 XML accept，
        PDP 请求走 HTML accept（_enrich_from_pdp 内覆盖）。
        """
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
        }

    # ------------------------------------------------------------------
    # main
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="cratebarrel")

        # ---- Step 1：sitemap_index ----
        try:
            res = fetcher.get(
                SITEMAP_INDEX,
                headers={**self._headers(), "Accept": "application/xml,text/xml,*/*"},
                timeout=30,
            )
            self.guard(res.status or 0, "sitemap_index")
            if (res.status or 0) != 200:
                result.notes.append(
                    f"⚠ sitemap_index 不可达（{res.status}）")
                return result
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ sitemap_index 异常: {exc}")
            return result

        all_subs = _LOC_RE.findall(res.text)
        # 先 prefer（在售）再其他 PDP 类，跳过 NLA
        prefer = [u for u in all_subs
                  if u.rsplit("/", 1)[-1] in _PDP_SITEMAP_PREFER]
        # 兜底：意外的其他 sitemap-pdp*.xml（防 C&B 加新分片）
        extra_pdp = [u for u in all_subs
                     if u.rsplit("/", 1)[-1].startswith("sitemap-pdp")
                     and u not in prefer
                     and u.rsplit("/", 1)[-1] not in _PDP_SITEMAP_SKIP]
        sub_sitemaps = prefer + extra_pdp
        result.notes.append(
            f"sitemap_index: 共 {len(all_subs)} 个子 sitemap，"
            f"PDP 类 {len(sub_sitemaps)}（跳过 NLA）")
        self.snapshot("sitemap_index", res.text)

        # ---- Step 2：扫子 sitemap → 商品 dict ----
        seen: set[str] = set()
        failed_sitemaps = 0
        for sm_url in sub_sitemaps:
            try:
                sm = fetcher.get(
                    sm_url,
                    headers={**self._headers(), "Accept": "application/xml,text/xml,*/*"},
                    timeout=60,
                )
                self.guard(sm.status or 0, f"sub:{sm_url}")
                if (sm.status or 0) != 200:
                    failed_sitemaps += 1
                    result.notes.append(
                        f"⚠ {sm_url.rsplit('/',1)[-1]} {sm.status}")
                    continue
            except BlockedError:
                raise
            except Exception as exc:
                failed_sitemaps += 1
                result.notes.append(
                    f"⚠ {sm_url.rsplit('/',1)[-1]} 异常: {exc}")
                continue

            self.snapshot(sm_url.rsplit("/", 1)[-1], sm.text[:500_000])

            parsed = 0
            for blk in _URL_BLOCK_RE.finditer(sm.text):
                row = self._parse_sitemap_entry(blk.group(1))
                if not row or row["sku"] in seen:
                    continue
                seen.add(row["sku"])
                if len(result.products) < self.limit:
                    result.products.append(row)
                    parsed += 1

            result.notes.append(
                f"{sm_url.rsplit('/',1)[-1]}: +{parsed} SKU "
                f"（累计入库 {len(result.products)} / sitemap 去重 {len(seen)}）")
            self.sleep()

        # ---- Step 3：轻量 JSON 端点丰富价格/描述/评分 ----
        if TRY_BROWSE_MODEL_ENRICH and result.products:
            ok = self._enrich_from_browse_model(result.products)
            result.notes.append(
                f"browse-model 价格兜底尝试 {len(result.products)}，成功 {ok}")

        # ---- Step 4（可选）：PDP 兜底丰富 ----
        if TRY_PDP_ENRICH and result.products:
            budget = min(PDP_ENRICH_BUDGET, len(result.products))
            ok = self._enrich_from_pdp(result.products[:budget])
            result.notes.append(
                f"PDP 兜底尝试 {budget}，成功 {ok}（住宅代理 + Camoufox 才能突破）")

        result.notes.append(
            f"采集 {len(result.products)} 个去重 SKU（sitemap-only 路径，"
            f"价格/描述/分类需 CRATEBARREL_TRY_PDP=1 + 住宅代理）")
        result.total_product_count = len(seen)
        if failed_sitemaps:
            result.coverage_complete = False
            result.coverage_code = "incomplete_discovery"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"Crate&Barrel 有 {failed_sitemaps}/{len(sub_sitemaps)} 个 PDP sitemap "
                "未成功读取，本次分母只包含成功读取的分片。"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "修复 sitemap 访问失败后重跑该站点。"
        if len(result.products) < len(seen):
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"Crate&Barrel sitemap 共 {len(seen)} 个在售 SKU，"
                f"本次只产出 {len(result.products)} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 CRATEBARREL_LIMIT 后重跑。"
        return result

    # ------------------------------------------------------------------
    # 单条 sitemap url 节点 → product dict
    # ------------------------------------------------------------------
    def _parse_sitemap_entry(self, block: str) -> dict | None:
        m_loc = _LOC_RE.search(block)
        if not m_loc:
            return None
        url = m_loc.group(1).strip()
        mu = _PROD_URL_RE.match(url.rstrip("/"))
        if not mu:
            return None
        slug, sku = mu.group(1), mu.group(2)

        # 主图（sitemap 给一张主图）
        img = None
        m_img = _IMG_LOC_RE.search(block)
        if m_img:
            img = m_img.group(1).strip()

        # 标题：image:title 去掉 ' - image X of Y' 尾巴
        title = None
        m_t = _IMG_TITLE_RE.search(block)
        if m_t:
            raw = m_t.group(1).strip()
            title = _IMG_TITLE_TAIL_RE.sub("", raw).strip()
        # 退化：slug → 'Anneli Upholstered King Bed'
        if not title:
            title = slug.replace("-", " ").strip().title()
        if not title:
            return None

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title,
            "description": None,             # PDP 才有
            "image_urls": [img] if img else [],
            "category_path": None,            # 需 PDP BreadcrumbList
            "sale_price": None,
            "original_price": None,
            "currency": "USD",
            "status": "on_sale",              # 出现在 sitemap-pdp（非 nla）即在售
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
            "_skip_price_history_if_no_price": True,
        }

    def _enrich_from_browse_model(self, rows: list[dict]) -> int:
        """Use Crate's JSON browse model endpoint.

        PDP HTML is Akamai-gated, but the numeric SKU endpoint is readable in
        production and carries the current price:
        /single-product-page/get-browse-model/{sku}
        """
        fetcher = self.make_fetcher(kind="product", source="cratebarrel_browse_model")
        ok = 0
        for row in rows:
            sku = str(row.get("sku") or "").strip()
            if not sku or not sku.isdigit():
                continue
            url = f"{self.base}/single-product-page/get-browse-model/{sku}"
            try:
                res = fetcher.get(
                    url,
                    headers={
                        **self._headers(),
                        "Accept": "application/json,text/plain,*/*",
                        "Referer": self.base + "/",
                    },
                    timeout=25,
                )
            except Exception:
                continue
            if (res.status or 0) != 200 or not (res.text or "").lstrip().startswith("{"):
                continue
            try:
                data = json.loads(res.text)
            except json.JSONDecodeError:
                continue
            if self._merge_from_browse_model(row, data):
                ok += 1
            self.sleep()
        return ok

    def _merge_from_browse_model(self, row: dict, data: dict) -> bool:
        browse = data.get("browseDto") if isinstance(data, dict) else {}
        if not isinstance(browse, dict):
            browse = {}
        rewards = browse.get("rewards") if isinstance(browse.get("rewards"), dict) else {}
        price = (
            self._num(rewards.get("currentPrice"))
            or self._find_number(browse, "currentPrice")
            or self._find_number(browse, "salePrice")
            or self._find_number(browse, "price")
        )
        original = (
            self._find_number(browse, "regularPrice")
            or self._find_number(browse, "listPrice")
            or self._find_number(browse, "wasPrice")
            or price
        )
        changed = False
        if price is not None and price > 0:
            row["sale_price"] = price
            row["original_price"] = original or price
            row["currency"] = row.get("currency") or "USD"
            changed = True

        rating = (
            self._find_number(browse, "ratingValue")
            or self._find_number(browse, "averageRating")
            or self._find_number(browse, "rating")
        )
        if rating is not None:
            row["ratings"] = rating
            changed = True
        review_count = (
            self._find_int(browse, "reviewCount")
            or self._find_int(browse, "reviewsCount")
            or self._find_int(browse, "ratingCount")
        )
        if review_count is not None:
            row["review_count"] = review_count
            changed = True

        description = self._find_text(browse, "description")
        if description and not row.get("description"):
            row["description"] = description
            changed = True

        image = (
            self._find_text(browse, "imageUrl")
            or self._find_text(browse, "heroImageUrl")
            or self._find_text(browse, "image")
        )
        if image:
            images = list(row.get("image_urls") or [])
            if image not in images:
                images.append(image)
                row["image_urls"] = images[:10]
                changed = True

        availability = str(
            self._find_text(browse, "availability")
            or self._find_text(browse, "stockStatus")
            or ""
        ).lower()
        if "out" in availability and "stock" in availability:
            row["status"] = "out_of_stock"
            changed = True
        return changed

    # ------------------------------------------------------------------
    # PDP 兜底（默认关）—— 试 make_fetcher().get() → 命中 Akamai 则 StealthyFetcher
    # ------------------------------------------------------------------
    def _enrich_from_pdp(self, rows: list[dict]) -> int:
        """对前 N 行尝试进 PDP 抓 JSON-LD，补价格/评分/描述。

        - curl_cffi 段（make_fetcher().get()）99% 拿到 Akamai 挑战页
          （identified by _is_akamai_challenge）
        - StealthyFetcher (Camoufox) 在住宅代理 + 持久 profile 下可突破
          （count_browser_fetch 包裹，成功计 browser_opens）
        - 单页平均 30-60s，1000 SKU 全开销 ~10-16h，故默认仅 fallback 前 50 条
        """
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return 0

        fetcher = self.make_fetcher(kind="product", source="cratebarrel")
        ok = 0
        kw = stealth_kwargs(
            proxy=self.proxy,
            country="US",
            persist_profile_key=f"cratebarrel_{self.site.site}",
            timeout_ms=60000,
            real_chrome=False,
            solve_cloudflare=False,   # Akamai 不是 Cloudflare
        )
        for row in rows:
            html: str | None = None
            url = row["product_url"]
            # 路径 1：make_fetcher().get()（成本最低，计 api_calls）
            try:
                res = fetcher.get(url, headers=self._headers(), timeout=30)
                if (res.status or 0) == 200 and not self._is_akamai_challenge(res.text):
                    html = res.text
            except Exception:
                pass
            # 路径 2：StealthyFetcher 兜底（count_browser_fetch 计 browser_opens）
            if html is None:
                try:
                    def _success(page) -> bool:
                        return (
                            getattr(page, "status", None) == 200
                            and bool(getattr(page, "html_content", None))
                            and not self._is_akamai_challenge(
                                getattr(page, "html_content", "") or "")
                        )

                    page = self.count_browser_fetch(
                        lambda: StealthyFetcher.fetch(url, **kw),
                        success=_success,
                    )
                    if (getattr(page, "status", None) == 200
                            and page.html_content
                            and not self._is_akamai_challenge(page.html_content)):
                        html = page.html_content
                except Exception:
                    pass

            if not html:
                continue

            self.snapshot(row["sku"], html)
            ld = self._extract_jsonld_product(html)
            if ld:
                self._merge_from_jsonld(row, ld)
                ok += 1
            else:
                # 退化：og: meta
                self._merge_from_og(row, html)
            self.sleep()
        return ok

    @staticmethod
    def _is_akamai_challenge(html: str) -> bool:
        """识别 Akamai BMP 挑战页 / 拒绝页。"""
        if not html:
            return True
        if len(html) < 10_000:        # 正常 PDP > 200KB；挑战页 ~6.5KB
            return True
        return any(m in html for m in _AKAMAI_MARKERS)

    @staticmethod
    def _extract_jsonld_product(html: str) -> dict | None:
        for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.S):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            candidates = data if isinstance(data, list) else [data]
            # @graph 容器
            for c in list(candidates):
                if isinstance(c, dict) and isinstance(c.get("@graph"), list):
                    candidates.extend(c["@graph"])
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                t = c.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    return c
        return None

    def _merge_from_jsonld(self, row: dict, ld: dict) -> None:
        # 价格 / 货币 / 库存
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = self._num(offers.get("price")
                          or offers.get("lowPrice"))
        if price is not None:
            row["sale_price"] = price
            row["original_price"] = self._num(
                offers.get("highPrice")) or price
        cur = offers.get("priceCurrency")
        if cur:
            row["currency"] = cur
        avail = str(offers.get("availability", "")).lower()
        if "outofstock" in avail or "out of stock" in avail:
            row["status"] = "out_of_stock"

        # 评分
        rating = ld.get("aggregateRating") or {}
        if isinstance(rating, dict):
            r = self._num(rating.get("ratingValue"))
            if r is not None:
                row["ratings"] = r
            rc = rating.get("reviewCount") or rating.get("ratingCount")
            if rc is not None:
                try:
                    row["review_count"] = int(rc)
                except (TypeError, ValueError):
                    pass

        # 描述
        desc = ld.get("description")
        if desc and not row.get("description"):
            row["description"] = desc

        # SKU 优先用 JSON-LD 的（更准），仍保留 sitemap 路径派生的 SKU 作为 spu
        sku2 = ld.get("sku") or ld.get("mpn")
        if sku2:
            row["sku"] = str(sku2).strip()

        # 图组：合并 JSON-LD 的 image 数组
        imgs = ld.get("image")
        if imgs:
            if isinstance(imgs, str):
                imgs = [imgs]
            merged = list(row.get("image_urls") or [])
            for u in imgs:
                if u and u not in merged:
                    merged.append(u)
            row["image_urls"] = merged[:10]

    def _merge_from_og(self, row: dict, html: str) -> None:
        """JSON-LD 缺失时退化：靠 og:price:amount / og:title。"""
        def meta(prop: str) -> str | None:
            m = re.search(
                r'<meta[^>]+(?:property|name)="' + re.escape(prop)
                + r'"[^>]+content="([^"]+)"', html, re.I)
            return m.group(1) if m else None

        price = self._num(meta("product:price:amount")
                          or meta("og:price:amount"))
        if price is not None:
            row["sale_price"] = price
            row["original_price"] = price
        cur = meta("product:price:currency") or meta("og:price:currency")
        if cur:
            row["currency"] = cur
        og_desc = meta("og:description")
        if og_desc and not row.get("description"):
            row["description"] = og_desc

    @staticmethod
    def _num(v):
        if v is None:
            return None
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None

    @classmethod
    def _find_number(cls, data, key: str) -> float | None:
        val = cls._find_value(data, key)
        return cls._num(val)

    @classmethod
    def _find_int(cls, data, key: str) -> int | None:
        val = cls._find_value(data, key)
        if val is None:
            return None
        try:
            return int(float(str(val).replace(",", "")))
        except ValueError:
            return None

    @classmethod
    def _find_text(cls, data, key: str) -> str | None:
        val = cls._find_value(data, key)
        if val is None or isinstance(val, (dict, list)):
            return None
        text = str(val).strip()
        return text or None

    @classmethod
    def _find_value(cls, data, key: str):
        if isinstance(data, dict):
            for k, v in data.items():
                if k == key:
                    return v
            for v in data.values():
                found = cls._find_value(v, key)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = cls._find_value(item, key)
                if found is not None:
                    return found
        return None
