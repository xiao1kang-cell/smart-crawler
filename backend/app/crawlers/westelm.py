"""West Elm 采集器 —— Williams-Sonoma 旗下美国高端家居电商。

平台：Salesforce Commerce Cloud（站本身托管在 Akamai NetStorage，前端 React +
window.__INITIAL_STATE__ 注入；非典型 SFCC URL）。

实地验证（2026-05-24，本机直连 99.x.x.x，无代理）：
- ✅ robots.txt 暴露 7 个 sitemap，其中 product-sitemap-index.xml → 单个
  product-sitemap-1.xml.gz（gzip，约 240KB，解压后 ~1.7MB），含 ~10k 个
  /products/<slug>/ URL（含 facet/SKU 变体可达更多）。本次取首份 ~10k 已超 1000 限额。
- ✅ 商品 URL 末段 slug 形如 `<name>-<code>` —— code 单字母 + 数字（h12385 / b4193 /
  w4719 / d15889 / mp798 / t7984 / e3056 / 等），用作 spu/groupId。
- ❌ 商品页 **没有 Product / Offer JSON-LD**（只有 og:* meta）。
- ✅ 商品页 SSR 注入 `window.__INITIAL_STATE__ = {...}` —— 含 productDetails:
    · title / breadcrumbs / aggregatePrice (low/highSellingPrice & lowRetailPrice)
    · subsets[0].definitions.skus[id] —— 真实 SKU id（如 619421），可多变体
    · 单 SKU 的 price.regularPrice / price.sellingPrice / inventory.availability
    · images（top-level 主图 path 列表，需配 assets.weimgs.com CDN 拼接）
    · copyBlocks（metadescription / pagetitle / romancecopy HTML）
    · primaryCategoryId / superCategoryId
  评分/评论数走 BazaarVoice 独立 API（本路径不抓，留空）。
- ✅ 反爬实测：curl_cffi(impersonate=chrome) 单 session 1.2s 间隔连发 10 个
  PDP 全部 200，~700KB-900KB SSR HTML，无 challenge / 429。Akamai 在
  sitemap 路径上只做"非浏览器指纹"过滤（裸 curl 403，curl_cffi 200）。

策略：
  1. 读 product-sitemap-index.xml → 取子 sitemap（.gz 需 gzip 解压）
  2. 抽 /products/<slug>/ URL，按 limit 截断
  3. curl_cffi 单 session 顺序抓 PDP，提取 __INITIAL_STATE__
  4. 解析 productDetails —— 多 SKU 展开为多条记录（spu = groupId, sku = sku id）
  5. 反爬命中（403/429/页面 < 200KB）→ stealth fallback（Camoufox），预算 5 次

字段输出：sku / spu / title / description / image_urls / category_path /
sale_price / original_price / currency=USD / status / product_url /
site / brand。ratings/review_count 走 BazaarVoice，本路径留空。

批C 收编（2026-06）：
  - curl 段改用 make_fetcher().get()，自动计 api_calls
  - stealth 段用 count_browser_fetch 包裹，成功计 browser_opens
  - 删 proxy 自管(_session 改为 _headers())；保留 guard / _is_blocked_body / parse
  - solve_cloudflare=False（Akamai 非 Cloudflare）原样保留
"""
from __future__ import annotations

import gzip
import html as _html
import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("WESTELM_LIMIT", "999999"))
MAX_ELAPSED_SEC = float(os.environ.get("WESTELM_MAX_ELAPSED_SEC", "0"))
SITEMAP_INDEX = ("https://www.westelm.com/netstorage/sitemaps/"
                 "product-sitemap-index.xml")
ASSETS_CDN = "https://assets.weimgs.com/weimgs/ab/images/wcm/"

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
# /products/<slug>-<code>/  —— code 末段如 h12385 / b4193 / mp798
_PROD_URL_RE = re.compile(
    r"^https?://www\.westelm\.com/products/([a-z0-9\-]+?)-([a-z]{1,3}\d{2,6})/?$",
    re.I)
