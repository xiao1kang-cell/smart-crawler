"""AliExpress.com 采集器 —— 阿里 nc + slider captcha + X5SEC，5/5 反爬。

数据源：
  - SRP HTML: /w/wholesale-<kw>.html?page=N
  - PDP HTML: /item/<id>.html
  - 内嵌 window.runParams = {data: {...}} 含完整商品 schema

反爬实测（设计阶段评估）：
  - curl_cffi 首页 OK，SRP/PDP 503 + nc challenge
  - nc.x.alicdn.com / _nc_token cookie 是必经
  - 阿里自研 slider captcha，**无 open-source solver**
  - JS-encrypted API params (_signature HMAC)
  - graceful-fail 设计：装好 zenrows / 真住宅 CN/US 代理后零修改全速跑

PDP 字段提取（window.runParams.data 路径）：
  - titleModule.subject / imageModule.imagePathList[]
  - priceModule.formatedPrice / formatedActivityPrice / currencyCode
  - storeModule.brandName / storeModule.storeID
  - specsModule.props[] —— 规格表
  - titleModule.feedbackRating.averageStar / totalValidNum
  - inventoryModule.totalAvailQuantity

策略：先 curl_cffi 尝试 → 失败走 StealthyFetcher（Camoufox patched playwright）
建议代理 tier：residential（CN/HK/US 三选一，US 段最稳）。

反爬等级：5/5（DataDome 之上，阿里自研 nc）。

批C 收编（2026-06）：
  - SRP/PDP curl 段改用 make_fetcher().get()，自动计 api_calls
  - stealth 段用 count_browser_fetch 包裹，成功计 browser_opens
  - 删 proxy 自管(_session → _headers)；保留 guard/_blocked/snapshot/解析/sleep/熔断/翻页
"""
from __future__ import annotations

import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("ALI_LIMIT", "500"))
DELAY = float(os.environ.get("ALI_DELAY", "8.0"))
MAX_PAGES_PER_KW = int(os.environ.get("ALI_MAX_PAGES_PER_KW", "5"))

_HOME_KW = [
    "sofa cover", "dining chair", "cookware set", "bedding sheet",
    "curtain panel", "table lamp", "area rug", "desk organizer",
    "kitchen knife set", "bath mat", "pillow case", "patio chair",
    "wall mirror", "wall clock", "storage box",
]

_ITEM_RE = re.compile(r'/item/(\d{10,16})\.html')
_RUN_RE = re.compile(
    r'window\.runParams\s*=\s*({.+?});\s*(?:</script>|window\.adcUtil)',
    re.S)

_BLOCK_MARKS = (
    "nc.x.alicdn.com",
    "_nc_token",
    "punish",
    "Access Denied",
    "captcha-delivery",
    "slidertest",
    "blocked",
    "behavior verification",
)


