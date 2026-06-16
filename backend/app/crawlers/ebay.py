"""eBay.com 采集器 —— 全球最大 C2C/B2C 平台，Akamai Edge 防护。

实测（2026-05-24 本机直连 99.x.x.x，无代理）：

URL 发现 —— 两条路：
- ✅ SRP（搜索结果页）：`/sch/i.html?_nkw=<kw>&_sacat=11700&_pgn=N&_ipg=240`
  · `_sacat=11700` 锁定 Home & Garden 主类目（子类 3197 Furniture / 20444 Yard,
    Garden & Outdoor / 67712 Kitchen, Dining & Bar / 20710 Bath / 20439 Bedding /
    20625 Home Improvement 也可单独打）
  · `_ipg=240` 每页 240 条，比默认 60 高 4 倍
  · `_pgn` 翻页，深翻到 ~40 页（约 ~9600 条/关键词）后 eBay 会自动截断
  · 用多关键词 + 多子类目并集去重，可凑足 1000+ 家居 SKU
  · 单纯只挂 _sacat（无 _nkw）走的是 BrowseNode 渲染路径，只回 8 个商品，
    必须带 _nkw —— 这是 SRP 路径
- ✅ Sitemap 兜底：`/lst/VIS-0-index.xml` 列出 1132 个 `GTC-0_N.xml.gz` 子文件
  （Good 'Til Cancelled listings 即在售商品），每个 gzip 后 100K-1M 条 itm URL，
  但**无类目过滤** —— 整个 eBay 全品类混在一起，家居占比低，性价比差。
  仅作 SRP 不够时的兜底，默认不开（EBAY_USE_SITEMAP=1 启用）。

PDP 解析：
- ❌ `<title>` / OG meta 是用 `Property=` `Content=`（大写、无引号）写的，
  标准 lowercase regex 抓不到；用 case-insensitive 匹配
- ✅ `<script type=application/ld+json>` 内含完整 Product JSON-LD（注意 type
  属性**无双引号**，标准 `type="application/ld+json"` regex 会漏；用宽松匹配）
  Product schema 字段齐：name / mpn / brand / image[] / offers.price /
  offers.priceCurrency / offers.availability / offers.priceSpecification（原价）
- ✅ 另一个 LD 块是 BreadcrumbList → 4 层类目路径（含具体子类目名）

反爬实测：
- ⚠ Akamai Edge：单 IP 连发 ~5-10 个 PDP 后整段 30+ 分钟硬封禁，所有
  /itm/* 和 /sch/* 都返回 HTTP 200 + 13KB Access Denied 页（含
  `errors.edgesuite.net` 标识）。首页 / 不受影响。
- ⚠ 触发后不分 fingerprint —— 换 impersonate=chrome / chrome131 /
  safari / firefox 全部一样吃 403。需要换 IP（proxy_tier=residential）。
- ✅ 200 但 len < 50KB 即视为 challenge，节流 + 重建 session

策略：
1. 收集阶段：SRP 多关键词 × 多子类目，去重收 product URL
2. PDP 阶段：curl_cffi + 5-8s 抖动间隔，每 50 条 rotate session，
   连续 3 次 Access Denied 触发 90s sleep + 重建 session，
   累计 BLOCK_BREAK 次后熔断（让上层调度换代理重启）
3. 字段：JSON-LD Product / BreadcrumbList 双块拼装

字段对齐 vonhaus.py：sku/spu/title/description/image_urls/category_path/
sale_price/original_price/currency/status/product_url/site/brand
+ ratings/review_count（eBay LD 有时含 aggregateRating）。
"""
from __future__ import annotations

import json
import os
import re
import time

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

DEFAULT_LIMIT = int(os.environ.get("EBAY_LIMIT", "1000"))
USE_SITEMAP = os.environ.get("EBAY_USE_SITEMAP", "0") == "1"
DELAY = float(os.environ.get("EBAY_DELAY", "5.0"))

