"""美客多(MercadoLibre)按需采集器。

实测验证(2026-06,住宅代理 + 真浏览器 + 滚动等待):**可用**。
  关键配方:items API 已强制 OAuth(403),改走商品页真浏览器渲染。但价格是
  懒加载,**必须滚动 + 等待**才会出现(只 fetch 不滚动只拿到无价格的壳页)。
    · listing 数据源 = 页面里的 JSON-LD(<script type="application/ld+json"> 的
      Product 块,schema.org 标准格式,最干净):
        title  = name
        price  = offers.price          currency = offers.priceCurrency
        avail  = offers.availability    brand    = brand(字符串或 {name})
        rating = aggregateRating.ratingValue   review_count = aggregateRating.reviewCount
        image  = image(字符串或数组)    sku = sku / productID
    · 评论数据源 = noindex 评论分页 JSON 接口(2026-06 逆向,免 token/cookie,
      数据中心 IP 直连即可,**不需要浏览器**):
        GET https://{host}/noindex/catalog/reviews/{ID}/search
            ?objectId={ID}&siteId={MLB|MLA|MLM…}&isItem=true&offset=N&limit=15&rating=R
        返回 {"reviews":[{id, rating, comment:{content:{text}, date}}]}。
      硬限制(平台侧):limit 锁死 15;单视角 offset 上限 ~300(≥315 返回空);
      无 paging.total。**唯一扩容手段:rating=5..1 分桶**(只有 rating= 生效,
      sort/order/filter 全是埋点假参数),每桶各 ~300 → 5 桶合计**约 1500 条/商品**
      封顶,深层评论平台不开放(标称上万的商品也只能拿这个量级)。
反爬:     listing 抓取强,强制住宅代理(proxy_tier=residential)+ 真浏览器 + 滚动;
          裸 IP / 无滚动会被弹到 /gz/account-verification 或只拿到壳页。评论 JSON 接口
          反爬宽松(裸 IP 可直连),但生产高频仍走代理。
          ⚠️ 稳定性:住宅 IP 信誉时好时坏,runner 已切代理重试 _MAX_RETRY 次。浏览器
          locale 必须随站点 ccTLD 走(BR 站 -> pt-BR/巴西时区),否则抬高被弹概率。
URL->id:  商品页 URL 含 MLM-/MLB-/MLA- 编码(catalog 用 /p/MLA…;单卖家 wid=MLA…)。
"""
from __future__ import annotations

import json
import re

from curl_cffi import requests as creq

from ..antiban import BlockedError, check_blocked
from .base import BaseOnDemand

_ID_RE = re.compile(r"(ML[A-Z])-?(\d+)")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
PLATFORM = "mercadolibre"
SITE = f"ondemand_{PLATFORM}"

# 评论分页接口:单页固定 15 条,单视角 offset ~300 封顶,靠 rating 分桶扩容。
_REVIEW_PAGE = 15
_REVIEW_MAX_OFFSET = 315          # ≥315 返回空,留一档冗余
_REVIEW_RATINGS = (5, 4, 3, 2, 1)  # 分桶维度(唯一生效的扩容参数)

# 域名 ccTLD -> 国家代码(决定浏览器 locale/timezone 指纹)。美客多多国站,
# locale 必须与目标域名对齐,否则反爬易弹 /gz/account-verification 壳页。
_TLD_COUNTRY = {
    "br": "BR", "mx": "MX", "ar": "AR", "cl": "CL", "co": "CO",
    "uy": "UY", "pe": "PE", "ec": "EC", "ve": "VE",
}


def _country_for(url: str) -> str:
    """从 URL 域名末段 ccTLD 推断国家;无法识别时退回 BR(站点流量最大)。"""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return _TLD_COUNTRY.get(host.rsplit(".", 1)[-1] if "." in host else "", "BR")


def _ld_product(html: str) -> dict | None:
    """从页面 HTML 抽出 JSON-LD 里 @type=Product 的块。"""
    for block in _LD_RE.findall(html):
        try:
            d = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
    return None


class MercadoLibreOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "residential"

    @staticmethod
    def parse_item_id(url: str) -> str:
        m = _ID_RE.search(url)
        if not m:
            raise ValueError(f"美客多 URL 无商品编码: {url}")
        return (m.group(1) + m.group(2)).upper()

    @staticmethod
    def parse_listing(html: str, url: str) -> dict:
        """从渲染后的页面 HTML 解析 listing(数据源:JSON-LD Product 块)。"""
        d = _ld_product(html)
        if not d:
            raise BlockedError("ml/pdp 未找到 JSON-LD Product(疑似壳页/未渲染)")
        offers = d.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        brand = d.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        img = d.get("image")
        if isinstance(img, str):
            imgs = [img]
        elif isinstance(img, list):
            imgs = [u for u in img if isinstance(u, str)]
        else:
            imgs = []
        agg = d.get("aggregateRating") or {}
        avail = str(offers.get("availability") or "")
        return {
            "sku": d.get("sku") or d.get("productID"),
            "title": d.get("name"),
            "sale_price": offers.get("price"),
            "original_price": offers.get("price"),
            "currency": offers.get("priceCurrency"),
            "image_urls": imgs,
            "ratings": agg.get("ratingValue"),
            "review_count": agg.get("reviewCount") or agg.get("ratingCount"),
            "status": "on_sale" if "InStock" in avail else "out_of_stock",
            "product_url": url,
            "site": SITE,
            "brand": brand or PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        """从评论分页接口的 JSON 解析评论(数据源:noindex/…/search)。

        每条结构:{id, rating, comment:{content:{text}, date}}。用真实数字
        id 作 review_id(去重稳),正文取 comment.content.text,日期是相对
        文案(如 "Há mais de 1 ano")原样保留——接口不给绝对时间。
        """
        sku = item_id[0] if isinstance(item_id, tuple) else item_id
        out = []
        for r in (data or {}).get("reviews") or []:
            rid = r.get("id")
            comment = r.get("comment") or {}
            content = ((comment.get("content") or {}).get("text") or "").strip()
            if rid is None or not content:   # 缺 id / 空正文 → 跳过
                continue
            out.append({
                "review_id": str(rid),
                "platform": SITE,
                "site": SITE,
                "reviewer_name": None,
                "rating": r.get("rating"),
                "title": None,
                "content": content,
                "review_date": comment.get("date"),
                "sku": sku,
                "product_url": url,
            })
        return out

    # ---- HTTP(真浏览器渲染,smoke 路径)----
    def _render(self, url: str, proxy=None) -> str:
        """住宅代理 + 真浏览器 + 滚动等待渲染。价格/评论懒加载,必须滚动。"""
        from scrapling.fetchers import StealthyFetcher

        from ..crawlers._stealth_config import stealth_kwargs

        def _scroll(page):
            try:
                for y in (3000, 6000, 9000, 12000):
                    page.mouse.wheel(0, y)
                    page.wait_for_timeout(1800)
            except Exception:
                pass
            return page

        kw = stealth_kwargs(proxy=proxy, country=_country_for(url),
                            solve_cloudflare=True,
                            network_idle=True, timeout_ms=90000,
                            extra={"wait": 4000, "page_action": _scroll})
        page = StealthyFetcher.fetch(url, **kw)
        html = page.html_content or page.body or ""
        if "account-verification" in html[:3000]:
            raise BlockedError("ml/pdp 被弹到账号验证页(代理 IP 信誉不足)")
        return html

    def fetch_listing(self, item_id: str, url: str, proxy=None) -> dict:
        html = self._render(url, proxy=proxy)
        return self.parse_listing(html, url)

    @staticmethod
    def _review_host(url: str) -> str:
        """评论接口走商品页同域(www.<站点>),去掉 articulo./produto. 等子域。"""
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "www.mercadolivre.com.br")
        # 站点根域:取末两段(com.br)或三段(com.ar→ .com.ar);统一前缀 www.
        host = re.sub(r"^(articulo|produto|www)\.", "", host)
        return f"www.{host}"

    @staticmethod
    def _site_id(item_id) -> str:
        """itemId 前缀即 siteId:MLB3856… → MLB。"""
        iid = item_id[0] if isinstance(item_id, tuple) else item_id
        m = re.match(r"(ML[A-Z])", str(iid) or "")
        return m.group(1) if m else "MLB"

    def fetch_reviews(self, item_id: str, url: str, limit: int = 100,
                      proxy=None, known_ids=None) -> list[dict]:
        """抓评论(数据源:noindex 评论分页 JSON 接口,order=dateCreated 时间倒序)。

        · 首次全量(known_ids 空):单页 15 条、单视角 offset ~300 封顶,故对
          rating=5..1 逐桶翻 offset,跨桶按真实 id 去重,累计达 limit 或抓尽即停。
        · 增量(known_ids 非空):库里已有该商品评论,只翻默认视角(不分桶),
          **碰到第一条已知 id 即停**——order=dateCreated 严格时间倒序保证后面全是旧的。

        curl_cffi 直连(反爬宽松),非 JSON / 被封 → BlockedError 交 runner 切代理重试。
        """
        iid = item_id[0] if isinstance(item_id, tuple) else item_id
        host, site_id = self._review_host(url), self._site_id(item_id)
        base = (f"https://{host}/noindex/catalog/reviews/{iid}/search"
                f"?objectId={iid}&siteId={site_id}&isItem=true&order=dateCreated")
        s = creq.Session(impersonate="chrome")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        s.headers.update({"Referer": url, "Accept": "application/json"})

        known = set(known_ids or ())
        seen: set[str] = set()
        out: list[dict] = []
        # 增量:只翻默认视角(rating=None);首次全量:逐星级桶。
        buckets = (None,) if known else _REVIEW_RATINGS
        for rating in buckets:
            offset = 0
            while offset < _REVIEW_MAX_OFFSET and len(out) < limit:
                api = f"{base}&offset={offset}&limit={_REVIEW_PAGE}"
                if rating is not None:
                    api += f"&rating={rating}"
                resp = s.get(api, timeout=40)
                check_blocked(resp.status_code, "ml/reviews")
                resp.raise_for_status()
                if "json" not in resp.headers.get("content-type", ""):
                    raise BlockedError("ml/reviews 返回非 JSON(疑似 IP 限速)")
                data = resp.json()
                # 翻页判据用**原始**条数:部分评论只有星级无正文,会被 parse 过滤,
                # 不能拿过滤后的数量判断是否末页,否则富桶会早停。
                raw_n = len(data.get("reviews") or [])
                hit_known = False
                for r in self.parse_reviews(data, item_id, url):
                    if r["review_id"] in known:   # 增量:碰到已有 → 后面全是旧的
                        hit_known = True
                        break
                    if r["review_id"] in seen:
                        continue
                    seen.add(r["review_id"])
                    out.append(r)
                if hit_known or raw_n < _REVIEW_PAGE:   # 撞到已知 / 原始不足一页 → 该桶抓尽
                    break
                offset += _REVIEW_PAGE
            if len(out) >= limit:
                break
        return out[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100,
                          proxy=None) -> list[str]:
        """列表/搜索页枚举 itemId(渲染后从页面内 ML 编码兜底)。"""
        html = self._render(url, proxy=proxy)
        ids = []
        for m in _ID_RE.finditer(html):
            iid = (m.group(1) + m.group(2)).upper()
            if iid not in ids:
                ids.append(iid)
            if len(ids) >= max_items:
                break
        return ids
