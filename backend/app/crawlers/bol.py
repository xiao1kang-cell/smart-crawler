"""Bol.com 采集器 —— NL/BE 最大电商，Akamai Bot Manager 防护。

实地验证（2026-05-24）：
- ✅ robots.txt 公开列出三个 sitemap 入口（curl_cffi impersonate=chrome 直接 200）：
    https://www.bol.com/sitemap/nl-nl/   （荷兰 NL 站，主入口）
    https://www.bol.com/sitemap/nl-be/   （比利时荷语）
    https://www.bol.com/sitemap/fr-be/   （比利时法语）
  每个 root 是 sitemap_index，下挂 1000+ 个 product-{N} 子 sitemap，
  每个子 sitemap ~50,000 个商品 URL（合计千万级 SKU）。
- ✅ 子 sitemap 是标准 sitemaps.org 0.9 格式，只有 <loc> + <lastmod>，
  **没有 image 扩展、没有任何额外元数据**。比 Overstock 的 Image Sitemap 更瘠。
- ❌ 商品详情页（/<locale>/p/<slug>/<numericId>/）被 Akamai Bot Manager 拦截：
  curl_cffi 返回 ~2.2 KB 的 sec-if-cpt 挑战页（HTTP 200 但 body 是 JS 挑战），
  Scrapling StealthyFetcher（含 Camoufox + persist profile + Google referer）
  实测也吃同样 2.2 KB 挑战 —— Akamai 对 PDP 比 sitemap 严格得多。
- ❌ 官方 API `api.bol.com/catalog/v4/products/{id}` 返回 403 Unauthorized，
  需 Bol Partner / Affiliate 凭据（合作伙伴接口，非公开）。
  `/api/products`、`/pdp-async/` 等内部端点全部 404 或被同一挑战拦下。
- ✅ sitemap root（不带 .xml 后缀）直接返回 XML，content-type 是 application/xml。

策略：**sitemap-first，与 Overstock 同款**。可拿字段：
    sku / spu       → URL 末段纯数字（Bol 内部商品 ID，13 位）
    title           → slug 解码（'4x-zijden-kussensloop-maanlicht' → '4x zijden kussensloop maanlicht'）
    product_url     → <loc>
    published_at    → <lastmod>
    currency        → EUR（NL/BE 均欧元区）
    site / brand    → 站点配置
价格 / 图片 / 评分 / 库存 / 描述：需 PDP，本路径无法拿到，留空。

如未来拿到 Bol Affiliate API token 或住宅代理 + 长 cookie warmup 方案，
可在 _enrich_from_pdp() 激活 JSON-LD 兜底（offers.price / aggregateRating）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import unquote

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("BOL_LIMIT", "1000"))
TRY_PDP_ENRICH = os.environ.get("BOL_TRY_PDP", "0") == "1"

# 每个站点对应的 sitemap_index 入口（来自 robots.txt 实测）
_SITEMAP_ROOT = {
    "NL": "https://www.bol.com/sitemap/nl-nl/",
    "BE": "https://www.bol.com/sitemap/nl-be/",   # 默认走荷语版（Bol 主语种）
    "BE_FR": "https://www.bol.com/sitemap/fr-be/",
}

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>")
# Bol 商品 URL：/<locale>/p/<slug>/<numericId>/  例如 /nl/nl/p/the-unwedding/9300000180270213/
_PROD_URL_RE = re.compile(
    r"^https?://www\.bol\.com/(nl/nl|nl/be|fr/be)/p/([^/]+)/(\d+)/?$")
# 子 sitemap URL 形如 https://www.bol.com/sitemap/nl-nl/product-1
_SUB_SITEMAP_RE = re.compile(r"/sitemap/[a-z]{2}-[a-z]{2}/product-\d+$")

_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class BolCrawler(BaseCrawler):
    platform = "bol"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        # 按站点 country 选 sitemap root：NL → nl-nl，BE → nl-be
        cc = (site.country or "NL").upper()
        self.sitemap_root = _SITEMAP_ROOT.get(cc, _SITEMAP_ROOT["NL"])

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept": "application/xml,text/xml,*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        })
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
            idx = sess.get(self.sitemap_root, timeout=30)
            self.guard(idx.status_code, "sitemap_index")
            if idx.status_code != 200:
                result.notes.append(
                    f"⚠ sitemap_index 不可达（{idx.status_code}）")
                # 被 Akamai 挑战时 fallback 到 StealthyFetcher 抓 sitemap
                if idx.status_code in (401, 403, 451):
                    txt = self._fetch_via_stealth(self.sitemap_root)
                    if not txt:
                        return result
                    idx_text = txt
                    result.notes.append(
                        "✅ curl_cffi 被拦 → StealthyFetcher 解锁 sitemap")
                else:
                    return result
            else:
                idx_text = idx.text
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ sitemap_index 异常: {exc}")
            return result

        sub_sitemaps = [u for u in _SITEMAP_LOC_RE.findall(idx_text)
                        if _SUB_SITEMAP_RE.search(u)]
        result.notes.append(
            f"sitemap_index 命中 {len(sub_sitemaps)} 个 product 子 sitemap"
            f"（{self.sitemap_root}）")
        self.snapshot("sitemap_index", idx_text)

        if not sub_sitemaps:
            result.notes.append("⚠ 无 product 子 sitemap，疑似站点结构变更")
            return result

        # ---- Step 2：依序拉子 sitemap，逐条解析商品 ----
        seen: set[str] = set()
        for i, sm_url in enumerate(sub_sitemaps, 1):
            if len(result.products) >= self.limit:
                break
            try:
                sm = sess.get(sm_url, timeout=60)
                self.guard(sm.status_code, f"sub:{sm_url}")
                if sm.status_code != 200:
                    result.notes.append(
                        f"⚠ {sm_url.rsplit('/',1)[-1]} {sm.status_code}")
                    continue
                sm_text = sm.text
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(
                    f"⚠ {sm_url.rsplit('/',1)[-1]} 异常: {exc}")
                continue

            # 子 sitemap 体积可达 9 MB，snapshot 截断到 500 KB 够追源
            self.snapshot(sm_url.rsplit("/", 1)[-1], sm_text[:500_000])

            parsed_in_file = 0
            for blk in _URL_BLOCK_RE.finditer(sm_text):
                if len(result.products) >= self.limit:
                    break
                row = self._parse_sitemap_entry(blk.group(1))
                if not row or row["sku"] in seen:
                    continue
                seen.add(row["sku"])
                result.products.append(row)
                parsed_in_file += 1

            result.notes.append(
                f"product-{i}: +{parsed_in_file} SKU "
                f"（累计 {len(result.products)}）")
            # sitemap 是 CDN 静态文件，节流可以轻一些
            self.sleep()

        # ---- Step 3（可选）：PDP 兜底丰富（默认关，Akamai 拦截严重）----
        if TRY_PDP_ENRICH and result.products:
            n_try = min(50, len(result.products))
            enriched = self._enrich_from_pdp(result.products[:n_try])
            result.notes.append(
                f"PDP 兜底尝试 {n_try}, 成功 {enriched}")

        result.notes.append(f"采集 {len(result.products)} 个去重 SKU")
        return result

    # ------------------------------------------------------------------
    # 单条 sitemap url 节点 → product dict
    # ------------------------------------------------------------------
    def _parse_sitemap_entry(self, block: str) -> dict | None:
        m_loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block)
        if not m_loc:
            return None
        url = m_loc.group(1)
        mu = _PROD_URL_RE.match(url)
        if not mu:
            return None
        locale_seg, slug, sku = mu.group(1), mu.group(2), mu.group(3)
        title = unquote(slug).replace("-", " ").strip()
        if not title:
            return None

        published_at = None
        m_lm = _LASTMOD_RE.search(block)
        if m_lm:
            published_at = self._parse_iso(m_lm.group(1))

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title,
            "description": None,             # PDP 才有，本路径留空
            "image_urls": [],                # sitemap 无 image 扩展
            "category_path": None,           # PDP 才有
            "sale_price": None,
            "original_price": None,
            "currency": "EUR",
            "status": "on_sale",             # 出现在 sitemap 即默认在售
            "product_url": url,
            "published_at": published_at,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    @staticmethod
    def _parse_iso(raw: str) -> datetime | None:
        """Bol 的 lastmod 含纳秒和时区偏移，先截到秒级再解析。"""
        try:
            # 形如 2026-05-24T03:15:54.636741549+02:00 → 截到 2026-05-24T03:15:54
            head = re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw)
            if head:
                return datetime.strptime(head.group(), "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            pass
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # StealthyFetcher 兜底 —— curl_cffi 拿 sitemap 被拦时用
    # ------------------------------------------------------------------
    def _fetch_via_stealth(self, url: str) -> str | None:
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country="NL",
                persist_profile_key=f"bol_{self.site.site}",
                timeout_ms=45000,
                real_chrome=False,
                solve_cloudflare=False,    # Akamai，不是 Cloudflare Turnstile
            )
            page = StealthyFetcher.fetch(url, **kw)
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # PDP 兜底（默认关）—— Akamai 长期挑战，留接口给未来代理 + warmup
    # ------------------------------------------------------------------
    def _enrich_from_pdp(self, rows: list[dict]) -> int:
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return 0
        ok = 0
        kw = stealth_kwargs(
            proxy=self.proxy,
            country="NL",
            persist_profile_key=f"bol_{self.site.site}",
            timeout_ms=45000,
            real_chrome=False,
            solve_cloudflare=False,
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
                row["currency"] = (offers.get("priceCurrency")
                                   or row.get("currency") or "EUR")
                rating = ld.get("aggregateRating") or {}
                if rating:
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
                imgs = ld.get("image")
                if isinstance(imgs, str):
                    imgs = [imgs]
                if imgs and not row.get("image_urls"):
                    row["image_urls"] = imgs
                ok += 1
            except Exception:
                continue
            self.sleep()
        return ok

    @staticmethod
    def _extract_jsonld_product(html: str) -> dict | None:
        import json
        for m in _LD_RE.finditer(html):
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
        m = re.search(r"[\d.]+", str(v).replace(",", "."))
        try:
            return float(m.group()) if m else None
        except ValueError:
            return None