# Home & Garden + 6 大子类目，覆盖完整家居谱
# 11700 = Home & Garden（顶层）
# 3197  = Furniture
# 20444 = Yard, Garden & Outdoor Living
# 67712 = Kitchen, Dining & Bar
# 20710 = Bath
# 20439 = Bedding
# 20625 = Home Improvement
_HOME_CATEGORIES = [
    ("furniture", "3197"),
    ("sofa", "3197"),
    ("chair", "3197"),
    ("table", "3197"),
    ("bed frame", "3197"),
    ("dresser", "3197"),
    ("desk", "3197"),
    ("bookshelf", "3197"),
    ("rug", "11700"),
    ("lamp", "11700"),
    ("curtain", "11700"),
    ("cushion", "11700"),
    ("mirror", "11700"),
    ("vase", "11700"),
    ("clock", "11700"),
    ("cookware", "67712"),
    ("dinnerware", "67712"),
    ("knife set", "67712"),
    ("blender", "67712"),
    ("bath towel", "20710"),
    ("shower curtain", "20710"),
    ("bedding set", "20439"),
    ("pillow", "20439"),
    ("comforter", "20439"),
    ("planter", "20444"),
    ("garden hose", "20444"),
    ("patio furniture", "20444"),
    ("tool set", "20625"),
    ("led light bulb", "20625"),
    ("door knob", "20625"),
]
MAX_PAGES_PER_KW = int(os.environ.get("EBAY_MAX_PAGES_PER_KW", "8"))  # 8 页 × 240 = 1920 ID

# 宽松匹配：eBay 的 type 属性写 `type=application/ld+json`（无引号）
_LD_RE = re.compile(
    r'<script[^>]*type=[\'"]?application/ld\+json[\'"]?[^>]*>(.*?)</script>',
    re.S | re.I)
_ITM_RE = re.compile(r'https?://www\.ebay\.com/itm/(\d{8,16})')
_LOC_RE = re.compile(r'<loc>\s*(.*?)\s*</loc>')
_OG_RE = re.compile(
    r'<meta\s+(?:property|Property)=["\']?(og:[a-z]+)["\']?\s+'
    r'(?:content|Content)=["\']([^"\']*)["\']', re.I)
# 13KB 反爬挑战页特征（Akamai Access Denied / PerimeterX Pardon Our Interruption）
_BLOCK_MARKERS = ("errors.edgesuite.net", "Access Denied",
                  "Pardon Our Interruption", "px-captcha",
                  "_Incapsula_Resource", "captcha-delivery")


