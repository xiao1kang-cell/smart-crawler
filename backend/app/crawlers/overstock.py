"""Overstock.com 采集器 —— 美国家居电商，Next.js 前端 + Akamai Bot Manager 防护。

实地验证（2026-05-24）：
- ✅ `api.overstock.com/sitemaps/overstock-v3/us/sitemap.xml` 公开可达（curl_cffi 直连，
  无任何反爬）。sitemap_index 下挂 60+ 个 products{N}.xml，每个 25,000 URL。
- ✅ 子 sitemap 是 Google Image Sitemap 扩展格式，每条 url 节点内置：
  `<loc>` 商品 URL、`<lastmod>` 更新时间、`<i:image><i:loc>` 高清图（通常 1-3 张）。
  即 sitemap 本身就给出 SKU + slug + 分类 + 图片 + 更新时间，无需进 PDP。
- ❌ 商品详情页（`/<cat>/<slug>/<id>/product.html`）被 Akamai Bot Manager 拦截，
  curl_cffi 返回 ~2.6 KB 的 sec-if-cpt JS 挑战页（HTTP 200 但内容是挑战）；
  StealthyFetcher（含 real_chrome / persistent profile / 先 warm 首页）也吃 403。
  Akamai 对 PDP 比首页严格——首页 200，PDP 立即 challenge。
- ❌ 没有公开 JSON API：`/api/...`、`api.overstock.com/products/{id}` 等均 404。
- ✅ 首页 `www.overstock.com/` 可直接 200 GET（304 KB Next.js streaming HTML）。

策略：**sitemap-first，只解析 sitemap 字段**（站点已主动暴露的合法元数据），
不去硬刚 Akamai 的 PDP 反爬。可拿到的字段：
  sku / spu       → URL 路径中的纯数字 ID
  title           → slug 解码（'Simple-Living-Mavis-Espresso-Writing-Desk' → 'Simple Living Mavis Espresso Writing Desk'）
  category_path   → URL 首段（'Home-Garden' → 'Home & Garden'）
  image_urls      → <i:image><i:loc> 列表
  product_url     → <loc>
  published_at    → <lastmod>
  site / brand    → 站点配置
价格 / 评分 / 库存 / 详细描述：需 PDP，本路径无法拿到，留空。

如未来 Akamai 放松或拿到住宅代理 + cookie warmup 方案，可在 _enrich_from_pdp()
里激活 StealthyFetcher 路径解析 JSON-LD `<script type="application/ld+json">`
里的 Product schema（offers.price / aggregateRating / availability）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import unquote

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("OVERSTOCK_LIMIT", "1000"))
SITEMAP_INDEX = ("https://api.overstock.com/sitemaps/overstock-v3/"
                 "us/sitemap.xml")
TRY_PDP_ENRICH = os.environ.get("OVERSTOCK_TRY_PDP", "0") == "1"

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
# 整条 <url>...</url> 块；以解析其中 <loc> / <lastmod> / <i:image><i:loc>
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>")
_IMG_LOC_RE = re.compile(r"<i:loc>\s*(.*?)\s*</i:loc>")
# 商品 URL 末段：/<numericId>/product.html
_PROD_URL_RE = re.compile(
    r"^https?://www\.overstock\.com/([^/]+)/([^/]+)/(\d+)/product\.html$")


class OverstockCrawler(BaseCrawler):
    platform = "overstock"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua(),
                          "Accept": "application/xml,text/xml,*/*"})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # ---- Step 1：拿 sitemap_index ----
        try:
            idx = sess.get(SITEMAP_INDEX, timeout=30)
            self.guard(idx.status_code, "sitemap_index")
            if idx.status_code != 200:
                result.notes.append(
                    f"⚠ sitemap_index 不可达（{idx.status_code}）")
                return result
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ sitemap_index 异常: {exc}")
            return result

        sub_sitemaps = [u for u in _SITEMAP_LOC_RE.findall(idx.text)
                        if "products" in u and u.endswith(".xml")
                        and "taxonomy" not in u]
        result.notes.append(
            f"sitemap_index 命中 {len(sub_sitemaps)} 个子 sitemap")
        self.snapshot("sitemap_index", idx.text)

        # ---- Step 2：依序拉子 sitemap，逐条解析商品 ----
        seen: set[str] = set()
        for sm_url in sub_sitemaps:
            if len(result.products) >= self.limit:
                break
            try:
                sm = sess.get(sm_url, timeout=60)
                self.guard(sm.status_code, f"sub:{sm_url}")
                if sm.status_code != 200:
                    result.notes.append(
                        f"⚠ {sm_url.rsplit('/',1)[-1]} {sm.status_code}")
                    continue
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(
                    f"⚠ {sm_url.rsplit('/',1)[-1]} 异常: {exc}")
                continue

            self.snapshot(sm_url.rsplit("/", 1)[-1], sm.text[:500_000])

            parsed_in_file = 0
            for blk in _URL_BLOCK_RE.finditer(sm.text):
                if len(result.products) >= self.limit:
                    break
                row = self._parse_sitemap_entry(blk.group(1))
                if not row or row["sku"] in seen:
                    continue
                seen.add(row["sku"])
                result.products.append(row)
                parsed_in_file += 1

            result.notes.append(
                f"{sm_url.rsplit('/',1)[-1]}: +{parsed_in_file} SKU "
                f"（累计 {len(result.products)}）")
            # sitemap 是静态 CDN 文件，节流可以很轻
            self.sleep()

        # ---- Step 3（可选）：PDP 兜底丰富（默认关，Akamai 拦截严重）----
        if TRY_PDP_ENRICH and result.products:
            enriched = self._enrich_from_pdp(sess, result.products[:50])
            result.notes.append(f"PDP 兜底尝试 {len(result.products[:50])}, "
                                f"成功 {enriched}")

        result.notes.append(f"采集 {len(result.products)} 个去重 SKU")
        return result

    # ------------------------------------------------------------------
    # 单条 sitemap url 节点 → product dict
    # ------------------------------------------------------------------
    def _parse_sitemap_entry(self, block: str) -> dict | None:
        """从 <url>...</url> 内文解析一个商品 dict。"""
        m_loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block)
        if not m_loc:
            return None
        url = m_loc.group(1)
        mu = _PROD_URL_RE.match(url)
        if not mu:
            return None
        category_seg, slug, sku = mu.group(1), mu.group(2), mu.group(3)
        title = unquote(slug).replace("-", " ").strip()
        if not title:
            return None
        category_path = unquote(category_seg).replace("-", " & ", 1).strip()

        images = _IMG_LOC_RE.findall(block)

        published_at = None
        m_lm = _LASTMOD_RE.search(block)
        if m_lm:
            published_at = self._parse_iso(m_lm.group(1))

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title,
            "description": None,                     # PDP 才有，本路径留空
            "image_urls": images,
            "category_path": category_path,
            "sale_price": None,                      # 需 PDP
            "original_price": None,
            "currency": "USD",
            "status": "on_sale",                     # 出现在 sitemap 即默认在售
            "product_url": url,
            "published_at": published_at,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    @staticmethod
    def _parse_iso(raw: str) -> datetime | None:
        try:
            # 形如 2026-02-04T22:19:31Z
            return datetime.strptime(raw.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            try:
                return datetime.strptime(raw, "%Y-%m-%d")
            except (ValueError, TypeError):
                return None

    # ------------------------------------------------------------------
    # PDP 兜底（默认关）—— 如未来打通 Akamai，价格/评分从 JSON-LD 提
    # ------------------------------------------------------------------
    def _enrich_from_pdp(self, sess: creq.Session,
                         rows: list[dict]) -> int:
        """尝试用 StealthyFetcher 进 PDP 抓 JSON-LD，补价格/评分。"""
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return 0
        ok = 0
        kw = stealth_kwargs(
            proxy=self.proxy,
            country="US",
            persist_profile_key=f"overstock_{self.site.site}",
            timeout_ms=45000,
            real_chrome=False,
            solve_cloudflare=False,    # Akamai 不是 Cloudflare
        )
        for row in rows:
            try:
                page = StealthyFetcher.fetch(row["product_url"], **kw)
                status = getattr(page, "status", None)
                content = getattr(page, "html_content", "") or ""
                if status != 200 or len(content) < 5000:
                    continue
                ld = self._extract_jsonld_product(content)
                if not ld:
                    continue
                offers = ld.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = self._num(offers.get("price"))
                if price is not None:
                    row["sale_price"] = price
                    row["original_price"] = price
                rating = ld.get("aggregateRating") or {}
                row["ratings"] = self._num(rating.get("ratingValue"))
                rc = rating.get("reviewCount") or rating.get("ratingCount")
                if rc is not None:
                    try:
                        row["review_count"] = int(rc)
                    except (TypeError, ValueError):
                        pass
                avail = str(offers.get("availability", "")).lower()
                if "outofstock" in avail or "out of stock" in avail:
                    row["status"] = "out_of_stock"
                desc = ld.get("description")
                if desc and not row.get("description"):
                    row["description"] = desc
                ok += 1
            except Exception:
                continue
            self.sleep()
        return ok

    @staticmethod
    def _extract_jsonld_product(html: str) -> dict | None:
        import json
        for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.S):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                t = c.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    return c
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
