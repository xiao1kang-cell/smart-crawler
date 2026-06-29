"""CDiscount.com 采集器 —— 法国大型综合电商，Baleen + Cloudflare 双层反爬。

实地验证（2026-05-24）：
- ✅ 首页 `https://www.cdiscount.com/` 默认走 **Baleen JS 挑战**（cookie 名
  `visit_baleen_ACM-655d43`）。HTTP 200 但 body 只有 ~14KB 的挑战 stub，含
  `__blnChallengeStore={…}` JSON。curl_cffi(impersonate="chrome") 也吃挑战。
- ✅ Baleen 解法（一次握手）：
    1. 解析 stub 里的 `__blnChallengeStore` 拿到 cookie name/value + checkChallengeParams
    2. 把 cookie 塞进请求头 Cookie 字段
    3. POST `/.well-known/baleen/challengejs/check?<name>=<value>`
       body 用 `bot_category=...&request_fate=...` 之类的 form data
    4. 重 GET 首页 → 返 ~400KB 真页面（含 37 个 f-* 商品 URL + 39 个 l-* 列表 URL）
  整个握手 < 2s，cookie 有效期 900s（maxAge），单 session 期间不必重做。
- ✅ Baleen 一过 → 同 session 直接访问商品页 / 列表页都无障碍：
    · 商品页 `/<cat>/<sub>/<slug>/f-<categoryId>-<sku>.html` → 540KB SSR HTML
    · 列表页 `/<cat>/<sub>/<slug>/l-<id>.html` → 1MB+，含 30~65 个商品 URL
    · 分页：`<list>.html?page=N`（站点用 query string，**不是** `-p-N.html`）
- ✅ 商品页内嵌两块 JSON-LD：
    · `@type=BreadcrumbList` —— 给 category_path
    · `@type=product` （注意是小写）—— 给 sku/name/description/brand/image/
      gtin/offers.price/offers.priceCurrency/offers.availability/aggregateRating
- ⚠️ Cloudflare insights beacon 在所有页面都有挂载（CF 是 CDN 层），但**不触发挑战**
  只要 Baleen 过了。CDN-CGI 挑战 stub `cdn-cgi/challenge` 字串确实在页面里出现，
  但那是 CF 的 jsd/main.js 注入脚本，**不等于** 当前响应是挑战页。
- ⚠️ Baleen cookie 不在 session 时（如 stealth fallback 重起）需要重新握手。

策略（discover → enrich，类 idealo）：
  1. **warmup**：GET 首页解 Baleen，通过 headers Cookie 字段持 cookie
  2. **discover**：BFS 列表页 + 分页扫描，从 home + l-* 页累计去重 f-* 商品 URL，
     直到 ≥ limit*1.2 个种子（留 20% 余量给单 PDP 失败）
  3. **enrich**：对每个商品 URL GET → 解 JSON-LD product → 输出 dict
  4. **fallback**：单 PDP 出现 challenge stub / 5xx 时跳过；连续 5 次失败重做 warmup

CDiscount 反爬等级：**2 级**（Baleen 一次握手；CF 不阻塞）。

字段映射：
  sku             → JSON-LD product.sku  或  URL `-<sku>.html` 末段
  spu             → 同 sku
  title           → product.name
  description     → product.description
  image_urls      → product.image（可能是 string 或 list）
  category_path   → BreadcrumbList itemListElement[*].item.name（去 "Accueil"）
  sale_price      → offers.price
  original_price  → 同 sale_price（详情页没原价/折扣分字段，只有当前价）
  currency        → offers.priceCurrency（默认 EUR）
  status          → offers.availability 含 OutOfStock → out_of_stock
  ratings         → aggregateRating.ratingValue
  review_count    → aggregateRating.ratingCount
  brand           → product.brand.name
  gtin            → product.gtin
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlencode

from curl_cffi import requests as creq

from .. import proxy_pool
from ..antiban import BlockedError
from ..crawl_diagnostics import STAGE_FETCH, classify_exception
from ..fetching import FetchResult
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("CDISCOUNT_LIMIT", "999999"))
if DEFAULT_LIMIT <= 0 or DEFAULT_LIMIT == 1000:
    DEFAULT_LIMIT = 999999
MAX_ELAPSED_SEC = float(os.environ.get("CDISCOUNT_MAX_ELAPSED_SEC", "0"))
# 0 表示不做保护性截断；只在显式调试时设置。
PAGES_PER_CATEGORY = int(os.environ.get("CDISCOUNT_PAGES_PER_CAT", "0"))
MAX_LIST_PAGES = int(os.environ.get("CDISCOUNT_MAX_LIST_PAGES", "0"))
DISCOVERY_TARGET = int(os.environ.get("CDISCOUNT_DISCOVERY_TARGET", "0"))
# 单 PDP 连续失败上限 → 重新握手 Baleen
PDP_FAIL_RESET = int(os.environ.get("CDISCOUNT_PDP_FAIL_RESET", "5"))
PROGRESS_EVERY = int(os.environ.get("CDISCOUNT_PROGRESS_EVERY", "50"))

_HOME = "https://www.cdiscount.com/"
_BALEEN_STORE_RE = re.compile(r"__blnChallengeStore\s*=\s*(\{.*?\});")
_BALEEN_MARK = "blnChallengeStore"            # 出现即说明命中 Baleen 挑战
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
# 商品 URL：/<cat>/<sub>.../<slug>/f-<categoryId>-<sku>.html
_PROD_URL_RE = re.compile(r"(/[\w./\-]+/f-[\w.\-]+\.html)")
# 列表 URL：/<cat>/<sub>.../l-<id>.html （不含 query / fragment）
_LIST_URL_RE = re.compile(r"(/[\w./\-]+/l-[\w.\-]+\.html)")
# 单 SKU 末段，用于从 URL 兜底回填
_SKU_FROM_URL_RE = re.compile(r"/f-\d+-([\w.\-]+)\.html$")


class _PersistentCdiscountFetcher:
    """CDiscount Baleen requires one curl session across challenge + crawl."""

    def __init__(self, crawler: "CdiscountCrawler"):
        self.crawler = crawler
        self.session = creq.Session(impersonate="chrome")
        self.proxy = None
        tier = crawler.site.proxy_tier
        if tier and tier != "none":
            self.proxy = proxy_pool.get_proxy(tier, site=crawler.site.site)
            if self.proxy:
                self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def get(self, url: str, **kwargs) -> FetchResult:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> FetchResult:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> FetchResult:
        timeout = int(kwargs.pop("timeout", 30))
        headers = kwargs.pop("headers", None) or {}
        kwargs.pop("kind", None)
        kwargs.pop("source", None)
        started = time.time()
        try:
            resp = self.session.request(
                method, url, timeout=timeout, headers=headers, **kwargs)
            text = resp.text or ""
            content = resp.content or b""
            ok = 200 <= int(resp.status_code or 0) < 400
            if ok:
                self.crawler.counter.api_calls += 1
            return FetchResult(
                ok=ok,
                url=url,
                status=resp.status_code,
                text=text,
                content=content,
                final_url=getattr(resp, "url", None) or url,
                proxy=self.proxy,
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:
            return FetchResult(
                ok=False,
                url=url,
                proxy=self.proxy,
                duration_ms=int((time.time() - started) * 1000),
                failure=classify_exception(exc, stage=STAGE_FETCH),
            )

    def sync_cookies_to(self, jar: dict[str, str]) -> None:
        for cookie in self.session.cookies.jar:
            jar[cookie.name] = cookie.value


class CdiscountCrawler(BaseCrawler):
    platform = "cdiscount"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        config = site.crawler_config if isinstance(site.crawler_config, dict) else {}
        # CDiscount 无公开全量 sitemap：limit 只控制详情解析量，不参与分母。
        # 生产环境通过站点 crawler_config 控制，避免默认全站无界扫描导致 worker
        # 长时间运行但 UI 无进度。
        self.detail_limit = self._cfg_int(
            config, ("max_products", "detail_limit", "limit"), DEFAULT_LIMIT)
        if self.detail_limit <= 0:
            self.detail_limit = DEFAULT_LIMIT
        self.max_elapsed_sec = self._cfg_float(
            config, ("max_elapsed_sec", "max_runtime_sec"), MAX_ELAPSED_SEC)
        self.pages_per_category = self._cfg_int(
            config, ("pages_per_category", "pages_per_cat"), PAGES_PER_CATEGORY)
        self.max_list_pages = self._cfg_int(
            config, ("max_list_pages",), MAX_LIST_PAGES)
        self.discovery_target = self._cfg_int(
            config, ("discovery_target",), DISCOVERY_TARGET)
        self.progress_every = max(1, self._cfg_int(
            config, ("progress_every",), PROGRESS_EVERY))
        # Baleen cookie jar：key=name, value=value；握手后逐请求透传 Cookie 头
        self._baleen_cookies: dict[str, str] = {}

    @staticmethod
    def _cfg_int(config: dict, keys: tuple[str, ...], default: int) -> int:
        for key in keys:
            value = config.get(key)
            if value not in (None, ""):
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    continue
        return max(0, int(default))

    @staticmethod
    def _cfg_float(config: dict, keys: tuple[str, ...], default: float) -> float:
        for key in keys:
            value = config.get(key)
            if value not in (None, ""):
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
        return max(0.0, float(default))

    # ------------------------------------------------------------------
    # headers
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get/post）。"""
        h = {
            "User-Agent": self.ua(),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/webp,*/*;q=0.8"),
        }
        if self._baleen_cookies:
            h["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self._baleen_cookies.items())
        return h

    # ------------------------------------------------------------------
    # Baleen 一次握手 —— 解 stub、写 cookie、POST check、重 GET 首页
    # ------------------------------------------------------------------
    def _warmup_baleen(self, fetcher, result: CrawlResult,
                       max_attempts: int = 2) -> str | None:
        """成功返回首页 HTML（>50KB 的真页面）；失败返回 None。"""
        for attempt in range(1, max_attempts + 1):
            try:
                res = fetcher.get(_HOME, headers=self._headers(), timeout=30)
                self.guard(res.status or 0, "home")
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(f"⚠ Baleen 握手 #{attempt} 首页异常: {exc}")
                continue

            html = res.text or ""
            if _BALEEN_MARK not in html and len(html) > 50_000:
                # 已经是真首页（极少数情况 CF 缓存直接给了）
                result.notes.append("Baleen 跳过：首页直接 200")
                return html

            m = _BALEEN_STORE_RE.search(html)
            if not m:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} 解 stub 失败 len={len(html)}")
                continue

            try:
                store = json.loads(m.group(1))
                cookie = store["cookie"]
                check_params = store.get("checkChallengeParams") or {}
            except (json.JSONDecodeError, KeyError) as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} stub 解析失败: {exc}")
                continue

            # 记录 Baleen cookie，后续请求通过 headers Cookie 字段透传
            self._baleen_cookies[cookie["name"]] = cookie["value"]

            check_url = (f"https://www.cdiscount.com/.well-known/baleen/"
                         f"challengejs/check?{cookie['name']}={cookie['value']}")
            body = urlencode(check_params)
            try:
                fetcher.post(
                    check_url, data=body,
                    headers={
                        **self._headers(),
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://www.cdiscount.com",
                        "Referer": _HOME,
                    },
                    kind="challenge",
                    source="cdiscount_baleen",
                )
            except Exception as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} check 失败: {exc}")
                continue

            # 重新拉首页 —— 这次应当返回真页面
            try:
                res2 = fetcher.get(_HOME, headers=self._headers(), timeout=30)
                self.guard(res2.status or 0, "home_after_baleen")
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} 重拉首页失败: {exc}")
                continue

            html2 = res2.text or ""
            if _BALEEN_MARK not in html2 and len(html2) > 50_000:
                if hasattr(fetcher, "sync_cookies_to"):
                    fetcher.sync_cookies_to(self._baleen_cookies)
                result.notes.append(
                    f"Baleen 握手成功（#{attempt}），首页 {len(html2)//1024}KB")
                return html2

            result.notes.append(
                f"⚠ Baleen 握手 #{attempt} 重拉首页仍是挑战页 len={len(html2)}")
            self.sleep()

        return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = _PersistentCdiscountFetcher(self)
        started = time.monotonic()

        # Step 1: warmup
        home_html = self._warmup_baleen(fetcher, result)
        if not home_html:
            result.notes.append("⚠ Baleen 终极失败，放弃采集")
            return result
        self.snapshot("home", home_html[:500_000])

        # Step 2: discover —— 从首页种子 + BFS 列表页 抽商品 URL
        seed_products = self._extract_product_urls(home_html)
        seed_lists = self._extract_list_urls(home_html)
        result.notes.append(
            f"首页种子：{len(seed_products)} 商品 / {len(seed_lists)} 列表")

        product_urls = self._discover(fetcher, seed_products, seed_lists, result)
        if not product_urls:
            result.notes.append("⚠ 商品 URL 发现为 0，放弃 PDP 阶段")
            return result
        result.notes.append(
            f"商品 URL 发现池：{len(product_urls)} 个"
            f"（详情解析上限 {self.detail_limit}）")
        target_total = min(len(product_urls), self.detail_limit)
        result.total_product_count = target_total
        self.persist_job_progress(
            products_count=0,
            total_product_count=target_total,
        )

        # Step 3: enrich —— 逐 PDP 解析 JSON-LD
        seen: set[str] = set()
        pdp_fails = 0
        ok = 0
        for idx, url in enumerate(product_urls, 1):
            if self.max_elapsed_sec > 0 and time.monotonic() - started >= self.max_elapsed_sec:
                result.notes.append(
                    f"达到 CDISCOUNT_MAX_ELAPSED_SEC={self.max_elapsed_sec:g}s，"
                    f"提前返回已解析结果（ok={ok}, pdp_fails={pdp_fails}）")
                break
            if len(result.products) >= self.detail_limit:
                break
            try:
                html = self._fetch_pdp(fetcher, url, result)
            except BlockedError:
                raise
            except Exception as exc:
                pdp_fails += 1
                if pdp_fails <= 3 or pdp_fails % 50 == 0:
                    result.notes.append(f"  · PDP 异常 {url[-50:]}: {exc}")
                self.sleep()
                continue
            if not html:
                pdp_fails += 1
                # 连续失败多了 → 怀疑 Baleen cookie 失效，重新握手
                if pdp_fails >= PDP_FAIL_RESET:
                    result.notes.append(
                        f"  · 连续 {pdp_fails} PDP 失败 → 重做 Baleen 握手")
                    if self._warmup_baleen(fetcher, result):
                        pdp_fails = 0
                self.sleep()
                continue
            pdp_fails = 0

            row = self._parse_product(html, url)
            if row:
                self.snapshot(row["sku"], html)
                result.products.append(row)
                ok += 1
                if ok % 100 == 0:
                    result.notes.append(
                        f"  · 进度 {ok} / 详情上限 {self.detail_limit}"
                        f"（已尝试 {idx}）")
                if ok == 1 or ok % self.progress_every == 0:
                    self.persist_job_progress(
                        products_count=ok,
                        total_product_count=target_total,
                    )
            self.sleep()

        self.persist_job_progress(
            products_count=len(result.products),
            total_product_count=target_total,
        )
        result.notes.append(
            f"采集 {len(result.products)} / {target_total} 个目标商品"
            f"（发现池 {len(product_urls)}，PDP 失败累计 {pdp_fails}）")
        if len(result.products) < target_total:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            result.coverage_reason = (
                f"CDiscount 已发现 {len(product_urls)} 个商品 URL，"
                f"本次目标 {target_total} 个详情，"
                f"只完成 {len(result.products)} 个详情解析"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = (
                "放宽 max_elapsed_sec/job_timeout_sec 后继续重跑，"
                "直到本次目标详情全部入库"
            )
        return result

    # ------------------------------------------------------------------
    # 商品 URL 发现：BFS 列表页 + 分页
    # ------------------------------------------------------------------
    def _discover(self, fetcher, seed_products: list[str],
                  seed_lists: list[str], result: CrawlResult) -> list[str]:
        """从列表页 BFS 累计商品 URL，去重保序。"""
        products: list[str] = []
        prod_seen: set[str] = set()
        for u in seed_products:
            if u not in prod_seen:
                prod_seen.add(u)
                products.append(u)

        list_queue: list[str] = list(seed_lists)
        list_seen: set[str] = set()
        # discovery_target 仅用于调试/保护，默认 0 表示把可达列表池扫完。
        target = self.discovery_target
        scanned_lists = 0

        while (
            list_queue
            and (target <= 0 or len(products) < target)
            and (self.max_list_pages <= 0 or scanned_lists < self.max_list_pages)
        ):
            list_path = list_queue.pop(0)
            if list_path in list_seen:
                continue
            list_seen.add(list_path)

            full = list_path if list_path.startswith("http") \
                else self.base + list_path

            # 翻 PAGES_PER_CATEGORY 页
            empty_streak = 0
            page = 1
            while True:
                if target > 0 and len(products) >= target:
                    break
                if self.pages_per_category > 0 and page > self.pages_per_category:
                    break
                url = full if page == 1 else f"{full}?page={page}"
                try:
                    cr = fetcher.get(url, timeout=30,
                                     headers={
                                         **self._headers(),
                                         "Referer": self.base + "/",
                                     })
                    self.guard(cr.status or 0, url)
                except BlockedError:
                    raise
                except Exception as exc:
                    result.notes.append(
                        f"  · 列表异常 {url[-60:]}: {exc}")
                    break
                scanned_lists += 1
                if (cr.status or 0) != 200:
                    break
                if _BALEEN_MARK in cr.text:
                    # 列表页被反爬挡了：重做握手再试一次
                    result.notes.append(
                        f"  · 列表中 Baleen，重试握手 ({url[-50:]})")
                    if not self._warmup_baleen(fetcher, result):
                        break
                    continue

                new_products = 0
                for pu in self._extract_product_urls(cr.text):
                    if pu not in prod_seen:
                        prod_seen.add(pu)
                        products.append(pu)
                        new_products += 1
                # 顺便发现新的列表 URL（深度扩张）
                for lu in self._extract_list_urls(cr.text):
                    if lu not in list_seen and lu not in list_queue:
                        list_queue.append(lu)

                if new_products == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        # 翻页两次没新货 → 该类目用尽，跳出
                        break
                else:
                    empty_streak = 0

                self.sleep()
                page += 1

            if scanned_lists % 20 == 0:
                result.notes.append(
                    f"  · 已扫 {scanned_lists} 列表页 → {len(products)} 商品 URL")
                self.persist_job_progress(
                    products_count=0,
                    total_product_count=len(products),
                )

        result.notes.append(
            f"discover 阶段：扫 {scanned_lists} 列表页 → "
            f"{len(products)} 唯一商品 URL")
        return products

    @staticmethod
    def _extract_product_urls(html: str) -> list[str]:
        """从任意页面抽出 /<...>/f-<id>-<sku>.html 商品 URL（去重保序）。"""
        out: list[str] = []
        seen: set[str] = set()
        for m in _PROD_URL_RE.finditer(html):
            path = m.group(1)
            # 跳掉 //www.cdiscount.com/... 这种相对协议的写法 → 统一成 /path
            if path.startswith("//"):
                idx = path.find("/", 2)
                if idx == -1:
                    continue
                path = path[idx:]
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    @staticmethod
    def _extract_list_urls(html: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for m in _LIST_URL_RE.finditer(html):
            path = m.group(1)
            if path.startswith("//"):
                idx = path.find("/", 2)
                if idx == -1:
                    continue
                path = path[idx:]
            # 同时排除商品 URL（保险）
            if "/f-" in path:
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    # ------------------------------------------------------------------
    # 单 PDP 拉取
    # ------------------------------------------------------------------
    def _fetch_pdp(self, fetcher, url: str,
                   result: CrawlResult) -> str | None:
        full = url if url.startswith("http") else self.base + url
        try:
            res = fetcher.get(full, timeout=30,
                              headers={
                                  **self._headers(),
                                  "Referer": self.base + "/",
                              })
            self.guard(res.status or 0, full)
        except BlockedError:
            raise
        if not res.ok:
            return None
        html = res.text or ""
        if _BALEEN_MARK in html or len(html) < 20_000:
            return None
        return html

    # ------------------------------------------------------------------
    # JSON-LD 解析
    # ------------------------------------------------------------------
    def _parse_product(self, html: str, url: str) -> dict | None:
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
                t = node.get("@type") or ""
                tl = (t.lower() if isinstance(t, str)
                      else ",".join(str(x).lower() for x in t))
                if "product" in tl and product_doc is None:
                    product_doc = node
                elif "breadcrumblist" in tl:
                    breadcrumbs = self._breadcrumb(node)

        if not product_doc:
            return None

        name = product_doc.get("name")
        if not name:
            return None

        sku = product_doc.get("sku")
        if not sku:
            m = _SKU_FROM_URL_RE.search(url)
            sku = m.group(1) if m else None
        if not sku:
            return None
        sku = str(sku)

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
        if not isinstance(rating, dict):
            rating = {}

        imgs = product_doc.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        imgs = [i for i in (imgs or []) if isinstance(i, str)]

        full_url = url if url.startswith("http") else self.base + url

        return {
            "sku": sku,
            "spu": sku,
            "title": str(name).strip(),
            "description": product_doc.get("description"),
            "image_urls": imgs,
            "category_path": "/".join(breadcrumbs[:3]) or None,
            "sale_price": price,
            "original_price": price,
            "currency": currency,
            "status": "out_of_stock" if "outofstock" in avail else "on_sale",
            "ratings": _num(rating.get("ratingValue")),
            "review_count": _int(rating.get("ratingCount")
                                 or rating.get("reviewCount")),
            "gtin": product_doc.get("gtin"),
            "brand": brand or self.site.brand,
            "product_url": full_url,
            "site": self.site.site,
        }

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
            if (nm and isinstance(nm, str)
                    and nm.strip().lower() not in
                    ("home", "accueil", "cdiscount", "")):
                crumbs.append(nm.strip())
        return crumbs


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[-1]
        if len(tail) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
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
