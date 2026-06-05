"""美客多(MercadoLibre)按需采集器。

⚠️ 状态(2026-06-05 实测):**当前不可用,待接 OAuth**。
    api.mercadolibre.com/items/{id} 现已强制 OAuth 鉴权 —— 无 token 直连一律返回
    403(PA_UNAUTHORIZED_RESULT_FROM_POLICIES,PolicyAgent)。
    实测矩阵(本地 / 住宅代理 / curl_cffi / 真浏览器 全试过):
      · items API 直连(含住宅代理)        -> 403 OAuth
      · 商品页 HTML(curl_cffi / 真浏览器)  -> 只拿到无价格的 SEO 占位页;价格/评论
                                              需登录态 + 真实交互后异步加载,抓不到
    结论:listing/评论都拿不到。下一步二选一:
      (A) 接入官方 OAuth(developers.mercadolibre 注册应用 -> access_token,带 token
          调 items + reviews API,最稳最全);
      (B) 坚持免 token 则需更深的浏览器自动化(登录态 + 行为模拟),成本高、不稳。
    落地前,本采集器对真实 URL 会因 403 触发 runner 切代理重试并最终放弃,notes 有
    明确提示。下面的 parse_listing/parse_reviews 是按 items API 响应结构写的,接上
    OAuth 后即可复用。

listing:  GET https://api.mercadolibre.com/items/{id}        (需 OAuth)
reviews:  GET https://api.mercadolibre.com/reviews/item/{id}  (需 OAuth)
URL->id:  商品页 URL 含 MLM-123 / MLB-123 / MLA-123 编码,去掉短横即 itemId。
反爬:     PolicyAgent 强制鉴权(非简单封禁),无 token 不可达。
"""
from __future__ import annotations

import re

from curl_cffi import requests as creq

from ..antiban import check_blocked
from .base import BaseOnDemand

_API = "https://api.mercadolibre.com"
_ID_RE = re.compile(r"(ML[A-Z])-?(\d+)")
PLATFORM = "mercadolibre"
SITE = f"ondemand_{PLATFORM}"


class MercadoLibreOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "none"

    @staticmethod
    def parse_item_id(url: str) -> str:
        m = _ID_RE.search(url)
        if not m:
            raise ValueError(f"美客多 URL 无商品编码: {url}")
        return (m.group(1) + m.group(2)).upper()

    @staticmethod
    def parse_listing(data: dict, url: str) -> dict:
        return {
            "sku": data.get("id"),
            "title": data.get("title"),
            "sale_price": data.get("price"),
            "original_price": data.get("original_price") or data.get("price"),
            "currency": data.get("currency_id"),
            "image_urls": [p.get("url") for p in (data.get("pictures") or [])
                           if p.get("url")],
            "inventory": str(data.get("available_quantity"))
            if data.get("available_quantity") is not None else None,
            "status": ("on_sale" if (data.get("available_quantity") or 0) > 0
                       else "out_of_stock"),
            "product_url": url,
            "site": SITE,
            "brand": PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        out = []
        for r in (data.get("reviews") or []):
            out.append({
                "review_id": r.get("id"),
                "platform": SITE,
                "site": SITE,
                "reviewer_name": r.get("reviewer_id"),
                "rating": r.get("rate"),
                "title": r.get("title"),
                "content": r.get("content"),
                "review_date": r.get("date_created"),
                "sku": item_id,
                "product_url": url,
            })
        return out

    # ---- HTTP(smoke 路径,单测不覆盖)----
    def _session(self, proxy: str | None) -> "creq.Session":
        s = creq.Session(impersonate="chrome")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def fetch_listing(self, item_id: str, url: str, proxy=None) -> dict:
        s = self._session(proxy)
        resp = s.get(f"{_API}/items/{item_id}", timeout=30)
        check_blocked(resp.status_code, f"ml/items/{item_id}")
        resp.raise_for_status()
        return self.parse_listing(resp.json(), url)

    def fetch_reviews(self, item_id: str, url: str, limit: int = 100,
                      proxy=None) -> list[dict]:
        s = self._session(proxy)
        resp = s.get(f"{_API}/reviews/item/{item_id}", timeout=30)
        check_blocked(resp.status_code, f"ml/reviews/{item_id}")
        resp.raise_for_status()
        return self.parse_reviews(resp.json(), item_id, url)[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100,
                          proxy=None) -> list[str]:
        """列表/搜索页枚举 itemId。美客多搜索 API:
        GET /sites/{SITE_ID}/search?q=... 或店铺 API。首版用页面内 ML 编码兜底。"""
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "ml/listing")
        resp.raise_for_status()
        ids = []
        for m in _ID_RE.finditer(resp.text):
            iid = (m.group(1) + m.group(2)).upper()
            if iid not in ids:
                ids.append(iid)
            if len(ids) >= max_items:
                break
        return ids
