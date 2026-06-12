"""Allegro.pl 采集器 —— 波兰最大电商（200M+ SKU），DataDome 3 级反爬。

Allegro 反爬现状（实测 2026-05-24，本机 + 数据中心代理 + 真 Firefox stealth）：
  - 入口：https://allegro.pl/，商品页模式 `/oferta/<slug>-<id>`
  - 整站站在 DataDome 后面，captcha-delivery.com 拦截
  - curl_cffi（impersonate=chrome）→ 任意 URL 直接 403（含 sitemap.xml）
  - StealthyFetcher（Camoufox / 修补版 Firefox）+ datacenter 代理 → 仍 403
  - 触发标识：响应体含 `geo.captcha-delivery.com` 或 `dd={...rt:'c'...}`
  - sitemap.xml / sitemap-index.xml 全部 403（不像 Aosom/Vidaxl 那种 sitemap 公开）

突破 DataDome 必备条件（任一即可）：
  1. **真住宅代理 PL 出口**（住宅 ASN，不是 Cogent/Hurricane 数据中心段）
  2. DataDome 专用 solver（DataDome 不是 Cloudflare，CF Turnstile solver 无效）
  3. 已被 DataDome 信任的浏览器 profile（带历史 cookie 的同 IP 持续访问）

采集策略（BFS 发现，绕开 sitemap）：
  1. StealthyFetcher 拉首页 → 抽出 `/oferta/...-<id>` 商品 URL（种子）
  2. 同时抽出 `/kategoria/...` 类目 URL（备用种子）
  3. 对种子做 BFS：每页解析 JSON-LD Product + 微数据 itemprop=offers
  4. 用 `dom-i-ogrod`（家居与园艺）等关键词过滤家居类目
  5. 命中 DataDome stub（body < 5KB 且含 `captcha-delivery`）→ 跳过并标记
  6. 直到队列耗尽或抓到 ALLEGRO_LIMIT 条

设计意图：DataDome 拦截在本机环境无解。这个 crawler 保证一旦换上
真住宅 PL 代理后零修改即可全速跑通。当前环境会优雅地报告 0 商品
+ 反爬告警，不会假装成功。

Allegro 反爬等级评估：3 级（DataDome + 自研指纹，比 Idealo 严，
比 Wayfair 仍可解；关键瓶颈是 IP 信誉而非 JS 挑战）。
"""
from __future__ import annotations

import json
import os
import re

from curl_cffi import requests as creq

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("ALLEGRO_LIMIT", "1000"))
SCAN_CAP = int(os.environ.get("ALLEGRO_SCAN_CAP", "3500"))

_HOME = "https://allegro.pl/"
# 商品 URL：/oferta/<polish-slug>-<numeric-id> 或 /oferta/<slug>
_OFERTA_RE = re.compile(r'href="(/oferta/[a-zA-Z0-9\-_%]+(?:\-\d+)?)"')
_CATEG_RE = re.compile(r'href="(/kategoria/[a-zA-Z0-9\-_%]+)"')
_OFERTA_ID_RE = re.compile(r'/oferta/[a-zA-Z0-9\-_%]*?(\d{8,})$')
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

# DataDome 阻拦标识 —— 看到任一即可判定为挑战页 stub
_DATADOME_MARKS = (
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "ct.captcha-delivery.com",
    "Please enable JS and disable any ad blocker",
)

# 家居类目关键词（波兰语）—— 过滤 BFS 时的种子相关度
_HOMEWARE_PL = (
    "dom", "ogrod", "meble", "wnetrz", "kuchnia", "lazienka",  # home/garden/furniture/interior/kitchen/bathroom
    "sypialnia", "salon", "oswietlenie", "dekoracj",            # bedroom/livingroom/lighting/decor
    "narzedzia", "agd", "tekstylia", "porzadki",                # tools/appliances/textiles/cleaning
)