# __INITIAL_STATE__ 的起始锚点（赋值右侧是巨大 JSON，靠括号配对截取）
_INIT_STATE_ANCHOR = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
# Akamai NetStorage assets.weimgs.com 图片 URL（PDP HTML 内出现的产品图）
_IMG_URL_RE = re.compile(
    r'https://assets\.weimgs\.com/weimgs/[a-z]{1,3}/images/wcm/'
    r'products/\d+/\d+/[a-z0-9\-]+\.(?:jpg|jpeg|png|webp)', re.I)


class WestElmCrawler(BaseCrawler):
    platform = "westelm"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit,
                                         honor_persisted=False)
        # 实测 1.2s 间隔连发 10 个全 200，留点余量保守 1.5s（含 jitter ~2.4s）
        self.delay = float(os.environ.get("WESTELM_DELAY", "1.5"))

    # ---------- headers (proxy handled by CrawlerFetcher) ----------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.westelm.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    # ---------- main ----------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="westelm")
        started = time.monotonic()

        # Warmup：访问首页建立会话 / 预热 Akamai cookie（计入 api_calls）
        try:
            fetcher.get(
                self.base + "/",
                headers=self._headers(),
                timeout=30,
            )
        except Exception:
            pass

        urls = self._collect_pdp_urls(fetcher, result)
        if not urls:
            result.notes.append("⚠ 未收集到任何 PDP URL —— 中止")
            return result

        targets = urls[: self.limit]
        result.total_product_count = len(urls)
        _persist_job_total_product_count(self.job_id, len(urls))
        result.notes.append(
            f"sitemap 累计 {len(urls)} PDP URL，本次抓取 {len(targets)}")
        if len(targets) < len(urls):
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"WestElm sitemap 共 {len(urls)} 个商品，本次只计划抓取 {len(targets)} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 WESTELM_LIMIT 后重跑。"

        import time as _t

        ok = fail = blocked = stealth_used = 0
        consecutive_block = 0
        BLOCK_BREAK = 6
        BLOCK_COOLDOWN_S = 60
        SESSION_ROTATE = 200
        STEALTH_USE = (os.environ.get("WESTELM_USE_STEALTH", "0") == "1")
        STEALTH_BUDGET = 5

        for i, url in enumerate(targets):
            if MAX_ELAPSED_SEC > 0 and time.monotonic() - started >= MAX_ELAPSED_SEC:
                result.notes.append(
                    f"达到 WESTELM_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"提前返回已解析结果（ok={ok}, fail={fail}, blocked={blocked}）")
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"达到 WESTELM_MAX_ELAPSED_SEC={MAX_ELAPSED_SEC:g}s，"
                    f"本次只解析 {ok}/{len(targets)} 个商品"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "放宽 WESTELM_MAX_ELAPSED_SEC 或拆分失败商品重抓。"
                )
                break
            if i > 0 and i % SESSION_ROTATE == 0:
                fetcher = self.make_fetcher(kind="product", source="westelm")
                result.notes.append(
                    f"… 第 {i} 条，主动 rotate fetcher（已抓 {ok}）")

            try:
                html, code = self._fetch_pdp(fetcher, url)

                if code == 404:
                    consecutive_block = 0
                    self.sleep()
                    continue

                is_block = (
                    code in (401, 403, 429, 451)
                    or (html is not None and self._is_blocked_body(html)))
                if is_block:
                    blocked += 1
                    consecutive_block += 1
                    if blocked <= 3 or consecutive_block in (1, 5):
                        result.notes.append(
                            f"⚠ {code or 'body-block'} (连击 {consecutive_block}) "
                            f"@ ok={ok}/{i} {url[-60:]}")

                    if consecutive_block == 1:
                        result.notes.append(
                            f"  → sleep {BLOCK_COOLDOWN_S}s + 重建 fetcher")
                        _t.sleep(BLOCK_COOLDOWN_S)
                        fetcher = self.make_fetcher(kind="product", source="westelm")
                        fail += 1
                        continue
                    if consecutive_block == 2:
                        result.notes.append(
                            f"  → 连续 block，sleep {BLOCK_COOLDOWN_S*2}s")
                        _t.sleep(BLOCK_COOLDOWN_S * 2)
                        fetcher = self.make_fetcher(kind="product", source="westelm")
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
                                    f"westelm 连续 {consecutive_block} 次封锁，熔断")
                            self.sleep()
                            continue
                    else:
                        fail += 1
                        if consecutive_block >= BLOCK_BREAK:
                            raise BlockedError(
                                f"westelm 连续 {consecutive_block} 次封锁，熔断"
                                f"（已抓 {ok}）")
                        _t.sleep(BLOCK_COOLDOWN_S * consecutive_block)
                        fetcher = self.make_fetcher(kind="product", source="westelm")
                        continue
                elif code == 200:
                    consecutive_block = 0

                if not html:
                    fail += 1
                    self.sleep()
                    continue

                rows = self._parse_product(html, url)
                if rows:
                    # 同一 PDP 多 SKU 变体 —— 全展开为多条记录
                    self.snapshot(rows[0]["spu"], html)
                    result.products.extend(rows)
                    ok += len(rows)
                    if ok and (ok // 50) > ((ok - len(rows)) // 50):
                        result.notes.append(
                            f"  进度 ok={ok} blocked={blocked}")
                else:
                    fail += 1
            except BlockedError:
                raise
            except Exception as exc:
                fail += 1
                if fail <= 5:
                    result.notes.append(f"跳过 {url[-60:]}: {exc}")

            # 达到 limit 提前退出（多变体可能让 ok 超过 limit）
            if len(result.products) >= self.limit:
                break

            self.sleep()

        result.notes.append(
            f"成功 {ok}/{len(targets)} · 失败 {fail} · 反爬命中 {blocked} · "
            f"stealth fallback {stealth_used}")
        result.total_product_count = max(
            int(result.total_product_count or 0),
            len(result.products),
        )
        return result

    # ---------- sitemap ----------
    def _collect_pdp_urls(self, fetcher,
                          result: CrawlResult) -> list[str]:
        """读 product-sitemap-index → 子 sitemap (.gz) → 列出全量 product URL。"""
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

        subs = _LOC_RE.findall(res.text)
        result.notes.append(f"sitemap_index: {len(subs)} 个子 sitemap")
        self.snapshot("sitemap_index", res.text)

        urls: list[str] = []
        seen: set[str] = set()
        for sm in subs:
            try:
                r = fetcher.get(sm, headers=self._headers(), timeout=60)
                if (r.status or 0) != 200:
                    result.notes.append(
                        f"⚠ {sm.rsplit('/',1)[-1]} {r.status}")
                    continue
                # 子 sitemap 大概率 .xml.gz
                content = r.content
                if sm.endswith(".gz") or content[:2] == b"\x1f\x8b":
                    try:
                        text = gzip.decompress(content).decode(
                            "utf-8", errors="replace")
                    except Exception:
                        text = r.text     # 已被 curl_cffi 自动解压
                else:
                    text = r.text

                count_before = len(urls)
                for u in _LOC_RE.findall(text):
                    if "/products/" not in u or u in seen:
                        continue
                    # 仅保留可解析 spu 的合法 PDP URL
                    if not _PROD_URL_RE.match(u.rstrip("/") + "/"):
                        continue
                    seen.add(u)
                    urls.append(u)
                result.notes.append(
                    f"{sm.rsplit('/',1)[-1]}: +{len(urls)-count_before} URL "
                    f"（累计 {len(urls)}）")
            except Exception as exc:
                result.notes.append(
                    f"⚠ {sm.rsplit('/',1)[-1]} 异常: {exc}")
                continue
            self.sleep()
        return urls

    # ---------- fetch ----------
    def _fetch_pdp(self, fetcher,
                   url: str) -> tuple[str | None, int]:
        """返回 (html_or_None, status_code)。"""
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
        solve_cloudflare=False（Akamai 非 Cloudflare）全部原样保留，
        只在最外层套计数。
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
                persist_profile_key=f"westelm_{self.site.site}",
                timeout_ms=60000,
                solve_cloudflare=False,    # Akamai 非 Cloudflare
            )

            def _do_fetch():
                return StealthyFetcher.fetch(url, **kw)

            # 成功标准：status == 200 且有 html_content 或 body（westelm 原判断）
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
        """West Elm 正常 PDP ~700KB-900KB；< 200KB 几乎必是错误页 / challenge。"""
        if not html:
            return True
        if len(html) < 200_000:
            # 但首页/小品类页可能合法 < 200KB，按内容关键字加确认
            markers = ("Access Denied", "Pardon Our Interruption",
                       "px-captcha", "/_Incapsula_Resource",
                       "Page Not Found", "404", "Reference #")
            return any(m in html for m in markers) or len(html) < 50_000
        return False

    # ---------- parse ----------
    def _parse_product(self, html: str, url: str) -> list[dict]:
        """解析 PDP 的 __INITIAL_STATE__，展开多 SKU 变体为多条记录。"""
        init = self._extract_initial_state(html)
        if not init:
            return []

        pd = (init.get("product") or {}).get("productDetails") or {}
        if not pd:
            return []

        group_id = pd.get("groupId") or self._slug_to_groupid(url)
        title = pd.get("title")
        if not group_id or not title:
            return []
        title = _html.unescape(title)

        category_path = self._breadcrumb(pd)
        description = self._description(pd)

        # subsets[].definitions.skus —— 真实 SKU 字典
        subsets = pd.get("subsets") or []
        skus_dict: dict = {}
        if subsets and isinstance(subsets[0], dict):
            defs = subsets[0].get("definitions") or {}
            skus_dict = defs.get("skus") or {}

        # 图片：HTML 内 weimgs.com 全集（一次扫描，去重保序）
        all_images = self._collect_images(html)

        rows: list[dict] = []
        currency = "USD"

        if skus_dict:
            for sku_id, sku in skus_dict.items():
                rows.append(self._row_from_sku(
                    sku_id=str(sku_id),
                    sku=sku,
                    group_id=group_id,
                    title=title,
                    description=description,
                    category_path=category_path,
                    images=all_images,
                    currency=currency,
                    url=url,
                    pd=pd,
                ))
        else:
            # 退化：没有变体（极少见），用 aggregatePrice + groupId 当 SKU
            agg = pd.get("aggregatePrice") or {}
            sale = self._num(agg.get("lowSellingPrice"))
            orig = self._num(agg.get("lowRetailPrice")) or sale
            rows.append({
                "sku": str(group_id),
                "spu": str(group_id),
                "title": title,
                "description": description,
                "image_urls": all_images[:10],
                "category_path": category_path,
                "sale_price": sale,
                "original_price": orig,
                "currency": currency,
                "status": "on_sale" if pd.get("isAvailable") else "out_of_stock",
                "product_url": url,
                "site": self.site.site,
                "brand": self.site.brand,
            })

        return rows

    # ---------- helpers ----------
    @staticmethod
    def _extract_initial_state(html: str) -> dict | None:
        """从 SSR HTML 抽取 window.__INITIAL_STATE__（巨型 JSON，靠括号配对截取）。"""
        m = _INIT_STATE_ANCHOR.search(html)
        if not m:
            return None
        start = m.end()
        if start >= len(html) or html[start] != "{":
            return None
        depth = 0
        in_str = False
        esc = False
        i = start
        while i < len(html):
            c = html[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        block = html[start:i + 1]
                        try:
                            return json.loads(block)
                        except json.JSONDecodeError:
                            return None
            i += 1
        return None

    @staticmethod
    def _slug_to_groupid(url: str) -> str | None:
        m = _PROD_URL_RE.match(url.rstrip("/") + "/")
        if not m:
            return None
        return f"{m.group(1)}-{m.group(2)}".lower()

    @staticmethod
    def _breadcrumb(pd: dict) -> str | None:
        crumbs = pd.get("breadcrumbs") or []
        labels: list[str] = []
        for c in crumbs:
            if not isinstance(c, dict):
                continue
            label = c.get("label")
            if label and label.lower() not in ("home", ""):
                labels.append(label)
        return "/".join(labels[:4]) or None

    @staticmethod
    def _description(pd: dict) -> str | None:
        """从 copyBlocks 拿 metadescription（纯文本），退化用 romancecopy（去 HTML）。"""
        blocks = pd.get("copyBlocks") or []
        meta_desc = None
        romance = None
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bid = b.get("id")
            val = b.get("value")
            if not val:
                continue
            if bid == "metadescription":
                meta_desc = val
            elif bid == "romancecopy":
                romance = val
        text = meta_desc or romance
        if not text:
            return None
        # 去 HTML 标签 + 解 entity（&#174; → ®, &quot; → "）
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @staticmethod
    def _collect_images(html: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        # 优先 og:image
        m = re.search(
            r'<meta\s+content="(https://assets\.weimgs\.com/[^"]+)"\s+'
            r'property="og:image"', html)
        if m:
            seen.add(m.group(1))
            out.append(m.group(1))
        for m in _IMG_URL_RE.finditer(html):
            u = m.group(0)
            # 去掉 swatch 小图（路径里通常是 -x.jpg 之类的色卡缩略）
            if u.endswith("-x.jpg"):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out[:10]

    def _row_from_sku(self, *, sku_id: str, sku: dict, group_id: str,
                      title: str, description: str | None,
                      category_path: str | None, images: list[str],
                      currency: str, url: str, pd: dict) -> dict:
        price = sku.get("price") or {}
        sale = self._num(price.get("sellingPrice"))
        orig = self._num(price.get("retailPrice")) or self._num(
            price.get("regularPrice")) or sale

        # 变体名（如 "Sand" / "72x74"）—— 拼到标题更可读
        sku_name = sku.get("name")
        if sku_name:
            sku_name = _html.unescape(sku_name)
        if sku_name and sku_name != title:
            full_title = f"{title} — {sku_name}"
        else:
            full_title = title

        # 库存：availability=BACK_ORDERED / IN_STOCK / OUT_OF_STOCK
        inv = sku.get("inventory") or {}
        avail = (inv.get("availability") or "").upper()
        avail_block = sku.get("availability") or {}
        if avail in ("OUT_OF_STOCK", "NLA"):
            status = "out_of_stock"
        elif avail_block and not avail_block.get("available", True):
            status = "out_of_stock"
        elif avail in ("DISCONTINUED",):
            status = "discontinued"
        else:
            status = "on_sale"

        # 属性：color / fabric / material / length / width
        props = sku.get("properties") or {}
        attributes = {k: v for k, v in props.items()
                      if k in ("color", "fabric", "material", "length",
                              "width", "size")}

        # flags → label / tags
        flags = sku.get("flags") or {}
        label = None
        tags: list[str] = []
        for f in (flags.get("top") or []):
            if isinstance(f, dict) and f.get("id"):
                fid = f["id"]
                tags.append(fid)
                if fid in ("bestseller", "new", "topRated") and not label:
                    label = fid.upper()

        return {
            "sku": str(sku_id),
            "spu": str(group_id),
            "title": full_title,
            "description": description,
            "image_urls": images,
            "category_path": category_path,
            "sale_price": sale,
            "original_price": orig,
            "currency": currency,
            "attributes": attributes or None,
            "status": status,
            "tags": tags or None,
            "label": label,
            "has_free_shipping": "freeship" in tags or None,
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    @staticmethod
    def _num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None


def _persist_job_total_product_count(job_id: int | None, total: int) -> None:
    """让运行中的任务也能展示本次全量分母。"""
    if not job_id or total < 0:
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
            job.total_product_count = int(total)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
