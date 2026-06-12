"""Idealo.de 采集器 —— 德国最大价格比较站。

Idealo 不是电商，是聚合站：每个商品页展示「同一商品在 N 个商家的报价范围」。
JSON-LD 里是 `AggregateOffer` 而非 `Offer`，含 lowPrice / highPrice / offerCount。

反爬现状（实测 2026-05-24）：
  - 站点站在 Akamai Bot Manager 后面（页面里挂着 /akam/13/... 像素与挑战脚本）
  - sitemap-index.xml / sitemap.xml / 任何变体 → 404（Idealo 没有公开 sitemap）
  - /preisvergleich/Liste/...     → 503 challenge
  - /preisvergleich/ProductCategory/<id>.html → 200 但返回 Akamai 挑战 stub（2.4KB）
  - /preisvergleich/OffersOfProduct/<id>_-slug.html → 200，干净 SSR HTML，含完整 JSON-LD
  - 首页（/）→ 200，里面散落 60~130 个 OffersOfProduct 商品 URL

采集策略（BFS 发现，绕开被挑战的列表页）：
  1. 拉首页 → 抽出种子商品 URL（一次 60+ 条起步）
  2. 对每个种子页解析 JSON-LD Product/AggregateOffer
  3. 同时从该页正文里扫到 20+ 个相关商品 URL，入队继续 BFS
  4. 直到队列耗尽或抓到 limit 条，curl_cffi(impersonate=chrome) 跑全程
  5. 一旦命中 Akamai 挑战 stub（body < 10KB 且含 sec-if-cpt-container）
     → fallback 到 StealthyFetcher（带 solve_cloudflare 等反爬全套）

Idealo 反爬等级评估：2 级（不算狠，关键是别去碰 Liste/Category）。
"""
from __future__ import annotations

import json
import os
import re

from curl_cffi import requests as creq

from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("IDEALO_LIMIT", "1000"))
SCAN_CAP = int(os.environ.get("IDEALO_SCAN_CAP", "4000"))

_HOME = "https://www.idealo.de/"
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_PROD_URL_RE = re.compile(
    r'/preisvergleich/OffersOfProduct/(\d+)(_-[^"\'?<>\s]+\.html)')
_AKAMAI_MARK = "sec-if-cpt-container"   # Akamai 挑战页标识