class AliExpressCrawler(BaseCrawler):
    platform = "aliexpress"

    def __init__(self, site, limit=None):
        super().__init__(site)
        self.base = "https://www.aliexpress.com"
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.delay = max(self.delay, DELAY)

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 make_fetcher().get()）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base + "/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="srp", source="aliexpress")
        urls: list[str] = []
        seen: set[str] = set()

        # ---------- SRP 阶段：多关键词翻页 ----------
        for kw in _HOME_KW:
            if len(urls) >= self.limit * 2:
                break
            for pg in range(1, MAX_PAGES_PER_KW + 1):
                u = (f"{self.base}/w/wholesale-"
                     f"{kw.replace(' ', '-')}.html?page={pg}")
                body = None
                try:
                    res = fetcher.get(u, headers=self._headers(), timeout=30)
                    if (res.status or 0) == 200 and not self._blocked(res.text):
                        body = res.text
                except Exception:
                    pass
                if not body:
                    body = self._fetch_via_stealth(u)
                if not body:
                    time.sleep(60)
                    fetcher = self.make_fetcher(kind="srp", source="aliexpress")
                    break
                new = 0
                for iid in _ITEM_RE.findall(body):
                    pdp = f"{self.base}/item/{iid}.html"
                    if pdp in seen:
                        continue
                    seen.add(pdp)
                    urls.append(pdp)
                    new += 1
                if new < 5:
                    break
                self.sleep()
            result.notes.append(f"  kw={kw} 累计 {len(urls)} PDP")
            self.sleep()

        if not urls:
            result.notes.append(
                "⚠ AliExpress nc 反爬全程拦截。"
                "解决方案：接 zenrows / scraperapi 或真住宅 CN/HK/US 代理。"
                "本采集器代码已就绪，proxy 就位后零修改运行。")
            return result

        # ---------- PDP 阶段：解析 window.runParams ----------
        pdp_fetcher = self.make_fetcher(kind="product", source="aliexpress")
        ok = denied = 0
        for i, url in enumerate(urls[: self.limit * 2]):
            if ok >= self.limit:
                break
            if i and i % 25 == 0:
                pdp_fetcher = self.make_fetcher(kind="product", source="aliexpress")
            html = None
            try:
                res = pdp_fetcher.get(url, headers=self._headers(), timeout=30)
                if (res.status or 0) == 200 and not self._blocked(res.text):
                    html = res.text
            except Exception:
                pass
            if not html:
                html = self._fetch_via_stealth(url)
            if not html:
                denied += 1
                time.sleep(60)
                continue
            row = self._parse_runparams(html, url)
            if row:
                self.snapshot(row["sku"], html)
                result.products.append(row)
                ok += 1
            self.sleep()

        result.notes.append(f"成功 {ok} · 反爬 {denied}")
        return result

    @staticmethod
    def _blocked(html: str) -> bool:
        if not html or len(html) < 20_000:
            return any(m in (html or "") for m in _BLOCK_MARKS)
        return False

    def _fetch_via_stealth(self, url: str) -> str | None:
        """curl_cffi 触发反爬时走 StealthyFetcher（Camoufox）。

        批C：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，
        成功时自动 browser_opens += 1。stealth kw 参数 / persist_profile /
        profile 目录逻辑全部原样保留，只在最外层套计数。

        success 判断：原标准反面 —— body 足够长(>=20K)且不含 _BLOCK_MARKS。
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
                persist_profile_key=f"ali_{self.site.site}",
                timeout_ms=60000,
            )
            kw["solve_cloudflare"] = False    # nc != Cloudflare

            def _do_fetch():
                return StealthyFetcher.fetch(url, **kw)

            def _success(page) -> bool:
                """成功标准：原 _blocked() 反面 —— body >= 20K 且不含 _BLOCK_MARKS。"""
                if getattr(page, "status", None) != 200:
                    return False
                html = getattr(page, "html_content", None) or getattr(page, "body", None) or ""
                return not self._blocked(html)

            page = self.count_browser_fetch(_do_fetch, success=_success)
            if getattr(page, "status", None) == 200:
                html = page.html_content or page.body or ""
                if not self._blocked(html):
                    return html
        except Exception:
            pass
        return None

    def _parse_runparams(self, html: str, url: str) -> dict | None:
        m = _RUN_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1)).get("data") or {}
        except json.JSONDecodeError:
            return None

        title_mod = data.get("titleModule") or {}
        price_mod = data.get("priceModule") or {}
        image_mod = data.get("imageModule") or {}
        store_mod = data.get("storeModule") or {}
        inv_mod = data.get("inventoryModule") or {}

        m_id = re.search(r'/item/(\d+)\.html', url)
        item_id = m_id.group(1) if m_id else (
            data.get("actionModule", {}).get("productId"))
        if not item_id:
            return None

        img_paths = image_mod.get("imagePathList") or []

        cur_price = (price_mod.get("formatedActivityPrice")
                     or price_mod.get("formatedPrice") or "")
        orig_price = (price_mod.get("formatedPrice") or cur_price)
        currency = price_mod.get("currencyCode") or "USD"

        feedback = title_mod.get("feedbackRating") or {}
        rating = feedback.get("averageStar")
        review_count = title_mod.get("totalValidNum") or feedback.get(
            "totalValidNum")

        total_avail = inv_mod.get("totalAvailQuantity")
        status = ("out_of_stock" if (isinstance(total_avail, int)
                                     and total_avail == 0)
                  else "on_sale")

        return {
            "sku": str(item_id),
            "spu": str(item_id),
            "title": title_mod.get("subject"),
            "description": title_mod.get("subject"),
            "image_urls": img_paths,
            "category_path": None,
            "sale_price": _num(cur_price),
            "original_price": _num(orig_price),
            "currency": currency,
            "ratings": _num(rating),
            "review_count": _int(review_count),
            "inventory": str(total_avail) if total_avail is not None else None,
            "status": status,
            "brand": store_mod.get("brandName") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("US", "").replace("$", "").replace("€", "")
    s = s.replace("£", "").replace(",", "").strip()
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
