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
"""
from __future__ import annotations

import gzip
import html as _html
import json
import os
import re

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("WESTELM_LIMIT", "1000"))
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
        self.limit = limit if limit is not None else DEFAULT_LIMIT
        # 实测 1.2s 间隔连发 10 个全 200，留点余量保守 1.5s（含 jitter ~2.4s）
        self.delay = float(os.environ.get("WESTELM_DELAY", "1.5"))

    # ---------- session ----------
    def _session(self, warmup: bool = False) -> creq.Session:
        """构建 curl_cffi 会话 —— Akamai 看 UA + TLS 指纹，缺一不可。"""
        s = creq.Session(impersonate="chrome")
        s.headers.update({
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
        })
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        if warmup:
            try:
                s.get(self.base + "/", timeout=30)
            except Exception:
                pass
        return s

    # ---------- main ----------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session(warmup=True)

        urls = self._collect_pdp_urls(sess, result)
        if not urls:
            result.notes.append("⚠ 未收集到任何 PDP URL —— 中止")
            return result

        targets = urls[: self.limit]
        result.notes.append(
            f"sitemap 累计 {len(urls)} PDP URL，本次抓取 {len(targets)}")

        import time as _t

        ok = fail = blocked = stealth_used = 0
        consecutive_block = 0
        BLOCK_BREAK = 6
        BLOCK_COOLDOWN_S = 60
        SESSION_ROTATE = 200
        STEALTH_USE = (os.environ.get("WESTELM_USE_STEALTH", "0") == "1")
        STEALTH_BUDGET = 5

        for i, url in enumerate(targets):
            if i > 0 and i % SESSION_ROTATE == 0:
                sess = self._session(warmup=True)
                result.notes.append(
                    f"… 第 {i} 条，主动 rotate session（已抓 {ok}）")

            try:
                html, code = self._fetch_pdp(sess, url)

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
                            f"  → sleep {BLOCK_COOLDOWN_S}s + 重建 session")
                        _t.sleep(BLOCK_COOLDOWN_S)
                        sess = self._session(warmup=True)
                        fail += 1
                        continue
                    if consecutive_block == 2:
                        result.notes.append(
                            f"  → 连续 block，sleep {BLOCK_COOLDOWN_S*2}s")
                        _t.sleep(BLOCK_COOLDOWN_S * 2)
                        sess = self._session(warmup=True)
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
                        sess = self._session(warmup=True)
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
        return result

    # ---------- sitemap ----------
    def _collect_pdp_urls(self, sess: creq.Session,
                          result: CrawlResult) -> list[str]:
        """读 product-sitemap-index → 子 sitemap (.gz) → 列出 product URL。"""
        try:
            idx = sess.get(SITEMAP_INDEX, timeout=30)
            self.guard(idx.status_code, "sitemap_index")
            if idx.status_code != 200:
                result.notes.append(
                    f"⚠ sitemap_index 返回 {idx.status_code}")
                return []
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ sitemap_index 不可达: {exc}")
            return []

        subs = _LOC_RE.findall(idx.text)
        result.notes.append(f"sitemap_index: {len(subs)} 个子 sitemap")
        self.snapshot("sitemap_index", idx.text)

        urls: list[str] = []
        seen: set[str] = set()
        for sm in subs:
            if len(urls) >= self.limit:
                break
            try:
                r = sess.get(sm, timeout=60)
                if r.status_code != 200:
                    result.notes.append(
                        f"⚠ {sm.rsplit('/',1)[-1]} {r.status_code}")
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
                    if len(urls) >= self.limit:
                        break
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
    def _fetch_pdp(self, sess: creq.Session,
                   url: str) -> tuple[str | None, int]:
        """返回 (html_or_None, status_code)。"""
        try:
            r = sess.get(url, timeout=30)
        except Exception:
            return None, 0
        if r.status_code == 200:
            return r.text, 200
        return None, r.status_code

    def _fetch_via_stealth(self, url: str) -> str | None:
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
            page = StealthyFetcher.fetch(url, **kw)
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