class AllegroCrawler(BaseCrawler):
    platform = "allegro"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/") or "https://allegro.pl"
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        self.scan_cap = SCAN_CAP

    # ---------- session（curl_cffi —— Allegro 几乎必败，仅作探针）----------
    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/webp,*/*;q=0.8"),
            "Referer": "https://www.google.pl/",
        })
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # ---------- core ----------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # 1) 首页种子 —— curl_cffi 99% 失败、stealth 兜底
        home_html = self._fetch(sess, _HOME, result, referer=None)
        if not home_html:
            result.notes.append(
                "⚠ DataDome 阻拦首页：curl_cffi + StealthyFetcher 均被 403。"
                "本机出口/代理 IP 已被 DataDome 列入数据中心黑名单。"
                "解决方案：换真住宅 PL 代理（Bright Data / Oxylabs / IPRoyal "
                "Polish residential pool）或接入 DataDome solver。")
            return result

        seeds = self._extract_oferta(home_html)
        cat_seeds = self._extract_kategoria(home_html)
        result.notes.append(
            f"首页种子：{len(seeds)} 个商品 URL + {len(cat_seeds)} 个类目 URL")

        # 2) 家居类目预扩展 —— 先扫几个家居类目页拉更多种子
        for cat in cat_seeds[:8]:
            if any(kw in cat.lower() for kw in _HOMEWARE_PL):
                cat_html = self._fetch(sess, self.base + cat, result,
                                       referer=_HOME)
                if cat_html:
                    new_offers = self._extract_oferta(cat_html)
                    seeds.extend(new_offers)
                    result.notes.append(
                        f"  · 家居类目 {cat[:60]} → +{len(new_offers)} 种子")
                self.sleep()

        # 去重保序
        seen_seeds: set[str] = set()
        ordered_seeds: list[str] = []
        for s in seeds:
            if s not in seen_seeds:
                seen_seeds.add(s)
                ordered_seeds.append(s)
        seeds = ordered_seeds
        if not seeds:
            result.notes.append("⚠ 种子为 0 —— 首页解出但无 /oferta/ 链接，"
                                "可能页面结构变了，或 DataDome 返回了精简 stub")
            return result

        # 3) BFS 抓商品
        queue: list[str] = list(seeds)
        seen_ids: set[str] = set()
        scanned = 0
        ok = 0
        blocked_hits = 0

        while queue and len(result.products) < self.limit \
                and scanned < self.scan_cap:
            path = queue.pop(0)
            url = self.base + path if path.startswith("/") else path
            pid = self._url_id(url)
            key = pid or url
            if key in seen_ids:
                continue
            seen_ids.add(key)
            scanned += 1

            html = self._fetch(sess, url, result, referer=_HOME)
            if not html:
                blocked_hits += 1
                self.sleep()
                continue
            if self._is_blocked(html):
                blocked_hits += 1
                self.sleep()
                continue

            row = self._parse_product(html, url)
            if row:
                self.snapshot(pid or url.rstrip("/").split("/")[-1], html)
                result.products.append(row)
                ok += 1

            # 4) 把本页发现的新商品入队（BFS 扩散）
            for new_path in self._extract_oferta(html):
                nid = self._url_id(self.base + new_path)
                nkey = nid or new_path
                if nkey not in seen_ids:
                    queue.append(new_path)

            if ok and ok % 100 == 0:
                result.notes.append(
                    f"  · 进度 {ok}/{self.limit}（队列 {len(queue)}，"
                    f"DataDome 拦截 {blocked_hits}）")

            self.sleep()

        result.notes.append(
            f"扫描 {scanned} 页，命中商品 {ok}，DataDome 拦截 {blocked_hits} 次"
        )
        return result

    # ---------- HTTP 兜底层 ----------
    def _fetch(self, sess: creq.Session, url: str,
               result: CrawlResult, referer: str | None) -> str | None:
        """单次 GET。DataDome 拦截 → 自动 StealthyFetcher 兜底。

        Returns:
            干净 HTML（不含 DataDome stub）；None = 兜底也失败。
        """
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            resp = sess.get(url, timeout=30, headers=headers)
        except Exception:
            # 网络层失败也走 stealth 兜底
            return self._fetch_via_stealth(url)
        # 熔断（429 / 401）—— 403 是 DataDome 常态，不熔断、走 stealth
        try:
            if resp.status_code in (401, 429):
                self.guard(resp.status_code, url)
        except Exception:
            raise

        html = resp.text
        if (resp.status_code == 200
                and not self._is_blocked(html)
                and len(html) > 10_000):
            return html

        # —— curl_cffi 被 DataDome 拦 → StealthyFetcher 兜底
        stealth_html = self._fetch_via_stealth(url)
        if stealth_html and not self._is_blocked(stealth_html):
            result.notes.append(
                f"  · stealth 解锁 {url[-70:]} (curl status {resp.status_code})")
            return stealth_html
        return None

    def _fetch_via_stealth(self, url: str) -> str | None:
        """Scrapling StealthyFetcher 兜底 —— Camoufox + 反爬全套。

        关键差异：DataDome 不是 Cloudflare，关闭 solve_cloudflare 避免
        Camoufox 卡在找不到 CF challenge 的报错；DataDome 自家挑战靠
        浏览器指纹 + cookie 持久化（per-site user_data_dir）解决。
        """
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.site.country or "PL",
                persist_profile_key=f"allegro_{self.site.site}",
                timeout_ms=60000,
            )
            kw["solve_cloudflare"] = False     # DataDome != Cloudflare
            page = StealthyFetcher.fetch(url, **kw)
            if getattr(page, "status", None) == 200:
                html = page.html_content or page.body or ""
                if not self._is_blocked(html):
                    return html
        except Exception:
            pass
        return None

    @staticmethod
    def _is_blocked(html: str) -> bool:
        if not html:
            return True
        if len(html) < 5_000:
            return any(m in html for m in _DATADOME_MARKS)
        return False

    # ---------- URL 抽取 ----------
    @staticmethod
    def _extract_oferta(html: str) -> list[str]:
        """从 HTML 抽 /oferta/... 商品 URL（去重保序）。"""
        out, seen = [], set()
        for m in _OFERTA_RE.finditer(html):
            path = m.group(1)
            # 过滤明显非商品（/oferta/ 后是子路径如 /list-of）
            if path.count("/") > 2:
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    @staticmethod
    def _extract_kategoria(html: str) -> list[str]:
        out, seen = [], set()
        for m in _CATEG_RE.finditer(html):
            path = m.group(1)
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    @staticmethod
    def _url_id(url: str) -> str | None:
        m = _OFERTA_ID_RE.search(url.split("?")[0])
        return m.group(1) if m else None

    # ---------- 解析 ----------
    def _parse_product(self, html: str, url: str) -> dict | None:
        """JSON-LD 优先 → 微数据 fallback。对齐 vonhaus/idealo 输出字段。"""
        row = self._parse_jsonld(html, url)
        if row:
            return row
        return self._parse_microdata(html, url)

    def _parse_jsonld(self, html: str, url: str) -> dict | None:
        product_doc = None
        breadcrumbs: list[str] = []
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            graph = (doc if isinstance(doc, list)
                     else doc.get("@graph", [doc]) if isinstance(doc, dict)
                     else [])
            for node in graph:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    product_doc = product_doc or node
                elif t == "BreadcrumbList":
                    breadcrumbs = self._breadcrumb_jsonld(node)

        if not product_doc:
            return None

        return self._build_row(product_doc, url, breadcrumbs)

    def _parse_microdata(self, html: str, url: str) -> dict | None:
        """JSON-LD 缺失时退到 microdata（itemtype="...Product"）。"""
        # 极简微数据抽取 —— 只在 JSON-LD 完全缺位时兜底
        if 'itemtype="https://schema.org/Product"' not in html \
                and 'itemtype="http://schema.org/Product"' not in html:
            return None
        title = _first_meta(html, "og:title") or _first_meta(html, "twitter:title")
        if not title:
            return None
        price = _first_attr(
            html,
            r'<meta[^>]+itemprop="price"[^>]+content="([\d.,]+)"')
        currency = _first_attr(
            html,
            r'<meta[^>]+itemprop="priceCurrency"[^>]+content="([A-Z]{3})"'
        ) or "PLN"
        avail = _first_attr(
            html,
            r'<link[^>]+itemprop="availability"[^>]+href="([^"]+)"') or ""
        image = _first_meta(html, "og:image")
        pid = self._url_id(url) or url.rstrip("/").split("/")[-1]
        return {
            "sku": str(pid),
            "spu": str(pid),
            "title": title,
            "description": _first_meta(html, "og:description"),
            "image_urls": [image] if image else [],
            "category_path": None,
            "sale_price": _num(price),
            "original_price": _num(price),
            "currency": currency,
            "status": "out_of_stock" if "outofstock" in avail.lower()
            else "on_sale",
            "brand": self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    def _build_row(self, doc: dict, url: str,
                   breadcrumbs: list[str]) -> dict:
        pid = self._url_id(url) or doc.get("sku") or doc.get("productID")
        offers = doc.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _num(offers.get("price")) if isinstance(offers, dict) else None
        low = _num(offers.get("lowPrice")) if isinstance(offers, dict) else None
        high = _num(offers.get("highPrice")) if isinstance(offers, dict) else None
        if price is None:
            price = low
        if low is None:
            low = price
        if high is None:
            high = price
        currency = (offers.get("priceCurrency") if isinstance(offers, dict)
                    else None) or "PLN"
        avail = ""
        if isinstance(offers, dict):
            avail = str(offers.get("availability", "")).lower()

        brand = doc.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        rating = doc.get("aggregateRating") or {}
        imgs = doc.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        imgs = imgs or []

        return {
            "sku": str(pid) if pid else url.rstrip("/").split("/")[-1],
            "spu": str(pid) if pid else None,
            "title": doc.get("name"),
            "description": doc.get("description"),
            "image_urls": imgs,
            "category_path": "/".join(breadcrumbs[:3]) or None,
            "sale_price": price,
            "original_price": high if high is not None else price,
            "currency": currency,
            "gtin": doc.get("gtin13") or doc.get("gtin"),
            "mpn": doc.get("mpn"),
            "ratings": _num(rating.get("ratingValue")),
            "review_count": _int(rating.get("ratingCount")
                                 or rating.get("reviewCount")),
            "status": "out_of_stock" if "outofstock" in avail else "on_sale",
            "brand": brand or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    @staticmethod
    def _breadcrumb_jsonld(node: dict) -> list[str]:
        items = node.get("itemListElement") or []
        crumbs: list[str] = []
        for el in items:
            if not isinstance(el, dict):
                continue
            it = el.get("item")
            name = it.get("name") if isinstance(it, dict) else el.get("name")
            # 跳过波兰语 / 英语 home
            if name and name.lower() not in (
                    "home", "strona główna", "allegro", ""):
                crumbs.append(name)
        return crumbs


# ---------- 工具 ----------
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "")
    # 波兰语千分位常用 "1 234,56" or "1.234,56"
    s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 \
        and s.count(".") <= 1 and s.rfind(",") > s.rfind(".") else s
    m = re.search(r"[\d.]+", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _first_meta(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"',
        html)
    return m.group(1) if m else None


def _first_attr(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html)
    return m.group(1) if m else None
