"""Shopify 采集器 —— SONGMICS 系站点。

SONGMICS 是标准 Shopify 站，公开 /products.json，无需浏览器 / 选择器：
  GET /products.json?limit=250&page=N      → 全量 SKU（翻页到空）
  GET /collections.json?limit=250&page=N   → 分类树
  GET /collections/new/products.json       → 新品
  GET /collections/top-picks/products.json → 热销品
"""
from __future__ import annotations

from curl_cffi import requests as creq

from .base import BaseCrawler, CrawlResult

PAGE_SIZE = 250
MAX_PAGES = 80          # 250 * 80 = 2 万 SKU 上限保护


class ShopifyCrawler(BaseCrawler):
    platform = "shopify"

    def _session(self) -> creq.Session:
        s = creq.Session(impersonate="chrome")
        s.headers.update({"User-Agent": self.ua(), "Accept": "application/json"})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def _get_json(self, sess: creq.Session, path: str) -> dict:
        url = self.site.url.rstrip("/") + path
        resp = sess.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _handles(self, sess: creq.Session, collection: str) -> set[str]:
        """取某 collection 下全部商品 handle（用于打新品/热销标签）。"""
        handles: set[str] = set()
        try:
            for page in range(1, 10):
                data = self._get_json(
                    sess, f"/collections/{collection}/products.json"
                    f"?limit={PAGE_SIZE}&page={page}")
                items = data.get("products", [])
                if not items:
                    break
                handles.update(p["handle"] for p in items)
                self.sleep()
        except Exception:
            pass            # 站点无此 collection，忽略
        return handles

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        sess = self._session()

        new_handles = self._handles(sess, "new")
        best_handles = self._handles(sess, "top-picks") | self._handles(sess, "best-sellers")
        result.notes.append(f"新品 collection {len(new_handles)} 款 / "
                             f"热销 collection {len(best_handles)} 款")

        # ---- 全量商品 ----
        for page in range(1, MAX_PAGES + 1):
            data = self._get_json(sess, f"/products.json?limit={PAGE_SIZE}&page={page}")
            products = data.get("products", [])
            if not products:
                break
            for prod in products:
                result.products.extend(self._expand(prod, new_handles, best_handles))
            self.sleep()

        # ---- 分类树 ----
        result.categories = self._crawl_categories(sess)
        return result

    def _expand(self, prod: dict, new_handles: set, best_handles: set) -> list[dict]:
        """一个 Shopify product 展开成「每变体一行」。"""
        rows = []
        handle = prod.get("handle", "")
        images = [img.get("src") for img in prod.get("images", []) if img.get("src")]
        opt_names = [o.get("name") for o in prod.get("options", [])]
        product_url = self.site.url.rstrip("/") + "/products/" + handle
        is_new = handle in new_handles
        is_best = handle in best_handles
        label = "NEW" if is_new else ("BEST SELLER" if is_best else None)

        for v in prod.get("variants", []):
            attrs = {}
            for i, name in enumerate(opt_names, start=1):
                val = v.get(f"option{i}")
                if val and val != "Default Title":
                    attrs[name] = val
            sale = v.get("price")
            compare = v.get("compare_at_price")
            rows.append({
                "sku": v.get("sku") or f"{prod.get('id')}-{v.get('id')}",
                "spu": str(prod.get("id")),
                "title": prod.get("title"),
                "description": prod.get("body_html"),
                "image_urls": images,
                "category_path": prod.get("product_type") or None,
                "sale_price": sale,
                "original_price": compare or sale,
                "currency": None,                       # products.json 不含币种
                "variant_id": str(v.get("id")),
                "attributes": attrs or None,
                "status": "on_sale" if v.get("available") else "out_of_stock",
                "inventory": v.get("inventory_quantity"),
                "label": label,
                "tags": prod.get("tags") or None,
                "product_url": product_url,
                "product_type": prod.get("product_type"),
                "weight": f"{v.get('grams')}g" if v.get("grams") else None,
                "published_at": prod.get("published_at"),
                "site": self.site.site,
                "brand": self.site.brand,
                "is_new": is_new,
                "is_bestseller": is_best,
            })
        return rows

    def _crawl_categories(self, sess: creq.Session) -> list[dict]:
        cats = []
        try:
            for page in range(1, 10):
                data = self._get_json(
                    sess, f"/collections.json?limit={PAGE_SIZE}&page={page}")
                items = data.get("collections", [])
                if not items:
                    break
                for c in items:
                    cats.append({
                        "site": self.site.site,
                        "category_id": str(c.get("id")),
                        "category_name": c.get("title"),
                        "category_url": self.site.url.rstrip("/")
                        + "/collections/" + c.get("handle", ""),
                        "parent_id": None,
                        "level": 1,
                        "product_count": c.get("products_count"),
                    })
                self.sleep()
        except Exception:
            pass
        return cats