class IdealoCrawler(BaseCrawler):
    platform = "idealo"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)
        self.scan_cap = SCAN_CAP

    # ---------- session ----------
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

    # ---------- core ----------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        # 1) 首页种子
        try:
            home_html = self._fetch(sess, _HOME, result, referer=None)
        except Exception as exc:
            result.notes.append(f"⚠ 首页不可达: {exc}")
            return result
        if not home_html:
            result.notes.append("⚠ 首页被 Akamai 挑战且 stealth 兜底失败")
            return result

        seeds = self._extract_product_urls(home_html)
        result.notes.append(f"首页种子 {len(seeds)} 个商品 URL")
        if not seeds:
            result.notes.append("⚠ 首页未抽到任何商品 URL —— 页面结构可能变了")
            return result

        # 2) BFS 抓取
        queue: list[str] = list(seeds)
        seen_ids: set[str] = set()
        scanned = 0
        ok = 0
        stealth_used = 0
        challenge_hits = 0

        while queue and len(result.products) < self.limit \
                and scanned < self.scan_cap:
            url = queue.pop(0)
            pid = self._url_id(url)
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            scanned += 1

            try:
                html = self._fetch(sess, url, result, referer=self.base + "/")
            except Exception as exc:
                if scanned <= 5 or scanned % 100 == 0:
                    result.notes.append(f"  · 抓取异常 {pid}: {exc}")
                self.sleep()
                continue
            if not html:
                challenge_hits += 1
                self.sleep()
                continue
            if _AKAMAI_MARK in html:
                # 挑战页 stub 没被 stealth 解开 —— 跳过
                challenge_hits += 1
                self.sleep()
                continue

            row = self._parse_product(html, url)
            if row:
                self.snapshot(pid, html)
                result.products.append(row)
                ok += 1

            # BFS：把本页发现的新商品 URL 入队
            for new_url in self._extract_product_urls(html):
                nid = self._url_id(new_url)
                if nid and nid not in seen_ids:
                    queue.append(new_url)

            # 节流日志
            if ok and ok % 100 == 0:
                result.notes.append(
                    f"  · 进度 {ok} / 目标 {self.limit}（队列 {len(queue)}）")

            self.sleep()

        result.notes.append(
            f"扫描 {scanned} 页，命中商品 {ok}，"
            f"Akamai 挑战页 {challenge_hits} 次"
            + (f"，stealth 兜底 {stealth_used} 次" if stealth_used else "")
        )
        return result

    # ---------- HTTP 兜底层 ----------
    def _fetch(self, sess: creq.Session, url: str,
               result: CrawlResult, referer: str | None) -> str | None:
        """单次 GET。Akamai 挑战 → 自动走 StealthyFetcher。

        Returns:
            干净 HTML（含 JSON-LD）。None = 即使 stealth 兜底也拿不到。
        """
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = sess.get(url, timeout=30, headers=headers)
        # 熔断点（IP 用量记 / 封禁状态码抛 BlockedError）
        try:
            self.guard(resp.status_code, url)
        except Exception:
            raise

        html = resp.text
        if resp.status_code == 200 and _AKAMAI_MARK not in html and len(html) > 10_000:
            return html

        # —— curl_cffi 命中挑战 / 503 / 短 body：StealthyFetcher 兜底
        stealth_html = self._fetch_via_stealth(url)
        if stealth_html and _AKAMAI_MARK not in stealth_html:
            result.notes.append(
                f"  · stealth 解锁 {url[-60:]} (curl status {resp.status_code})")
            return stealth_html
        return None

    def _fetch_via_stealth(self, url: str) -> str | None:
        """Scrapling StealthyFetcher 兜底 —— Camoufox 真浏览器 + 全套反爬开关。"""
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.site.country,
                persist_profile_key=f"idealo_{self.site.site}",
                timeout_ms=45000,
            )
            page = StealthyFetcher.fetch(url, **kw)
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            pass
        return None

    # ---------- 解析 ----------
    @staticmethod
    def _url_id(url: str) -> str | None:
        m = _PROD_URL_RE.search(url)
        return m.group(1) if m else None

    def _extract_product_urls(self, html: str) -> list[str]:
        """从任意页面（首页 / 商品页）抽出商品 URL，去重保序。"""
        out: list[str] = []
        seen: set[str] = set()
        for m in _PROD_URL_RE.finditer(html):
            pid, tail = m.group(1), m.group(2)
            if pid in seen:
                continue
            seen.add(pid)
            out.append(
                f"{self.base}/preisvergleich/OffersOfProduct/{pid}{tail}")
        return out

    def _parse_product(self, html: str, url: str) -> dict | None:
        """对齐 vonhaus 字段 + Idealo 特有 price_low / price_high / offer_count。"""
        product_doc = None
        breadcrumbs: list[str] = []
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            nodes = doc if isinstance(doc, list) else [doc]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    # 优先取 offers 是 AggregateOffer 的那块
                    if product_doc is None or self._is_aggregate(node):
                        product_doc = node
                elif t == "BreadcrumbList":
                    breadcrumbs = self._breadcrumb(node)

        if not product_doc:
            return None

        pid = self._url_id(url) or product_doc.get("sku")
        name = product_doc.get("name")
        if not pid or not name:
            return None

        brand = product_doc.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        offers = product_doc.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        low = _num(offers.get("lowPrice")) if isinstance(offers, dict) else None
        high = _num(offers.get("highPrice")) if isinstance(offers, dict) else None
        offer_count = _int(offers.get("offerCount")) if isinstance(offers, dict) else None
        # 单价 fallback（极少数 Product 只有单 Offer.price）
        single_price = _num(offers.get("price")) if isinstance(offers, dict) else None
        if low is None:
            low = single_price
        if high is None:
            high = single_price
        currency = (offers.get("priceCurrency") if isinstance(offers, dict)
                    else None) or "EUR"

        avail = ""
        if isinstance(offers, dict):
            avail = str(offers.get("availability", "")).lower()

        rating = product_doc.get("aggregateRating") or {}
        imgs = product_doc.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        imgs = imgs or []

        return {
            "sku": str(pid),
            "spu": str(pid),
            "title": name,
            "description": product_doc.get("description"),
            "image_urls": imgs,
            "category_path": "/".join(breadcrumbs[:3]) or None,
            # 价格范围 —— Idealo 是聚合站，sale_price/original_price 用 low / high
            "sale_price": low,
            "original_price": high if high is not None else low,
            "currency": currency,
            # Idealo 专属字段：多商家价格分布
            "price_low": low,
            "price_high": high,
            "offer_count": offer_count,
            "ratings": _num(rating.get("ratingValue")),
            "review_count": _int(rating.get("ratingCount")
                                 or rating.get("reviewCount")),
            "status": "out_of_stock" if "outofstock" in avail else "on_sale",
            "brand": brand or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    @staticmethod
    def _is_aggregate(node: dict) -> bool:
        offers = node.get("offers")
        if isinstance(offers, dict):
            return offers.get("@type") == "AggregateOffer"
        return False

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
            if nm and nm.lower() not in ("home", "startseite", ""):
                crumbs.append(nm)
        return crumbs


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
