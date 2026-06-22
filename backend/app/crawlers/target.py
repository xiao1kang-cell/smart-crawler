"""Target.com 采集器 —— US #3 电商，Akamai Bot Manager（无 PX，比 Walmart 软）。

数据源：RedSky API（Target 自己的前端 API，公开 key 直接调）
  - SRP: redsky.target.com/redsky_aggregations/v1/web/plp_search_v2
  - PDP: redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1
  - key: ff457966e64d5e877fdbad070f276d18ecec4a01（公开前端 key，覆盖稳定）

反爬实测：
  - curl_cffi（impersonate=chrome131）直连 PDP 通常 200
  - SRP 翻页 > 5 页/IP 触发 403
  - PDP 单 IP 50+ 才出问题
  - Datacenter 代理 + chrome131 fingerprint 够用，不强求 residential

家居家具 category id=5xtg6（Home）。

反爬等级：3/5（API 比 HTML 抓取快 10x，但 RedSky key 撤销风险）。
"""
from __future__ import annotations

import gzip
import html
import os
import re
import uuid

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

API = "https://redsky.target.com/redsky_aggregations/v1/web"
KEY = os.environ.get("TARGET_REDSKY_KEY",
                     "ff457966e64d5e877fdbad070f276d18ecec4a01")
DEFAULT_LIMIT = int(os.environ.get("TARGET_LIMIT", "1000"))
STORE_ID = os.environ.get("TARGET_STORE_ID", "1768")  # Brentwood CA 物理店
MAX_OFFSET = int(os.environ.get("TARGET_MAX_OFFSET", "240"))
SITEMAP_INDEX = "https://www.target.com/sitemap_pdp-index.xml.gz"

# 实测：单字宽泛词（sofa）返 0；具体 2-3 字组合稳定返 24 条
# 不带 category（5xtg6 反而 gate 掉结果）
_HOME_KW = [
    "desk", "office chair", "dining table", "coffee table", "bookshelf",
    "sectional sofa", "accent chair", "bed frame", "nightstand", "dresser",
    "cookware set", "knife set", "blender", "coffee maker", "toaster",
    "bedding set", "comforter", "throw pillow", "blanket", "bath towel",
    "shower curtain", "bath mat", "area rug", "table lamp", "floor lamp",
    "wall mirror", "wall art", "curtain panel", "patio chair", "outdoor table",
    "storage bin", "shelving unit",
]


