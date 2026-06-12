"""Otto.de 采集器 —— 德国 Top 2 电商，Kasada (KPSDK) 防护。

实地验证（2026-05-24）：
- ❌ Sitemap 全军覆没：/sitemap.xml、/sitemap_index.xml、/sitemaps/sitemap.xml 全 404。
  robots.txt 里也未声明 sitemap。Otto 不公开 sitemap。
- ✅ 首页 https://www.otto.de/ 与一级 / 二级类目页 curl_cffi(impersonate=chrome) 直连可达：
  • 首页 → 47-130 个 /p/ 商品链接（视类目栏渲染深度）
  • /mode/bekleidung/、/moebel/、/sale/deal-des-tages/ 等列表页 → 每页 100+ 商品 URL
  • 类目 sweep（20 个一二级类目）→ 1300+ 去重商品 URL 不重不漏
- ❌ 商品页（/p/...）被 Kasada (KPSDK) JS 挑战拦截：curl_cffi 直连 429
  返回 853 字节 stub，含 `<script>window.KPSDK={};...` 与 `/149e9513-01fa-.../ips.js`
  （robots.txt 主动 Disallow 的同一路径）。Referer warmup / 节流 / impersonate 均无效。
- ✅ StealthyFetcher（Camoufox + persistent profile，**solve_cloudflare=False**）
  完美通过 Kasada：单 PDP 8-35s（首次解挑战慢，复用 profile 后稳定 ~8s），
  HTML 完整含 2 个 JSON-LD（Product + BreadcrumbList）。
- ⚠ **profile warmup 是硬性前置**：实测裸的全新 profile 直冲 PDP → Kasada 全 429；
  先 StealthyFetcher.fetch('/') 让 Kasada 在 profile 里写好 cookies/storage，
  之后同 profile 抓 PDP 才稳定 200。一旦 profile 被警告/封禁，整套就废了。

Otto 反爬等级评估：**3 级**（Kasada 比 Cloudflare Turnstile / Akamai 更狠 ——
专门拦自动化，但只锁 PDP，列表页放行；persistent profile + 先 warm 首页是关键）。

策略（discovery 廉价 + PDP 贵）：
  1. curl_cffi 扫一组首页 + 类目页（~20 个），harvest 1000+ 商品 URL；
  2. 对每条 URL 走 StealthyFetcher 解 Kasada → 抓干净 HTML
  3. 解析 JSON-LD Product：sku / name / brand / gtin13 / image / offers.price /
     aggregateRating + 同页 BreadcrumbList → category_path
  4. 单 PDP ~8s（含挑战 + network_idle），1000 SKU ≈ 2-3 h；
     OTTO_LIMIT 可调低做 smoke test。

字段对齐 vonhaus._parse_product 返回的 dict。
"""
from __future__ import annotations

import json
import os
import re
import time

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("OTTO_LIMIT", "1000"))
SCAN_CAP = int(os.environ.get("OTTO_SCAN_CAP", "4000"))

# 类目种子 —— 实测 2026-05-24 全部 200 + 每页 ≥ 100 商品 URL（除 /garten /sport /technik
# 这种 SPA-redirect 顶层页只有 12 条，二级页 100+）。前缀 = 'https://www.otto.de'。
_SEED_PATHS = (
    "/",
    "/mode/bekleidung/", "/mode/hosen/", "/mode/kleider/", "/mode/hemden/",
    "/mode/roecke/", "/mode/bodies/", "/mode/westen/",
    "/moebel/", "/moebel/sofas-couches/", "/moebel/betten/", "/moebel/tische/",
    "/garten/", "/baumarkt/",
    "/technik/multimedia/",
    "/sport/",
    "/damen/mode/", "/herren/mode/",
    "/sale/deal-des-tages/", "/sale/deals-der-woche/",
)

