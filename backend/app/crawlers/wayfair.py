"""Wayfair 采集器 —— 北美家居电商，PerimeterX / Akamai 双层防护。

入口：robots.txt 暴露了 6 个 sitemap，PDP 子图为 `seo-pdp-index.xml`，
下挂数十个 `seo-pdp-sitemap~N.xml`（每个 ~370 商品 URL）。

商品页结构：
  · 无 Product JSON-LD（只有 WebSite 和 BreadcrumbList 两块）
  · 没有公开的 __APP_INIT__ 全量 JSON（被打散进 webpack chunks）
  · 价格、标题、SKU 全在 SSR HTML 里，靠 data-test-id 锚定：
      - <input name="sku" value="W003077221"/>
      - <span data-test-id="StandardPricingPrice-PRIMARY">$399.99</span>
      - <s data-test-id="PriceDisplay" ...>$478.79</s>（原价，带 <s> 划线）
      - <p>Rated 4.4 out of 5 stars.</p>
      - <span>361 Reviews</span>
  · BreadcrumbList JSON-LD 拿分类路径
  · og:image / og:title / og:description 拿描述

策略：
  1. 顺序读 seo-pdp-index.xml → 列出 50+ 个子 sitemap
  2. 顺序读子 sitemap，累积 product URL（默认 limit 1000）
  3. 用 curl_cffi(impersonate=chrome) 请求每个 PDP，正则 + selectolax 解析
  4. 命中 401/403/451/429 即 fallback 到 StealthyFetcher（Camoufox）

实测（2026-05-24，本机直连，无代理）：curl_cffi 路径 200 OK，
~0.7-0.9s/页，1000 SKU 约 15-25 分钟（含拟人 sleep）。
"""
from __future__ import annotations

import json
import os
import re

from curl_cffi import requests as creq
from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("WAYFAIR_LIMIT", "1000"))
SITEMAP_INDEX = "https://www.wayfair.com/seo-pdp-index.xml"
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
_SKU_INPUT_RE = re.compile(
    r'<input[^>]+name="sku"[^>]+value="([^"]+)"', re.I)
_SKU_URL_RE = re.compile(r"-([a-z0-9]+)\.html$", re.I)
# 销售价 / 原价（划线）
_SALE_PRICE_RE = re.compile(
    r'StandardPricingPrice-PRIMARY[^$]*?\$([\d,]+\.\d+)', re.S)
_ORIG_PRICE_RE = re.compile(
    r'StandardPricingPrice-PREVIOUS[^$]*?<s[^>]*>\$([\d,]+\.\d+)</s>', re.S)
# 评分文案 'Rated 4.4 out of 5 stars.'
_RATING_RE = re.compile(r"Rated\s+([\d.]+)\s+out of 5 stars", re.I)
# '361 Reviews'
_REVIEWS_RE = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s+Reviews?", re.I)