class TargetCrawler(BaseCrawler):
    platform = "target"

    def __init__(self, site, limit=None):
        super().__init__(site)
        self.base = "https://www.target.com"
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)
        self.visitor = uuid.uuid4().hex.upper()

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def crawl(self) -> CrawlResult:
        if os.environ.get("TARGET_USE_REDSKY", "0") != "1":
            return self._crawl_sitemap_only()
        return self._crawl_redsky()

    def _crawl_sitemap_only(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(
            source="target_sitemap",
            timeout=30,
            use_proxy=False,
        )
        urls = self._collect_sitemap_urls(fetcher, result)
        targets = urls[: self.limit]
        rows = [self._row_from_sitemap(entry) for entry in targets]
        result.products.extend(row for row in rows if row)
        result.notes.append(
            f"Target sitemap-only 产出 {len(result.products)} 个商品"
            "（价格/库存字段后续由 RedSky/PDP 增量补齐）")
        return result

    def _crawl_redsky(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(
            source="target_redsky",
            timeout=30,
            use_proxy=True,
        )
        tcins: list[str] = []
        seen: set[str] = set()

        # ---------- SRP 阶段：多关键词 × offset 翻页 ----------
        for kw in _HOME_KW:
            if len(tcins) >= self.limit * 2:
                break
            for offset in range(0, MAX_OFFSET, 24):
                params = {
                    "key": KEY,
                    "keyword": kw,
                    "channel": "WEB",
                    "count": "24",
                    "offset": str(offset),
                    "page": f"/s/{kw}",
                    "platform": "desktop",
                    "pricing_store_id": STORE_ID,
                    "scheduled_delivery_store_id": STORE_ID,
                    "store_ids": STORE_ID,
                    "useragent": self.ua(),
                    "visitor_id": self.visitor,
                }
                res = fetcher.get(
                    f"{API}/plp_search_v2",
                    headers=self._headers(),
                    params=params,
                )
                if not res.ok:
                    self.guard(res.status or 0, "target_srp")
                    self.sleep()
                    break
                js = res.json() or {}
                items = (js.get("data", {}).get("search", {})
                         .get("products") or [])
                if not items:
                    break
                for it in items:
                    t = it.get("tcin")
                    if t and t not in seen:
                        seen.add(t)
                        tcins.append(t)
                self.sleep()
            result.notes.append(f"  kw={kw} 累计 {len(tcins)} TCINs")

        if not tcins:
            result.notes.append("⚠ RedSky SRP 全部失败 —— 检查 key 是否撤销")
            return result

        # ---------- PDP 阶段：批量调 pdp_client_v1 ----------
        ok = denied = streak = 0
        for tcin in tcins[: self.limit * 2]:
            if ok >= self.limit:
                break
            res = fetcher.get(
                f"{API}/pdp_client_v1",
                headers=self._headers(),
                params={
                    "key": KEY,
                    "tcin": tcin,
                    "is_bot": "false",
                    "store_id": STORE_ID,
                    "pricing_store_id": STORE_ID,
                    "has_pricing_store_id": "true",
                    "visitor_id": self.visitor,
                    "channel": "WEB",
                    "page": f"/p/-/A-{tcin}",
                },
            )
            if res.status in (403, 429):
                denied += 1
                streak += 1
                if streak >= 8:
                    raise BlockedError(
                        f"target 连续 {streak} 次 403/429，熔断")
                self.sleep()
                continue
            if not res.ok:
                self.sleep()
                continue
            streak = 0
            js = res.json() or {}
            url = f"{self.base}/p/-/A-{tcin}"
            row = self._map_pdp(js, tcin, url)
            if row:
                self.snapshot(tcin, res.text)
                result.products.append(row)
                ok += 1
            else:
                denied += 1
            self.sleep()

        result.notes.append(
            f"Target RedSky 成功 {ok}/{len(tcins)} · 解析失败 {denied}")
        return result

    def _collect_sitemap_urls(self, fetcher, result: CrawlResult) -> list[dict]:
        shards = self._sitemap_locs(fetcher, SITEMAP_INDEX)
        result.notes.append(f"Target sitemap index 发现 {len(shards)} 个子图")
        out: list[dict] = []
        seen: set[str] = set()
        sample_limit = max(self.limit * 10, 200)
        for shard in shards:
            entries = self._sitemap_entries(fetcher, shard)
            for entry in entries:
                url = entry.get("url") or ""
                if "/p/" not in url or "/-/A-" not in url or url in seen:
                    continue
                seen.add(url)
                out.append(entry)
                if len(out) >= sample_limit:
                    return self._prioritize_home(out)
        return self._prioritize_home(out)

    def _sitemap_locs(self, fetcher, url: str) -> list[str]:
        text = self._sitemap_text(fetcher, url)
        return re.findall(r"<loc>\s*(.*?)\s*</loc>", text)

    def _sitemap_entries(self, fetcher, url: str) -> list[dict]:
        text = self._sitemap_text(fetcher, url)
        entries: list[dict] = []
        for block in re.findall(r"<url>(.*?)</url>", text, re.S):
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S)
            if not loc:
                continue
            images = re.findall(
                r"<image:loc>\s*(.*?)\s*</image:loc>", block, re.S)
            entries.append({
                "url": html.unescape(loc.group(1).strip()),
                "images": [html.unescape(x.strip()) for x in images if x.strip()],
            })
        return entries

    def _sitemap_text(self, fetcher, url: str) -> str:
        res = fetcher.get(url, headers=self._headers(), timeout=30)
        raw = res.content or (res.text or "").encode()
        if url.endswith(".gz"):
            try:
                return gzip.decompress(raw).decode("utf-8", "ignore")
            except Exception:
                pass
        try:
            return raw.decode("utf-8", "ignore")
        except Exception:
            return res.text or ""

    def _row_from_sitemap(self, entry: dict) -> dict | None:
        url = entry.get("url") or ""
        match = re.search(r"/-/A-(\d+)", url)
        if not match:
            return None
        sku = match.group(1)
        title = _title_from_target_url(url)
        return {
            "sku": sku,
            "spu": sku,
            "title": title,
            "description": None,
            "image_urls": entry.get("images") or [],
            "category_path": None,
            "sale_price": None,
            "original_price": None,
            "currency": "USD",
            "ratings": None,
            "review_count": None,
            "status": "on_sale",
            "brand": self.site.brand,
            "product_url": url,
            "site": self.site.site,
            "attributes": {"source": "sitemap"},
        }

    @staticmethod
    def _prioritize_home(entries: list[dict]) -> list[dict]:
        terms = (
            "rug", "pillow", "lamp", "table", "chair", "sofa", "couch",
            "cabinet", "shelf", "storage", "curtain", "bedding", "comforter",
            "mattress", "decor", "kitchen", "bath", "patio", "outdoor",
            "furniture", "dining", "desk", "mirror", "blanket",
        )
        return sorted(
            entries,
            key=lambda e: (
                not any(t in (e.get("url") or "").lower() for t in terms),
                e.get("url") or "",
            ),
        )

    def _map_pdp(self, js: dict, tcin: str, url: str) -> dict | None:
        try:
            prod = js.get("data", {}).get("product", {})
            item = prod.get("item") or {}
            desc = item.get("product_description") or {}
            images = (item.get("enrichment") or {}).get("images") or {}
            price = prod.get("price") or {}
            rr = (prod.get("ratings_and_reviews") or {}).get("statistics") \
                or {}
            classification = item.get("product_classification") or {}
        except AttributeError:
            return None

        primary = images.get("primary_image_url")
        alt = images.get("alternate_image_urls") or []
        img_urls = ([primary] if primary else []) + alt

        bullets = (desc.get("soft_bullets") or {}).get("bullets") or []
        desc_text = (desc.get("downstream_description")
                     or " ".join(bullets) if bullets else None)

        avail = (item.get("fulfillment", {}).get("is_out_of_stock_in_all_store_locations")
                 if isinstance(item.get("fulfillment"), dict) else False)
        status = "out_of_stock" if avail else "on_sale"

        cat_path = (classification.get("item_type", {})
                    if isinstance(classification.get("item_type"), dict)
                    else {}).get("name")

        avg = (rr.get("rating") or {}).get("average")
        count = (rr.get("rating") or {}).get("count")

        return {
            "sku": str(tcin),
            "spu": str(tcin),
            "title": desc.get("title"),
            "description": desc_text,
            "image_urls": img_urls,
            "category_path": cat_path,
            "sale_price": _num(price.get("current_retail")),
            "original_price": _num(price.get("reg_retail")
                                   or price.get("current_retail")),
            "currency": "USD",
            "ratings": _num(avg),
            "review_count": _int(count),
            "status": status,
            "brand": (item.get("primary_brand") or {}).get("name")
            or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").strip()
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


def _title_from_target_url(url: str) -> str:
    match = re.search(r"/p/([^/]+)/-/A-\d+", url)
    slug = match.group(1) if match else url.rstrip("/").split("/")[-1]
    return html.unescape(slug.replace("-", " ").strip()).title()