class EbayCrawler(BaseCrawler):
    platform = "ebay"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.base = "https://www.ebay.com"
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        # 实测 1.5s 间隔在第 5-10 个 PDP 就触发 Akamai → 强制 ≥5s
        self.delay = max(self.delay, DELAY)

    # ---------------- headers ----------------
    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。

        ebay Akamai 4/5 反爬对指纹敏感 —— 透传 impersonate=chrome131 保留。
        """
        return {
            "User-Agent": self.ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": "https://www.ebay.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _warmup(self, fetcher) -> None:
        """向首页发一次 warmup GET（计入 api_calls）。"""
        try:
            fetcher.get(self.base + "/",
                        headers=self._headers(),
                        impersonate="chrome131",
                        timeout=20)
        except Exception:
            pass

    # ---------------- main ----------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="ebay")
        self._warmup(fetcher)

        # ---- Step 1: 收集 PDP URL ----
        urls = self._collect_urls_from_srp(fetcher, result)
        if len(urls) < self.limit and USE_SITEMAP:
            urls += self._collect_urls_from_sitemap(fetcher, result,
                                                    need=self.limit - len(urls))
        urls = urls[: self.limit * 2]   # 留点 buffer，扣掉 404 / block
        result.notes.append(
            f"URL 池就绪：{len(urls)} 个 PDP URL（目标 {self.limit}）")

        if not urls:
            result.notes.append("⚠ 未收集到任何 PDP URL，中止")
            return result

        # ---- Step 2: 抓 PDP，解析 JSON-LD ----
        ok = blocked = denied = parse_fail = 0
        consecutive_block = 0
        BLOCK_BREAK = 8           # 连续 8 次 Access Denied → 熔断
        SESSION_ROTATE = 30       # 每 30 个 PDP 主动换 warmup
        BLOCK_COOLDOWN_S = 90

        for i, url in enumerate(urls):
            if ok >= self.limit:
                break
            if i > 0 and i % SESSION_ROTATE == 0:
                self._warmup(fetcher)
                result.notes.append(
                    f"  rotate warmup @ {i} (ok={ok}, denied={denied})")

            try:
                res = fetcher.get(url, timeout=30,
                                  impersonate="chrome131",
                                  headers={**self._headers(),
                                           "Referer": "https://www.ebay.com/"})
                code = res.status or 0
                html = res.text or ""
            except BlockedError:
                raise
            except Exception as exc:
                parse_fail += 1
                if parse_fail <= 5:
                    result.notes.append(f"跳过 {url[-40:]}: {exc}")
                self.sleep()
                continue

            # Akamai 用 200 + 小页面返回 challenge —— 必须按 body 判
            is_block = (code in (401, 403, 429, 451)
                        or self._is_blocked_body(html))
            if is_block:
                denied += 1
                consecutive_block += 1
                if denied <= 3 or consecutive_block in (1, 5):
                    result.notes.append(
                        f"⚠ block code={code} len={len(html)} "
                        f"(连击 {consecutive_block}) @ ok={ok}/{i}")
                # 第 1 次封锁 → 长睡眠 + warmup
                if consecutive_block == 1:
                    time.sleep(BLOCK_COOLDOWN_S)
                    self._warmup(fetcher)
                    blocked += 1
                    continue
                # 第 2 次（仍封锁）→ 更长睡眠
                if consecutive_block == 2:
                    time.sleep(BLOCK_COOLDOWN_S * 2)
                    self._warmup(fetcher)
                    blocked += 1
                    continue
                # 第 3+ 次 → 继续退避，达到熔断阈值则抛
                if consecutive_block >= BLOCK_BREAK:
                    raise BlockedError(
                        f"ebay 连续 {consecutive_block} 次 Access Denied，"
                        f"熔断（已抓 {ok}）")
                time.sleep(BLOCK_COOLDOWN_S * consecutive_block)
                self._warmup(fetcher)
                blocked += 1
                continue
            else:
                consecutive_block = 0

            row = self._parse_product(html, url)
            if row:
                self.snapshot(row["sku"], html)
                result.products.append(row)
                ok += 1
                if ok and ok % 50 == 0:
                    result.notes.append(
                        f"  进度 ok={ok} denied={denied} parse_fail={parse_fail}")
            else:
                parse_fail += 1
            self.sleep()

        result.notes.append(
            f"成功 {ok} · 反爬命中 {blocked} · access denied {denied} · "
            f"解析失败 {parse_fail}")
        return result

    # ---------------- URL discovery: SRP ----------------
    def _collect_urls_from_srp(self, fetcher,
                               result: CrawlResult) -> list[str]:
        """搜索结果页拉 itm URL。多关键词 × 多子类 → 取并集去重。"""
        urls: list[str] = []
        seen: set[str] = set()
        # 给 URL 池留 2-3x buffer 以应对 PDP 阶段的 404 / Akamai 误伤
        url_pool_target = max(self.limit * 2, self.limit + 500)

        srp_block_streak = 0
        SRP_BLOCK_BREAK = 5     # 连续 5 个关键词全 block → 整体放弃 SRP
        for kw, cat in _HOME_CATEGORIES:
            if len(urls) >= url_pool_target:
                break
            if srp_block_streak >= SRP_BLOCK_BREAK:
                result.notes.append(
                    f"⚠ SRP 连续 {srp_block_streak} 个关键词全 block，"
                    f"放弃 SRP 路径")
                break
            kw_added = 0
            kw_blocked = False
            for page in range(1, MAX_PAGES_PER_KW + 1):
                if len(urls) >= url_pool_target:
                    break
                srp_url = (f"{self.base}/sch/i.html?_nkw={kw.replace(' ', '+')}"
                           f"&_sacat={cat}&_pgn={page}&_ipg=240")
                try:
                    res = fetcher.get(srp_url, timeout=30,
                                      impersonate="chrome131",
                                      headers={**self._headers(),
                                               "Referer": self.base + "/"})
                except Exception as exc:
                    result.notes.append(f"⚠ SRP fail {kw}/p{page}: {exc}")
                    kw_blocked = True
                    break
                if (res.status or 0) != 200 or self._is_blocked_body(res.text or ""):
                    result.notes.append(
                        f"⚠ SRP block kw={kw} cat={cat} p={page} "
                        f"code={res.status or 0} len={len(res.text or '')}")
                    # SRP 被封 → 长睡眠 + warmup
                    time.sleep(45)
                    self._warmup(fetcher)
                    kw_blocked = True
                    break
                found = _ITM_RE.findall(res.text or "")
                new = 0
                for iid in found:
                    iurl = f"{self.base}/itm/{iid}"
                    if iurl in seen:
                        continue
                    seen.add(iurl)
                    urls.append(iurl)
                    new += 1
                    kw_added += 1
                if new < 20:
                    # 接近尾页 / eBay 在去重，跳到下个关键词
                    break
                self.sleep()
            if kw_blocked and kw_added == 0:
                srp_block_streak += 1
            else:
                srp_block_streak = 0
            result.notes.append(
                f"  SRP kw={kw} cat={cat} +{kw_added}（累计 {len(urls)}）")
            self.sleep()
        return urls

    # ---------------- URL discovery: VIS sitemap (兜底) ----------------
    def _collect_urls_from_sitemap(self, fetcher,
                                   result: CrawlResult, need: int) -> list[str]:
        """VIS-0-index → GTC-0_N.xml.gz → itm URL。无类目过滤，慎用。"""
        import gzip
        urls: list[str] = []
        try:
            res = fetcher.get(f"{self.base}/lst/VIS-0-index.xml", timeout=30,
                              impersonate="chrome131",
                              headers=self._headers())
            if (res.status or 0) != 200:
                result.notes.append(f"⚠ sitemap index {res.status or 0}")
                return urls
        except Exception as exc:
            result.notes.append(f"⚠ sitemap index 不可达: {exc}")
            return urls
        subs = _LOC_RE.findall(res.text or "")
        result.notes.append(f"sitemap index: {len(subs)} 个 gz 子文件")
        for sm in subs:
            if len(urls) >= need:
                break
            try:
                sub = fetcher.get(sm, timeout=60,
                                  impersonate="chrome131",
                                  headers=self._headers())
                if (sub.status or 0) != 200:
                    continue
                # 用 res.content（bytes）做 gzip 解压，res.text 是已解码文本
                txt = gzip.decompress(sub.content).decode("utf-8",
                                                          errors="replace")
            except Exception:
                continue
            for u in _LOC_RE.findall(txt):
                if "/itm/" in u and u not in urls:
                    urls.append(u)
                    if len(urls) >= need:
                        break
            self.sleep()
        return urls

    # ---------------- parse ----------------
    def _parse_product(self, html: str, url: str) -> dict | None:
        """从 JSON-LD Product + BreadcrumbList 拼装 product dict。"""
        product_ld = None
        breadcrumb_ld = None
        for block in _LD_RE.findall(html):
            try:
                d = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("@type") or ""
            if (t == "Product" or
                    (isinstance(t, list) and "Product" in t)):
                product_ld = d
            elif t == "BreadcrumbList":
                breadcrumb_ld = d
        if not product_ld:
            return None

        title = product_ld.get("name")
        if not title:
            return None

        # SKU：mpn > sku > URL itm id
        sku = (product_ld.get("mpn") or product_ld.get("sku"))
        if not sku:
            m = _ITM_RE.search(url) or re.search(r"/itm/(\d+)", url)
            sku = m.group(1) if m else None
        if not sku:
            return None
        sku = str(sku).strip()

        # 图片
        imgs = product_ld.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        elif not isinstance(imgs, list):
            imgs = []
        # 去重 + 截前 10
        seen = set()
        image_urls: list[str] = []
        for u in imgs:
            if not isinstance(u, str) or not u or u in seen:
                continue
            seen.add(u)
            image_urls.append(u)
            if len(image_urls) >= 10:
                break

        # 价格 / 货币 / 库存
        offers = product_ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            offers = {}
        sale_price = self._num(offers.get("price"))
        currency = offers.get("priceCurrency") or "USD"
        # 原价（划线价）藏在 offers.priceSpecification.price
        original_price = sale_price
        ps = offers.get("priceSpecification")
        if isinstance(ps, dict):
            op = self._num(ps.get("price"))
            if op is not None and op > 0:
                original_price = op
        elif isinstance(ps, list) and ps:
            for sp in ps:
                if isinstance(sp, dict):
                    op = self._num(sp.get("price"))
                    if op is not None and op > 0:
                        original_price = op
                        break
        avail = str(offers.get("availability") or "").lower()
        status = ("out_of_stock"
                  if ("outofstock" in avail or "out of stock" in avail
                      or "soldout" in avail or "discontinued" in avail)
                  else "on_sale")

        # 品牌：dict 形 {'@type':'Brand','name':...} 或纯字符串
        brand_node = product_ld.get("brand")
        brand_name = None
        if isinstance(brand_node, dict):
            brand_name = brand_node.get("name")
        elif isinstance(brand_node, str):
            brand_name = brand_node

        # 描述：JSON-LD description > OG description
        description = product_ld.get("description")
        if not description:
            description = self._og(html, "og:description")

        # 评分 / 评论数（部分 PDP 有）
        rating = review_count = None
        agg = product_ld.get("aggregateRating")
        if isinstance(agg, dict):
            rating = self._num(agg.get("ratingValue"))
            rc = agg.get("reviewCount") or agg.get("ratingCount")
            if rc is not None:
                try:
                    review_count = int(rc)
                except (TypeError, ValueError):
                    pass

        # 分类：BreadcrumbList itemListElement[].name（跳过 'eBay' 顶节点）
        category_path = self._breadcrumb_path(breadcrumb_ld)

        row = {
            "sku": sku,
            "spu": sku,
            "title": title,
            "description": description,
            "image_urls": image_urls,
            "category_path": category_path,
            "sale_price": sale_price,
            "original_price": original_price,
            "currency": currency,
            "status": status,
            "product_url": url,
            "site": self.site.site,
            "brand": brand_name or self.site.brand,
        }
        if rating is not None:
            row["ratings"] = rating
        if review_count is not None:
            row["review_count"] = review_count
        return row

    # ---------------- helpers ----------------
    @staticmethod
    def _is_blocked_body(html: str) -> bool:
        """识别 Akamai / PerimeterX 挑战页（13KB 小页面）。

        eBay 正常 SRP/PDP 总 >100KB（含完整 React SSR）。
        实测看到两种挑战页：
          · Akamai Access Denied（含 errors.edgesuite.net）
          · PerimeterX Pardon Our Interruption（小页 + 一段 px js）
        策略：len < 50K → 视为挑战（不再纠结具体 marker），<5K 必然封禁。
        """
        if not html:
            return True
        if len(html) < 50_000:
            return True
        return any(m in html for m in _BLOCK_MARKERS)

    @staticmethod
    def _og(html: str, prop: str) -> str | None:
        """从大写 / 无引号的 eBay meta 里抠 OG 值。"""
        for k, v in _OG_RE.findall(html):
            if k.lower() == prop.lower():
                return v
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

    @staticmethod
    def _breadcrumb_path(ld: dict | None) -> str | None:
        if not ld or not isinstance(ld, dict):
            return None
        names: list[str] = []
        for item in ld.get("itemListElement", []):
            if not isinstance(item, dict):
                continue
            n = item.get("name")
            if not n:
                inner = item.get("item")
                if isinstance(inner, dict):
                    n = inner.get("name")
            if n and n.lower() not in ("ebay", "home", ""):
                names.append(n)
        return "/".join(names[:4]) or None