class WayfairCrawler(BaseCrawler):
    platform = "wayfair"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = limit if limit is not None else DEFAULT_LIMIT

    # ---------- session ----------
    def _session(self, warmup: bool = False) -> creq.Session:
        """构建 curl_cffi session。

        Wayfair 实测：不带 Sec-Fetch-* 和 Referer 的"裸"请求会被 PerimeterX
        识别为 bot → 第二次请求即触发 429。补全浏览器头 + 首次访问首页拿到
        _px3 cookie 后，可稳定连发 25+ 次（实测 24/25 OK，单次 404）。
        """
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.wayfair.com/",
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
                s.get(self.base + "/", timeout=30)   # 取 _px3 / _pxvid cookie
            except Exception:
                pass
        return s

    # ---------- main ----------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session(warmup=True)

        urls = self._collect_pdp_urls(sess, result)
        if not urls:
            result.notes.append("⚠ 未能收集到任何 PDP URL —— 中止")
            return result

        targets = urls[: self.limit]
        result.notes.append(
            f"sitemap 累计 {len(urls)} PDP URL，本次抓取 {len(targets)}")

        import time as _t

        ok = missing = fail = blocked = stealth_used = 0
        consecutive_block = 0
        # 反爬节奏（实测 2026-05-24，本机直连无代理）：
        #   curl_cffi 可稳定连发 ~30-40 个 PDP → 触发 429 → 等待 ~60-90s 后恢复
        #   StealthyFetcher (Camoufox) 每页 ~60s，对 1000 SKU 不可行 → 不开
        #   策略：被封时 sleep 90s + 重建 session，最多 5 个 block 周期
        BLOCK_BREAK = 6               # 第 6 个 block 周期仍失败 → 熔断
        STEALTH_USE = (os.environ.get("WAYFAIR_USE_STEALTH", "0") == "1")
        STEALTH_BUDGET = 5            # 默认不开 stealth；显式打开后预算 5 次
        SESSION_ROTATE = 100          # 每 100 次请求主动 rotate session
        BLOCK_COOLDOWN_S = 90         # 单次封锁后的 IP 冷却

        for i, url in enumerate(targets):
            # 周期性 session rotation —— 防长尾被打入观察名单
            if i > 0 and i % SESSION_ROTATE == 0:
                sess = self._session(warmup=True)
                result.notes.append(
                    f"… 第 {i} 条，主动 rotate session（已抓 {ok}）")

            try:
                html, code = self._fetch_pdp(sess, url)

                # 404 = 商品已下架，不是反爬 → 静默跳过
                if code == 404:
                    missing += 1
                    consecutive_block = 0
                    self.sleep()
                    continue

                is_block = (code in (401, 403, 429, 451)
                            or (html is not None and self._is_blocked_body(html)))
                if is_block:
                    blocked += 1
                    consecutive_block += 1
                    if blocked <= 3 or consecutive_block in (1, 5):
                        result.notes.append(
                            f"⚠ {code or 'body-block'} (连击 {consecutive_block}) "
                            f"@ ok={ok}/{i} {url[-50:]}")

                    # 第 1 次封锁 → 长睡眠（让 PerimeterX 衰减）+ 重建 session
                    if consecutive_block == 1:
                        result.notes.append(
                            f"  → sleep {BLOCK_COOLDOWN_S}s + 重建 session")
                        _t.sleep(BLOCK_COOLDOWN_S)
                        sess = self._session(warmup=True)
                        # 该 URL 不重试，继续向后扫描（容忍小漏）
                        fail += 1
                        continue
                    # 第 2 次（仍然 block）→ 更长睡眠
                    if consecutive_block == 2:
                        result.notes.append(
                            f"  → 连续 block，sleep {BLOCK_COOLDOWN_S*2}s")
                        _t.sleep(BLOCK_COOLDOWN_S * 2)
                        sess = self._session(warmup=True)
                        fail += 1
                        continue
                    # 第 3+ 次 → 走 stealth（如果允许）否则熔断
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
                                    f"wayfair 连续 {consecutive_block} 次封锁"
                                    f"（含 stealth 失败），熔断")
                            self.sleep()
                            continue
                    else:
                        fail += 1
                        if consecutive_block >= BLOCK_BREAK:
                            raise BlockedError(
                                f"wayfair 连续 {consecutive_block} 次封锁，熔断"
                                f"（已抓 {ok}）")
                        # 还有 budget，继续长睡
                        _t.sleep(BLOCK_COOLDOWN_S * consecutive_block)
                        sess = self._session(warmup=True)
                        continue
                elif code == 200:
                    consecutive_block = 0

                if not html:
                    fail += 1
                    self.sleep()
                    continue

                row = self._parse_product(html, url)
                if row:
                    self.snapshot(row["sku"], html)
                    result.products.append(row)
                    ok += 1
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

    # ---------- sitemap ----------
    def _collect_pdp_urls(self, sess: creq.Session, result: CrawlResult) -> list[str]:
        """读取 PDP sitemap_index → 子 sitemap → product URL（增量直到 limit）。"""
        try:
            idx = sess.get(SITEMAP_INDEX, timeout=30)
            self.guard(idx.status_code, "seo-pdp-index")
            if idx.status_code != 200:
                result.notes.append(
                    f"⚠ seo-pdp-index 返回 {idx.status_code}")
                return []
        except BlockedError:
            raise
        except Exception as exc:
            result.notes.append(f"⚠ seo-pdp-index 不可达: {exc}")
            return []

        subs = _LOC_RE.findall(idx.text)
        result.notes.append(f"PDP sitemap_index: {len(subs)} 个子 sitemap")

        urls: list[str] = []
        seen: set[str] = set()
        for sm in subs:
            if len(urls) >= self.limit:
                break
            try:
                r = sess.get(sm, timeout=40)
                if r.status_code != 200:
                    continue
                for u in _LOC_RE.findall(r.text):
                    if "/pdp/" not in u or u in seen:
                        continue
                    seen.add(u)
                    urls.append(u)
                    if len(urls) >= self.limit:
                        break
            except Exception:
                continue
            self.sleep()
        return urls

    # ---------- fetch ----------
    def _fetch_pdp(self, sess: creq.Session, url: str) -> tuple[str | None, int]:
        """单页抓取。返回 (html_or_None, status_code)。

        不在这里直接 guard() —— Wayfair 偶发 429 是常态，连续 429 才视为封禁。
        由上层调用方累计 block 次数后再决定熔断 / stealth fallback。"""
        try:
            r = sess.get(url, timeout=30)
        except Exception:
            return None, 0
        if r.status_code == 200:
            return r.text, 200
        return None, r.status_code

    def _fetch_via_stealth(self, url: str) -> str | None:
        """curl_cffi 被反爬时（403/451 或返回 challenge）走 StealthyFetcher。"""
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.site.country or "US",
                persist_profile_key=f"wayfair_{self.site.site}",
                timeout_ms=60000,
            )
            page = StealthyFetcher.fetch(url, **kw)
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            return None
        return None

    @staticmethod
    def _is_blocked_body(html: str) -> bool:
        """识别 PerimeterX / Akamai challenge 页面（典型标识：'Pardon Our' /
        'Access Denied' / px-captcha 主体很短）。注意 wayfair 正常页 ~1.2MB，
        challenge 页通常 < 50KB。"""
        if not html:
            return True
        if len(html) < 50_000:
            return True
        markers = ("Pardon Our Interruption", "Access Denied",
                   "px-captcha", "/_Incapsula_Resource",
                   "ROBOTS NOINDEX")
        return any(m in html for m in markers)

    # ---------- parse ----------
    def _parse_product(self, html: str, url: str) -> dict | None:
        tree = HTMLParser(html)

        # SKU：优先 <input name="sku">；退化为 URL 末段
        sku = None
        m = _SKU_INPUT_RE.search(html)
        if m:
            sku = m.group(1).strip()
        if not sku:
            m = _SKU_URL_RE.search(url)
            sku = m.group(1).upper() if m else None
        if not sku:
            return None

        # 标题：H1
        title = None
        h1 = tree.css_first("h1")
        if h1:
            title = h1.text(strip=True)
        if not title:
            title = self._meta(tree, "og:title")
            if title:
                title = re.split(r"\s+\|\s+", title)[0].strip()
        if not title:
            return None

        # 价格
        sale_price = self._first_price(_SALE_PRICE_RE.search(html))
        original_price = self._first_price(_ORIG_PRICE_RE.search(html))
        if original_price is None:
            original_price = sale_price

        # 评分 / 评论数
        rating = None
        m = _RATING_RE.search(html)
        if m:
            try:
                rating = float(m.group(1))
            except ValueError:
                rating = None
        review_count = None
        m = _REVIEWS_RE.search(html)
        if m:
            try:
                review_count = int(m.group(1).replace(",", ""))
            except ValueError:
                review_count = None

        # 分类路径：BreadcrumbList JSON-LD
        category_path = self._breadcrumb(html)

        # 图片：og:image + sitemap 已经给过，但 PDP 也有完整图组
        og_img = self._meta(tree, "og:image")
        images: list[str] = []
        # 商品页里 img src 命中 assets.wfcdn.com
        for n in tree.css("img"):
            src = (n.attributes.get("src")
                   or n.attributes.get("data-src")
                   or n.attributes.get("data-srcset"))
            if not src:
                continue
            if "assets.wfcdn.com" in src and src not in images:
                # 取首张主图 URL；srcset 可能是 'a 1x, b 2x'，取第一段
                clean = src.split(" ")[0].split(",")[0].strip()
                if clean and clean not in images:
                    images.append(clean)
            if len(images) >= 10:
                break
        if og_img and og_img not in images:
            images.insert(0, og_img)

        description = self._meta(tree, "og:description")

        # 库存
        body_lower = html.lower()
        out_of_stock = (
            ("out of stock" in body_lower)
            or ("sold out" in body_lower and "in stock" not in body_lower)
        )
        status = "out_of_stock" if out_of_stock else "on_sale"

        return {
            "sku": str(sku),
            "spu": str(sku),
            "title": title,
            "description": description,
            "image_urls": images[:10] or ([og_img] if og_img else []),
            "category_path": category_path,
            "sale_price": sale_price,
            "original_price": original_price,
            "currency": "USD",
            "ratings": rating,
            "review_count": review_count,
            "status": status,
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
        }

    # ---------- helpers ----------
    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    @staticmethod
    def _first_price(match) -> float | None:
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _breadcrumb(html: str) -> str | None:
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("@type") != "BreadcrumbList":
                continue
            names: list[str] = []
            for item in doc.get("itemListElement", []):
                if not isinstance(item, dict):
                    continue
                inner = item.get("item")
                if isinstance(inner, dict):
                    n = inner.get("name")
                elif isinstance(inner, str):
                    n = item.get("name") or inner
                else:
                    n = item.get("name")
                if n and n.lower() not in ("home", ""):
                    names.append(n)
            return "/".join(names[:4]) or None
        return None
