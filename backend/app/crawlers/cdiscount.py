"""CDiscount.com 采集器 —— 法国大型综合电商，Baleen + Cloudflare 双层反爬。

实地验证（2026-05-24）：
- ✅ 首页 `https://www.cdiscount.com/` 默认走 **Baleen JS 挑战**（cookie 名
  `visit_baleen_ACM-655d43`）。HTTP 200 但 body 只有 ~14KB 的挑战 stub，含
  `__blnChallengeStore={…}` JSON。curl_cffi(impersonate="chrome") 也吃挑战。
- ✅ Baleen 解法（一次握手）：
    1. 解析 stub 里的 `__blnChallengeStore` 拿到 cookie name/value + checkChallengeParams
    2. 把 cookie 塞 session
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
  1. **warmup**：GET 首页解 Baleen，session 持 cookie
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

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("CDISCOUNT_LIMIT", "1000"))
# 单类目最多翻几页（防止某些大类几百页打爆）
PAGES_PER_CATEGORY = int(os.environ.get("CDISCOUNT_PAGES_PER_CAT", "5"))
# 最多探索多少个列表页（防止 BFS 漫游失控）
MAX_LIST_PAGES = int(os.environ.get("CDISCOUNT_MAX_LIST_PAGES", "120"))
# 单 PDP 连续失败上限 → 重新握手 Baleen
PDP_FAIL_RESET = int(os.environ.get("CDISCOUNT_PDP_FAIL_RESET", "5"))

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


class CdiscountCrawler(BaseCrawler):
    platform = "cdiscount"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.limit = self._resolve_limit(DEFAULT_LIMIT)

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({
            "User-Agent": self.ua(),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/webp,*/*;q=0.8"),
        })
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # ------------------------------------------------------------------
    # Baleen 一次握手 —— 解 stub、写 cookie、POST check、重 GET 首页
    # ------------------------------------------------------------------
    def _warmup_baleen(self, sess: creq.Session, result: CrawlResult,
                       max_attempts: int = 2) -> str | None:
        """成功返回首页 HTML（>50KB 的真页面）；失败返回 None。"""
        for attempt in range(1, max_attempts + 1):
            try:
                r = sess.get(_HOME, timeout=30)
                self.guard(r.status_code, "home")
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(f"⚠ Baleen 握手 #{attempt} 首页异常: {exc}")
                continue

            html = r.text or ""
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

            # 写 cookie（Baleen 期望该 cookie 在请求头里）
            try:
                sess.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=".cdiscount.com", path="/")
            except Exception as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} 写 cookie 失败: {exc}")
                continue

            check_url = (f"https://www.cdiscount.com/.well-known/baleen/"
                         f"challengejs/check?{cookie['name']}={cookie['value']}")
            body = "&".join(f"{k}={v}" for k, v in check_params.items())
            try:
                sess.post(
                    check_url, data=body, timeout=30,
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"})
            except Exception as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} check 失败: {exc}")
                continue

            # 重新拉首页 —— 这次应当返回真页面
            try:
                r2 = sess.get(_HOME, timeout=30)
                self.guard(r2.status_code, "home_after_baleen")
            except BlockedError:
                raise
            except Exception as exc:
                result.notes.append(
                    f"⚠ Baleen 握手 #{attempt} 重拉首页失败: {exc}")
                continue

            html2 = r2.text or ""
            if _BALEEN_MARK not in html2 and len(html2) > 50_000:
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
        sess = self._session()

        # Step 1: warmup
        home_html = self._warmup_baleen(sess, result)
        if not home_html:
            result.notes.append("⚠ Baleen 终极失败，放弃采集")
            return result
        self.snapshot("home", home_html[:500_000])

        # Step 2: discover —— 从首页种子 + BFS 列表页 抽商品 URL
        seed_products = self._extract_product_urls(home_html)
        seed_lists = self._extract_list_urls(home_html)
        result.notes.append(
            f"首页种子：{len(seed_products)} 商品 / {len(seed_lists)} 列表")

        product_urls = self._discover(sess, seed_products, seed_lists, result)
        if not product_urls:
            result.notes.append("⚠ 商品 URL 发现为 0，放弃 PDP 阶段")
            return result
        result.notes.append(
            f"商品 URL 池：{len(product_urls)} 个（目标 {self.limit}）")

        # Step 3: enrich —— 逐 PDP 解析 JSON-LD
        seen: set[str] = set()
        pdp_fails = 0
        ok = 0
        for idx, url in enumerate(product_urls, 1):
            if len(result.products) >= self.limit:
                break
            try:
                html = self._fetch_pdp(sess, url, result)
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
                    if self._warmup_baleen(sess, result):
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
                        f"  · 进度 {ok} / 目标 {self.limit}（已尝试 {idx}）")
            self.sleep()

        result.notes.append(
            f"采集 {len(result.products)} 商品（PDP 失败累计 {pdp_fails}）")
        return result

    # ------------------------------------------------------------------
    # 商品 URL 发现：BFS 列表页 + 分页
    # ------------------------------------------------------------------
    def _discover(self, sess: creq.Session, seed_products: list[str],
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
        # 目标 URL 数 = limit * 1.3（留 30% 余量给 PDP 失败/重复 SKU）
        target = int(self.limit * 1.3)
        scanned_lists = 0

        while (list_queue and len(products) < target
               and scanned_lists < MAX_LIST_PAGES):
            list_path = list_queue.pop(0)
            if list_path in list_seen:
                continue
            list_seen.add(list_path)

            full = list_path if list_path.startswith("http") \
                else self.base + list_path

            # 翻 PAGES_PER_CATEGORY 页
            empty_streak = 0
            for page in range(1, PAGES_PER_CATEGORY + 1):
                if len(products) >= target:
                    break
                url = full if page == 1 else f"{full}?page={page}"
                try:
                    cr = sess.get(url, timeout=30,
                                  headers={"Referer": self.base + "/"})
                    self.guard(cr.status_code, url)
                except BlockedError:
                    raise
                except Exception as exc:
                    result.notes.append(
                        f"  · 列表异常 {url[-60:]}: {exc}")
                    break
                scanned_lists += 1
                if cr.status_code != 200:
                    break
                if _BALEEN_MARK in cr.text:
                    # 列表页被反爬挡了：重做握手再试一次
                    result.notes.append(
                        f"  · 列表中 Baleen，重试握手 ({url[-50:]})")
                    if not self._warmup_baleen(sess, result):
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

            if scanned_lists % 20 == 0:
                result.notes.append(
                    f"  · 已扫 {scanned_lists} 列表页 → {len(products)} 商品 URL")

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
    def _fetch_pdp(self, sess: creq.Session, url: str,
                   result: CrawlResult) -> str | None:
        full = url if url.startswith("http") else self.base + url
        try:
            r = sess.get(full, timeout=30,
                         headers={"Referer": self.base + "/"})
            self.guard(r.status_code, full)
        except BlockedError:
            raise
        if r.status_code != 200:
            return None
        html = r.text or ""
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