_PROD_HREF_RE = re.compile(r"href=[\"'](/p/[^\"'?#]+)")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
# Kasada 挑战页特征（curl_cffi 命中时返回的小 stub）
_KASADA_MARKS = ("KPSDK", "/ips.js?KP_UIDz")
# 商品 URL 末尾的 SKU pattern：
#   /p/.../<SKU>/  其中 SKU 可能是纯数字 / S\w+ / C\d+
_URL_SKU_RE = re.compile(r"/p/.*?/([A-Z0-9]{6,})/?$")


class OttoCrawler(BaseCrawler):
    platform = "otto"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        self.scan_cap = SCAN_CAP

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/webp,*/*;q=0.8"),
        })
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # ------------------------------------------------------------------
    # main
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # ---- Phase 1: discovery（curl_cffi 扫类目）----
        urls = self._discover_urls(sess, result)
        if not urls:
            result.notes.append("⚠ discovery 0 商品 URL —— 类目页结构可能变了")
            return result
        result.notes.append(
            f"discovery 命中 {len(urls)} 个去重商品 URL（种子 {len(_SEED_PATHS)} 类目）")

        # ---- Phase 2: PDP via StealthyFetcher ----
        targets = urls[: self.scan_cap]
        ok = 0
        challenge_hits = 0
        seen_skus: set[str] = set()
        stealth_handle = self._build_stealth_kw()

        # Kasada 关键：profile 首次走 PDP 必 429，先 warm 首页让它写 cookie/storage
        warmed = self._warm_profile(stealth_handle)
        result.notes.append("profile warmup " + ("OK" if warmed else "失败 —— PDP 阶段大概率全 429"))

        idx = 0
        for idx, url in enumerate(targets, 1):
            if len(result.products) >= self.limit:
                break
            try:
                html = self._fetch_pdp(url, stealth_handle)
            except BlockedError:
                raise
            except Exception as exc:
                if idx <= 5 or idx % 100 == 0:
                    result.notes.append(f"  · PDP exc {url[-50:]}: {exc}")
                self.sleep()
                continue
            if not html:
                challenge_hits += 1
                self.sleep()
                continue

            row = self._parse_product(html, url)
            if not row or row["sku"] in seen_skus:
                self.sleep()
                continue
            seen_skus.add(row["sku"])
            self.snapshot(row["sku"], html)
            result.products.append(row)
            ok += 1

            if ok and ok % 50 == 0:
                result.notes.append(
                    f"  · 进度 {ok} / 目标 {self.limit}（扫描 {idx}）")
            self.sleep()

        result.notes.append(
            f"采集 {ok} 个去重 SKU，扫 PDP {idx}, Kasada 挑战 {challenge_hits} 次")
        return result

    # ------------------------------------------------------------------
    # Phase 1：discovery —— curl_cffi 扫种子类目页拿 /p/ 链接
    # ------------------------------------------------------------------
    def _discover_urls(self, sess: creq.Session,
                       result: CrawlResult) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for path in _SEED_PATHS:
            seed_url = self.base + path
            try:
                resp = sess.get(seed_url, timeout=30,
                                headers={"Referer": self.base + "/"})
                self.guard(resp.status_code, f"seed:{path}")
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(f"  · seed {path} exc: {exc}")
                continue
            if resp.status_code != 200:
                result.notes.append(f"  · seed {path} {resp.status_code}")
                continue

            new = 0
            for href in _PROD_HREF_RE.findall(resp.text):
                url = self.base + href
                # 规整：去掉尾随多余 query，统一带末尾 /
                url = url.split("#", 1)[0].rstrip("/") + "/"
                if url in seen:
                    continue
                seen.add(url)
                ordered.append(url)
                new += 1
            # 单条 seed 调试日志（控制噪音：仅前几条 + 每 5 条一次）
            if len(ordered) <= 500 or path.endswith(("woche/", "tages/")):
                result.notes.append(
                    f"  · seed {path}: +{new}（累计 {len(ordered)}）")
            self.sleep()
        return ordered

    # ------------------------------------------------------------------
    # Phase 2：PDP via Stealth（Kasada 必走）
    # ------------------------------------------------------------------
    def _build_stealth_kw(self):
        """复用同一份 stealth_kwargs（persistent profile 让 Kasada 当老客户）。"""
        from ._stealth_config import stealth_kwargs
        return stealth_kwargs(
            proxy=self.proxy,
            country=self.site.country or "DE",
            persist_profile_key=f"otto_{self.site.site}",
            timeout_ms=60000,
            solve_cloudflare=False,    # Kasada 不是 Cloudflare
            real_chrome=False,
        )

    def _warm_profile(self, kw: dict) -> bool:
        """对新 / 沉睡 profile 先 fetch 首页，让 Kasada 写 cookies；
        否则后续 PDP 全 429。"""
        try:
            from scrapling.fetchers import StealthyFetcher
        except Exception:
            return False
        try:
            page = StealthyFetcher.fetch(self.base + "/", **kw)
        except Exception:
            return False
        return getattr(page, "status", None) == 200 \
            and len(getattr(page, "html_content", "") or "") > 50_000

    def _fetch_pdp(self, url: str, kw: dict) -> str | None:
        """StealthyFetcher 抓单 PDP；成功 → 返回 HTML，失败/挑战 → None。"""
        try:
            from scrapling.fetchers import StealthyFetcher
        except Exception:
            return None
        try:
            page = StealthyFetcher.fetch(url, **kw)
        except Exception:
            return None
        status = getattr(page, "status", None)
        if status not in (200, None):
            return None
        html = getattr(page, "html_content", "") or ""
        if len(html) < 50_000:
            # 挑战 stub 通常 <1KB，正常 PDP 800KB+
            return None
        return html

    # ------------------------------------------------------------------
    # JSON-LD 解析
    # ------------------------------------------------------------------
    def _parse_product(self, html: str, url: str) -> dict | None:
        product_doc, breadcrumbs = None, []
        for blk in _LD_RE.findall(html):
            try:
                data = json.loads(blk.strip())
            except json.JSONDecodeError:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    if product_doc is None:
                        product_doc = node
                elif t == "BreadcrumbList":
                    breadcrumbs = self._breadcrumb(node)

        if not product_doc:
            return None

        name = product_doc.get("name")
        sku = product_doc.get("sku") or self._url_sku(url)
        if not name or not sku:
            return None

        brand = product_doc.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        offers = product_doc.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _num(offers.get("price")) if isinstance(offers, dict) else None
        currency = (offers.get("priceCurrency") if isinstance(offers, dict)
                    else None) or "EUR"
        avail = ""
        if isinstance(offers, dict):
            avail = str(offers.get("availability", "")).lower()

        rating = product_doc.get("aggregateRating") or {}
        imgs = product_doc.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        imgs = [i for i in (imgs or []) if isinstance(i, str)]

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": name,
            "description": product_doc.get("description"),
            "image_urls": imgs,
            "category_path": "/".join(breadcrumbs[:3]) or None,
            "sale_price": price,
            "original_price": price,
            "currency": currency,
            "ratings": _num(rating.get("ratingValue")),
            "review_count": _int(rating.get("reviewCount")
                                 or rating.get("ratingCount")),
            "status": "out_of_stock" if "outofstock" in avail else "on_sale",
            "brand": brand or self.site.brand,
            "gtin": product_doc.get("gtin13") or product_doc.get("gtin"),
            "product_url": url,
            "site": self.site.site,
        }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _breadcrumb(node: dict) -> list[str]:
        items = node.get("itemListElement") or []
        crumbs: list[str] = []
        for el in items:
            if not isinstance(el, dict):
                continue
            it = el.get("item")
            if isinstance(it, dict):
                nm = it.get("name")
            else:
                nm = el.get("name")
            if nm and nm.lower() not in ("startseite", "home", ""):
                crumbs.append(nm)
        return crumbs

    @staticmethod
    def _url_sku(url: str) -> str | None:
        m = _URL_SKU_RE.search(url.rstrip("/"))
        return m.group(1) if m else None


# ----------------------------------------------------------------------
# 数字解析（与 idealo._num/_int 对齐）
# ----------------------------------------------------------------------
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d.]+", str(v).replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
