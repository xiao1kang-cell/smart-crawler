"""虾皮(Shopee)按需采集器。

⚠️ 状态(2026-06-05):**未经真实站点验证**。下面的接口路径与 parse_* 解析逻辑是
    按「假设的 Shopee API 结构」写的,只过了用假 fixture 的单测,从未打过真实 Shopee。
    姊妹平台 Lazada 已踩过坑:实测后发现原先假设的 JSON 结构与真实站点完全对不上,
    listing 必须改真浏览器渲染、解析路径全部重写。**Shopee 大概率需要同样一轮逆向**:
      · get_pc / get_ratings 近年常加签名头(af-ac-enc-dat 等),裸调可能直接被拒;
      · 真实响应字段名/层级需以实测为准(参考 Lazada 重写的做法:
        先 StealthyFetcher 抓真实页/接口 -> dump 结构 -> 按真实路径写 parse + 真实 fixture)。
    在完成真实验证前,勿把本采集器当作可用。

listing:  GET https://{host}/api/v4/pdp/get_pc?shop_id={s}&item_id={i}       (待验证)
reviews:  GET https://{host}/api/v2/item/get_ratings?shopid={s}&itemid={i}    (待验证)
URL->id:  单品 URL 形如  .../<slug>-i.<shopid>.<itemid>  或  /product/<shopid>/<itemid>
反爬:     最强,强制住宅代理(proxy_tier=residential)+ 拟人头/限速;失败由 runner 切代理重试。
价格:     (假设)Shopee 价格字段放大 100000 倍,解析时除回 —— 需真实验证。
图片:     (假设)字段是 hash,拼 https://cf.{host}/file/<hash> —— 需真实验证。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from curl_cffi import requests as creq

from ..antiban import check_blocked
from .base import BaseOnDemand

_ID_DOT_RE = re.compile(r"-i\.(\d+)\.(\d+)")
_ID_PATH_RE = re.compile(r"/product/(\d+)/(\d+)")
_PRICE_SCALE = 100000
PLATFORM = "shopee"
SITE = f"ondemand_{PLATFORM}"
_IMG_BASE = "https://cf.shopee.com.my/file/"


class ShopeeOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "residential"

    @staticmethod
    def parse_item_id(url: str):
        m = _ID_DOT_RE.search(url) or _ID_PATH_RE.search(url)
        if not m:
            raise ValueError(f"Shopee URL 无 shopid.itemid: {url}")
        return m.group(1), m.group(2)

    @staticmethod
    def _img(hash_or_url: str) -> str:
        if not hash_or_url:
            return ""
        if hash_or_url.startswith("http"):
            return hash_or_url
        return _IMG_BASE + hash_or_url

    @staticmethod
    def parse_listing(data: dict, url: str) -> dict:
        it = data.get("data", {}).get("item", {}) or {}
        shopid, itemid = it.get("shopid"), it.get("itemid")
        imgs = it.get("images") or ([it["image"]] if it.get("image") else [])
        rating = (it.get("item_rating") or {}).get("rating_star")
        return {
            "sku": f"{shopid}_{itemid}",
            "title": it.get("name"),
            "sale_price": (it.get("price") or 0) / _PRICE_SCALE or None,
            "original_price": (it.get("price_before_discount") or it.get("price") or 0)
            / _PRICE_SCALE or None,
            "currency": it.get("currency"),
            "image_urls": [ShopeeOnDemand._img(h) for h in imgs],
            "ratings": rating,
            "inventory": str(it.get("stock")) if it.get("stock") is not None else None,
            "status": ("on_sale" if (it.get("stock") or 0) > 0 else "out_of_stock"),
            "product_url": url,
            "site": SITE,
            "brand": PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        if isinstance(item_id, tuple):
            sku = f"{item_id[0]}_{item_id[1]}"
        else:
            sku = str(item_id)
        out = []
        for r in (data.get("data", {}).get("ratings") or []):
            ctime = r.get("ctime")
            rdate = (datetime.fromtimestamp(ctime, tz=timezone.utc).isoformat()
                     if ctime else None)
            out.append({
                "review_id": str(r["cmtid"]) if r.get("cmtid") is not None else None,
                "platform": SITE,
                "site": SITE,
                "reviewer_name": r.get("author_username"),
                "rating": r.get("rating_star"),
                "title": None,
                "content": r.get("comment"),
                "review_date": rdate,
                "sku": sku,
                "product_url": url,
            })
        return out

    # ---- HTTP(smoke 路径)----
    def _session(self, proxy):
        s = creq.Session(impersonate="chrome")
        s.headers.update({"Referer": "https://shopee.com/",
                          "X-Requested-With": "XMLHttpRequest"})
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def _host(self, url: str) -> str:
        return re.sub(r"^https?://", "", url).split("/")[0]

    def fetch_listing(self, item_id, url: str, proxy=None) -> dict:
        shopid, itemid = item_id
        s = self._session(proxy)
        api = (f"https://{self._host(url)}/api/v4/pdp/get_pc"
               f"?shop_id={shopid}&item_id={itemid}")
        resp = s.get(api, timeout=30)
        check_blocked(resp.status_code, "shopee/pdp")
        resp.raise_for_status()
        return self.parse_listing(resp.json(), url)

    def fetch_reviews(self, item_id, url: str, limit: int = 100, proxy=None):
        shopid, itemid = item_id
        s = self._session(proxy)
        out, offset = [], 0
        while len(out) < limit and offset < 20 * 20:   # 兜底页数上限,与 lazada 一致
            api = (f"https://{self._host(url)}/api/v2/item/get_ratings"
                   f"?shopid={shopid}&itemid={itemid}&offset={offset}&limit=20")
            resp = s.get(api, timeout=30)
            check_blocked(resp.status_code, "shopee/ratings")
            resp.raise_for_status()
            batch = self.parse_reviews(resp.json(), item_id, url)
            if not batch:
                break
            out.extend(batch)
            offset += 20
        return out[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100, proxy=None):
        """店铺/类目页枚举。Shopee 店铺 API:
        GET /api/v4/shop/search_items?shopid=...&limit=...  首版用页面正则兜底。"""
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "shopee/listing")
        resp.raise_for_status()
        ids = []
        for m in list(_ID_DOT_RE.finditer(resp.text)) + list(_ID_PATH_RE.finditer(resp.text)):
            pair = (m.group(1), m.group(2))
            if pair not in ids:
                ids.append(pair)
            if len(ids) >= max_items:
                break
        return ids
