"""Shopify 采集器 —— SONGMICS 系站点。

SONGMICS 是标准 Shopify 站，公开 /products.json，无需浏览器 / 选择器：
  GET /products.json?limit=250&page=N      → 全量 SKU（翻页到空）
  GET /collections.json?limit=250&page=N   → 分类树
  GET /collections/new/products.json       → 新品
  GET /collections/top-picks/products.json → 热销品
"""
from __future__ import annotations

import json
import re

from selectolax.parser import HTMLParser

from .base import BaseCrawler, CrawlResult
from ..fetching import CrawlerFetcher, FetchResult

PAGE_SIZE = 250
MAX_PAGES = 80          # 250 * 80 = 2 万 SKU 上限保护


class ShopifyCrawler(BaseCrawler):
    platform = "shopify"

    def _headers(self) -> dict:
        """构造请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {
            "User-Agent": self.ua(),
            "Accept": "application/json",
        }

    def _get_json(self, fetcher: CrawlerFetcher, path: str) -> dict:
        url = self.site.url.rstrip("/") + path
        res = fetcher.get(url, headers=self._headers(), timeout=30)
        self.guard(res.status or 0, url)          # 熔断检查
        if not res.ok:
            raise RuntimeError(f"HTTP {res.status or 0} fetching {url}")
        self.snapshot(path, res.text)             # 原始响应归档
        return res.json() or {}

    def _handles(self, fetcher: CrawlerFetcher, collection: str) -> set[str]:
        """取某 collection 下全部商品 handle（用于打新品/热销标签）。"""
        handles: set[str] = set()
        try:
            for page in range(1, 10):
                data = self._get_json(
                    fetcher, f"/collections/{collection}/products.json"
                    f"?limit={PAGE_SIZE}&page={page}")
                items = data.get("products", [])
                if not items:
                    break
                handles.update(p["handle"] for p in items)
                self.sleep()
        except Exception:
            pass            # 站点无此 collection，忽略
        return handles

    def _collection_categories(self, fetcher: CrawlerFetcher) -> tuple[list[dict], dict[str, str]]:
        cats = []
        category_by_handle: dict[str, str] = {}
        try:
            for page in range(1, 10):
                data = self._get_json(
                    fetcher, f"/collections.json?limit={PAGE_SIZE}&page={page}")
                items = data.get("collections", [])
                if not items:
                    break
                for c in items:
                    handle = str(c.get("handle") or "").strip()
                    title = str(c.get("title") or "").strip()
                    if handle and title:
                        category_by_handle[handle] = title
                    cats.append({
                        "site": self.site.site,
                        "category_id": str(c.get("id")),
                        "category_name": c.get("title"),
                        "category_url": self.site.url.rstrip("/")
                        + "/collections/" + (c.get("handle") or ""),
                        "parent_id": None,
                        "level": 1,
                        "product_count": c.get("products_count"),
                    })
                self.sleep()
        except Exception:
            pass
        return cats, category_by_handle

    def _product_details(self, fetcher: CrawlerFetcher, handle: str) -> dict:
        if not handle:
            return {}
        url = self.site.url.rstrip("/") + "/products/" + handle
        try:
            res = fetcher.get(url, headers={
                "User-Agent": self.ua(),
                "Accept": "text/html,application/xhtml+xml",
            }, timeout=20)
        except Exception:
            return {}
        if not res.ok:
            return {}
        self.snapshot(f"pdp-{handle}", res.text)
        tree = HTMLParser(res.text or "")
        data = _extract_shopify_pdp_details(res.text or "", tree)
        data.setdefault("product_url", url)
        return data

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="product", source="shopify")

        new_handles = self._handles(fetcher, "new")
        best_handles = self._handles(fetcher, "top-picks") | self._handles(fetcher, "best-sellers")
        categories, category_by_handle = self._collection_categories(fetcher)
        result.notes.append(f"新品 collection {len(new_handles)} 款 / "
                             f"热销 collection {len(best_handles)} 款")

        # ---- 全量商品 ----
        seen_skus: set[str] = set()
        duplicate_skus = 0
        hit_page_cap = False
        fetched_raw_products = 0
        for page in range(1, MAX_PAGES + 1):
            data = self._get_json(fetcher, f"/products.json?limit={PAGE_SIZE}&page={page}")
            products = data.get("products", [])
            if not products:
                break
            fetched_raw_products += len(products)
            if page == MAX_PAGES:
                hit_page_cap = True
            for prod in products:
                details = self._product_details(fetcher, str(prod.get("handle") or ""))
                category = (
                    prod.get("product_type")
                    or details.get("category")
                    or _category_from_collections(prod, category_by_handle)
                    or _category_from_shopify_fallback(prod)
                )
                for row in self._expand(
                    prod,
                    new_handles,
                    best_handles,
                    details=details,
                    category=category,
                ):
                    sku = str(row.get("sku") or "").strip()
                    if sku and sku in seen_skus:
                        duplicate_skus += 1
                        continue
                    if sku:
                        seen_skus.add(sku)
                    result.products.append(row)
            self.sleep()

        # ---- 分类树 ----
        result.categories = categories
        result.total_product_count = len(result.products)
        if hit_page_cap:
            result.total_product_count = max(
                result.total_product_count,
                fetched_raw_products + 1,
            )
        if duplicate_skus:
            result.notes.append(f"Shopify feed 去重重复 SKU {duplicate_skus} 条")
        if hit_page_cap:
            result.coverage_complete = False
            result.coverage_code = "incomplete_discovery"
            result.coverage_stage = "products_json"
            result.coverage_reason = (
                f"Shopify /products.json 已打满 MAX_PAGES={MAX_PAGES}，"
                "未看到空页终止，无法证明已覆盖全量商品。"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = (
                "提高 Shopify MAX_PAGES 或按 collection/feed 分片后重跑。"
            )
        return result

    def _expand(
        self,
        prod: dict,
        new_handles: set,
        best_handles: set,
        *,
        details: dict | None = None,
        category: str | None = None,
    ) -> list[dict]:
        """一个 Shopify product 展开成「每变体一行」。"""
        rows = []
        details = details or {}
        handle = prod.get("handle", "")
        images = [img.get("src") for img in prod.get("images", []) if img.get("src")]
        if not images and details.get("images"):
            images = details["images"]
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
                "category_path": category or prod.get("product_type") or None,
                "sale_price": sale,
                "original_price": compare or sale,
                "currency": _country_to_currency(self.site.country),
                "variant_id": str(v.get("id")),
                "attributes": attrs or None,
                "ratings": details.get("rating"),
                "review_count": details.get("review_count"),
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

    def _crawl_categories(self, fetcher: CrawlerFetcher) -> list[dict]:
        cats = []
        try:
            for page in range(1, 10):
                data = self._get_json(
                    fetcher, f"/collections.json?limit={PAGE_SIZE}&page={page}")
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


def _country_to_currency(country: str | None) -> str | None:
    """按国别推断币种。Shopify products.json 不含 currency 字段。"""
    if not country:
        return None
    m = {
        "US": "USD", "CA": "CAD",
        "UK": "GBP", "GB": "GBP", "IE": "EUR",
        "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
        "NL": "EUR", "PT": "EUR", "BE": "EUR", "AT": "EUR",
        "PL": "PLN", "RO": "RON", "JP": "JPY", "AU": "AUD",
    }
    return m.get(country.upper())


def _category_from_collections(prod: dict, category_by_handle: dict[str, str]) -> str | None:
    handles: list[str] = []
    for key in ("collections", "collection_handles"):
        value = prod.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    handle = item.get("handle")
                else:
                    handle = item
                if handle:
                    handles.append(str(handle))
    for handle in handles:
        title = category_by_handle.get(handle)
        if title:
            return title
    return None


def _category_from_shopify_fallback(prod: dict) -> str | None:
    text = " ".join(
        str(value or "")
        for value in (
            prod.get("handle"),
            prod.get("title"),
            prod.get("tags"),
        )
    ).lower()
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    rules = (
        ("gift card", "Gift Cards"),
        ("geschenkkarte", "Gift Cards"),
        ("geschenkgutschein", "Gift Cards"),
        ("gift card", "Gift Cards"),
        ("recycling fee", "Fees"),
        ("retail delivery fee", "Fees"),
        ("fee", "Fees"),
        ("mattress", "Mattresses"),
        ("keyboard", "Keyboards"),
        ("christmas tree", "Christmas Trees"),
        ("weihnachtsbaum", "Christmas Trees"),
        ("árbol de navidad", "Christmas Trees"),
        ("arbol de navidad", "Christmas Trees"),
        ("albero di natale", "Christmas Trees"),
        ("canopy", "Canopies"),
        ("pavillon", "Canopies"),
        ("carpa", "Canopies"),
        ("chapiteau", "Canopies"),
        ("gazebo", "Canopies"),
        ("bookcase", "Bookcases"),
        ("bookshelf", "Bookcases"),
        ("bücherschrank", "Bookcases"),
        ("libreria", "Bookcases"),
        ("librería", "Bookcases"),
        ("bibliothèque", "Bookcases"),
        ("bibliotheque", "Bookcases"),
        ("kitchen", "Kitchen"),
        ("cocina", "Kitchen"),
        ("cuisine", "Kitchen"),
        ("cucina", "Kitchen"),
        ("escurreplatos", "Kitchen"),
        ("égouttoir", "Kitchen"),
        ("egouttoir", "Kitchen"),
        ("grill", "Garden & Outdoor/Grills"),
        ("barbacoa", "Garden & Outdoor/Grills"),
        ("barbecue", "Garden & Outdoor/Grills"),
        ("gewächshaus", "Garden & Outdoor/Greenhouses"),
        ("gewaechshaus", "Garden & Outdoor/Greenhouses"),
        ("treibhaus", "Garden & Outdoor/Greenhouses"),
        ("greenhouse", "Garden & Outdoor/Greenhouses"),
        ("invernadero", "Garden & Outdoor/Greenhouses"),
        ("serre", "Garden & Outdoor/Greenhouses"),
        ("voile d'ombrage", "Garden & Outdoor/Shade Sails"),
        ("voile dombrage", "Garden & Outdoor/Shade Sails"),
        ("tenda a vela", "Garden & Outdoor/Shade Sails"),
        ("privacy screen", "Garden & Outdoor/Privacy Screens"),
        ("pantalla de privacidad", "Garden & Outdoor/Privacy Screens"),
        ("brise-vue", "Garden & Outdoor/Privacy Screens"),
        ("frangivista", "Garden & Outdoor/Privacy Screens"),
        ("canisse", "Garden & Outdoor/Privacy Screens"),
        ("palissade", "Garden & Outdoor/Privacy Screens"),
        ("valla", "Garden & Outdoor/Privacy Screens"),
        ("hammock", "Garden & Outdoor/Hammocks"),
        ("amaca", "Garden & Outdoor/Hammocks"),
        ("unkraut", "Garden & Outdoor/Garden Supplies"),
        ("garten", "Garden & Outdoor"),
        ("garden", "Garden & Outdoor"),
        ("jardin", "Garden & Outdoor"),
        ("jardín", "Garden & Outdoor"),
        ("giardino", "Garden & Outdoor"),
        ("outdoor", "Garden & Outdoor"),
        ("exterior", "Garden & Outdoor"),
        ("picknicktisch", "Garden & Outdoor/Tables"),
        ("balkon", "Garden & Outdoor/Tables"),
        ("balcon", "Garden & Outdoor/Tables"),
        ("balcón", "Garden & Outdoor/Tables"),
        ("schutzhülle", "Garden & Outdoor/Covers"),
        ("schutzhuelle", "Garden & Outdoor/Covers"),
        ("abdeckung", "Garden & Outdoor/Covers"),
        ("abdeck", "Garden & Outdoor/Covers"),
        ("cover", "Garden & Outdoor/Covers"),
        ("funda", "Garden & Outdoor/Covers"),
        ("cubierta", "Garden & Outdoor/Covers"),
        ("housse", "Garden & Outdoor/Covers"),
        ("copertura", "Garden & Outdoor/Covers"),
        ("parasol", "Garden & Outdoor/Umbrellas"),
        ("umbrella", "Garden & Outdoor/Umbrellas"),
        ("sombrilla", "Garden & Outdoor/Umbrellas"),
        ("briefkasten", "Outdoor Mailboxes"),
        ("mailbox", "Outdoor Mailboxes"),
        ("buzon", "Outdoor Mailboxes"),
        ("buzón", "Outdoor Mailboxes"),
        ("boîte aux lettres", "Outdoor Mailboxes"),
        ("boite aux lettres", "Outdoor Mailboxes"),
        ("cassetta postale", "Outdoor Mailboxes"),
        ("cassetta della posta", "Outdoor Mailboxes"),
        ("cassetta delle lettere", "Outdoor Mailboxes"),
        ("nachtlicht", "Lighting"),
        ("standleuchte", "Lighting"),
        ("veilleuse", "Lighting"),
        ("spieluhr", "Home Decor/Music Boxes"),
        ("music box", "Home Decor/Music Boxes"),
        ("caja de musica", "Home Decor/Music Boxes"),
        ("caja de música", "Home Decor/Music Boxes"),
        ("boîte à musique", "Home Decor/Music Boxes"),
        ("boite a musique", "Home Decor/Music Boxes"),
        ("carillon", "Home Decor/Music Boxes"),
        ("ventilator", "Fans"),
        ("badezimmer", "Bathroom"),
        ("waschbeckenunterschrank", "Bathroom"),
        ("bathroom", "Bathroom"),
        ("baño", "Bathroom"),
        ("bano", "Bathroom"),
        ("salle de bain", "Bathroom"),
        ("bagno", "Bathroom"),
        ("lavabo", "Bathroom"),
        ("shower", "Bathroom"),
        ("doccia", "Bathroom"),
        ("douche", "Bathroom"),
        ("schrank", "Storage Furniture"),
        ("kommode", "Storage Furniture"),
        ("cabinet", "Storage Furniture"),
        ("armario", "Storage Furniture"),
        ("armoire", "Storage Furniture"),
        ("armadietto", "Storage Furniture"),
        ("credenza", "Storage Furniture"),
        ("chest of", "Storage Furniture"),
        ("schuhregal", "Storage Furniture"),
        ("shoe rack", "Entryway/Shoe Storage"),
        ("shoe bench", "Entryway/Shoe Storage"),
        ("folding storage ottoman bench", "Furniture/Storage & Seating"),
        ("shoe boxes", "Entryway/Shoe Storage"),
        ("shoe cabinet", "Entryway/Shoe Storage"),
        ("zapatero", "Entryway/Shoe Storage"),
        ("zapatos", "Entryway/Shoe Storage"),
        ("chaussure", "Entryway/Shoe Storage"),
        ("scarpe", "Entryway/Shoe Storage"),
        ("scarpiera", "Entryway/Shoe Storage"),
        ("organizer", "Storage & Organization"),
        ("storage", "Storage & Organization"),
        ("rangement", "Storage & Organization"),
        ("sous vide", "Storage & Organization"),
        ("vacío", "Storage & Organization"),
        ("vacio", "Storage & Organization"),
        ("sottovuoto", "Storage & Organization"),
        ("organizador", "Storage & Organization"),
        ("organisateur", "Storage & Organization"),
        ("organiseurs", "Storage & Organization"),
        ("portaoggetti", "Storage & Organization"),
        ("shelf", "Storage & Organization"),
        ("estante", "Storage & Organization"),
        ("etagere", "Storage & Organization"),
        ("étagère", "Storage & Organization"),
        ("scaffale", "Storage & Organization"),
        ("scaffalatura", "Storage & Organization"),
        ("basket", "Storage & Organization"),
        ("cesta", "Storage & Organization"),
        ("panier", "Storage & Organization"),
        ("cesto", "Storage & Organization"),
        ("spice rack", "Storage & Organization"),
        ("divisori per cassetti", "Storage & Organization"),
        ("separadores de cajones", "Storage & Organization"),
        ("séparateurs de tiroirs", "Storage & Organization"),
        ("separateurs de tiroirs", "Storage & Organization"),
        ("roulettes de rechange", "Storage & Organization"),
        ("desserte", "Storage & Organization"),
        ("carrello", "Storage & Organization"),
        ("treteimer", "Trash Cans"),
        ("trash can", "Trash Cans"),
        ("garbage can", "Trash Cans"),
        ("rubbish bin", "Trash Cans"),
        ("dustbin", "Trash Cans"),
        ("waste bin", "Trash Cans"),
        ("basura", "Trash Cans"),
        ("poubelle", "Trash Cans"),
        ("pattumiera", "Trash Cans"),
        ("cestino", "Trash Cans"),
        ("recycling bin", "Trash Cans"),
        ("spiegel", "Mirrors"),
        ("mirror", "Mirrors"),
        ("espejo", "Mirrors"),
        ("miroir", "Mirrors"),
        ("specchio", "Mirrors"),
        ("schmuck", "Jewelry Storage"),
        ("jewelry", "Jewelry Storage"),
        ("jewellery", "Jewelry Storage"),
        ("joyero", "Jewelry Storage"),
        ("joyas", "Jewelry Storage"),
        ("bijoux", "Jewelry Storage"),
        ("gioielli", "Jewelry Storage"),
        ("portagioie", "Jewelry Storage"),
        ("bilderrahmen", "Home Decor/Frames"),
        ("picture frame", "Home Decor/Frames"),
        ("photo frame", "Home Decor/Frames"),
        ("artwork frame", "Home Decor/Frames"),
        ("marco de fotos", "Home Decor/Frames"),
        ("cadre photo", "Home Decor/Frames"),
        ("cornice", "Home Decor/Frames"),
        ("künstliche", "Home Decor/Artificial Plants"),
        ("kuenstliche", "Home Decor/Artificial Plants"),
        ("sukkulenten", "Home Decor/Artificial Plants"),
        ("artificial plant", "Home Decor/Artificial Plants"),
        ("plantas suculentas artificiales", "Home Decor/Artificial Plants"),
        ("plantes grasses artificielles", "Home Decor/Artificial Plants"),
        ("rideaux", "Home Decor/Curtains"),
        ("curtain", "Home Decor/Curtains"),
        ("makeup", "Beauty Storage"),
        ("make-up", "Beauty Storage"),
        ("maquillaje", "Beauty Storage"),
        ("maquillage", "Beauty Storage"),
        ("barhocker", "Furniture/Chairs & Seating"),
        ("hocker", "Furniture/Chairs & Seating"),
        ("chair", "Furniture/Chairs & Seating"),
        ("stool", "Furniture/Chairs & Seating"),
        ("bench", "Furniture/Chairs & Seating"),
        ("ottoman", "Furniture/Chairs & Seating"),
        ("couch", "Furniture/Chairs & Seating"),
        ("sofa", "Furniture/Chairs & Seating"),
        ("silla", "Furniture/Chairs & Seating"),
        ("taburete", "Furniture/Chairs & Seating"),
        ("puff", "Furniture/Chairs & Seating"),
        ("chaise", "Furniture/Chairs & Seating"),
        ("fauteuil", "Furniture/Chairs & Seating"),
        ("tabouret", "Furniture/Chairs & Seating"),
        ("pouf", "Furniture/Chairs & Seating"),
        ("sedia", "Furniture/Chairs & Seating"),
        ("sedie", "Furniture/Chairs & Seating"),
        ("sgabelli", "Furniture/Chairs & Seating"),
        ("sgabello", "Furniture/Chairs & Seating"),
        ("panchina", "Furniture/Chairs & Seating"),
        ("poltrona", "Furniture/Chairs & Seating"),
        ("bettgestell", "Furniture/Bedroom"),
        ("bett", "Furniture/Bedroom"),
        ("bed frame", "Furniture/Bedroom"),
        ("bedroom", "Furniture/Bedroom"),
        ("dresser", "Furniture/Bedroom"),
        ("nightstand", "Furniture/Bedroom"),
        ("cama", "Furniture/Bedroom"),
        ("dormitorio", "Furniture/Bedroom"),
        ("mesita de noche", "Furniture/Bedroom"),
        ("chambre", "Furniture/Bedroom"),
        ("letto", "Furniture/Bedroom"),
        ("comodino", "Furniture/Bedroom"),
        ("tv stand", "Furniture/TV Stands"),
        ("av component stand", "Furniture/TV Stands"),
        ("component stand", "Furniture/TV Stands"),
        ("media console", "Furniture/TV Stands"),
        ("soporte para tv", "Furniture/TV Stands"),
        ("meuble tv", "Furniture/TV Stands"),
        ("mobile tv", "Furniture/TV Stands"),
        ("desk", "Office Furniture"),
        ("office", "Office Furniture"),
        ("file cabinet", "Office Furniture"),
        ("filing cabinet", "Office Furniture"),
        ("printer stand", "Office Furniture"),
        ("soporte de impresora", "Office Furniture"),
        ("archivador", "Office Furniture"),
        ("cajonera", "Office Furniture"),
        ("bureau", "Office Furniture"),
        ("ufficio", "Office Furniture"),
        ("scrivania", "Office Furniture"),
        ("table", "Furniture/Tables"),
        ("mesa", "Furniture/Tables"),
        ("tavolo", "Furniture/Tables"),
        ("tavolino", "Furniture/Tables"),
        ("kopfkissen", "Bedding"),
        ("pillow", "Bedding"),
        ("almohada", "Bedding"),
        ("oreiller", "Bedding"),
        ("cuscino", "Bedding"),
        ("edredón", "Bedding"),
        ("edredon", "Bedding"),
        ("koffer", "Luggage"),
        ("walizka", "Luggage"),
        ("luggage", "Luggage"),
        ("suitcase", "Luggage"),
        ("valise", "Luggage"),
        ("maleta", "Luggage"),
        ("valigia", "Luggage"),
        ("schaufensterpuppe", "Mannequins"),
        ("schneiderpuppe", "Mannequins"),
        ("puppe", "Mannequins"),
        ("mannequin", "Mannequins"),
        ("manichino", "Mannequins"),
        ("fahrrad", "Sports & Fitness"),
        ("liegestütz", "Sports & Fitness"),
        ("liegestuetz", "Sports & Fitness"),
        ("hantelbank", "Sports & Fitness"),
        ("push up", "Sports & Fitness"),
        ("flexiones", "Sports & Fitness"),
        ("pompe", "Sports & Fitness"),
        ("musculation", "Sports & Fitness"),
        ("mancuerna", "Sports & Fitness"),
        ("dumbbell", "Sports & Fitness"),
        ("fitness", "Sports & Fitness"),
        ("bicicleta", "Sports & Fitness"),
        ("cyclette", "Sports & Fitness"),
        ("fútbol", "Sports & Fitness"),
        ("futbol", "Sports & Fitness"),
        ("calcio", "Sports & Fitness"),
        ("feandrea", "Pet Supplies"),
        ("litter box", "Pet Supplies"),
        ("playpen", "Pet Supplies"),
        ("mascota", "Pet Supplies"),
        ("gato", "Pet Supplies"),
        ("perro", "Pet Supplies"),
        ("chien", "Pet Supplies"),
        ("gatti", "Pet Supplies"),
        ("gatto", "Pet Supplies"),
        ("animaux", "Pet Supplies"),
        ("lettiera", "Pet Supplies"),
        ("schlafsack", "Camping"),
        ("camping bed", "Camping"),
        ("camping cot", "Camping"),
        ("zelt", "Camping"),
        ("tente de jeu", "Toys & Games"),
        ("tenda da gioco", "Toys & Games"),
        ("ladder", "Tools & Home Improvement"),
        ("escalera", "Tools & Home Improvement"),
        ("échelle", "Tools & Home Improvement"),
        ("echelle", "Tools & Home Improvement"),
        ("scaletta", "Tools & Home Improvement"),
        ("rug", "Rugs"),
        ("carpet", "Rugs"),
        ("alfombra", "Rugs"),
        ("tapis", "Rugs"),
        ("tappeto", "Rugs"),
    )
    for token, category in rules:
        if token in text:
            return category
    return None


def _extract_shopify_pdp_details(html: str, tree: HTMLParser) -> dict:
    data = _jsonld_product_details(html)
    hydration = _shopify_hydration_details(html)
    for key, value in hydration.items():
        if data.get(key) in (None, "", [], {}):
            data[key] = value
    images = [] if _product_json_declares_no_images(html) else _meta_images(tree)
    if images and not data.get("images"):
        data["images"] = images
    review_count, rating = _dom_review_details(html, tree)
    if data.get("review_count") is None:
        data["review_count"] = review_count
    if data.get("rating") is None:
        data["rating"] = rating
    category = data.get("category") or _dom_breadcrumb(tree)
    if category:
        data["category"] = category
    return data


def _product_json_declares_no_images(html: str) -> bool:
    if not html:
        return False
    return bool(re.search(
        r'"images"\s*:\s*\[\s*\]\s*,\s*"featured_image"\s*:\s*null',
        html,
        re.I,
    ))


def _meta_images(tree: HTMLParser) -> list[str]:
    images: list[str] = []
    for selector in (
        'meta[property="og:image"]',
        'meta[property="og:image:secure_url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ):
        node = tree.css_first(selector)
        if not node:
            continue
        value = (node.attributes.get("content") or "").strip()
        if value and value not in images:
            images.append(value)
    return images


def _jsonld_product_details(html: str) -> dict:
    out: dict = {}
    for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.S):
        try:
            doc = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk_jsonld(doc):
            if not isinstance(node, dict):
                continue
            types = _jsonld_types(node.get("@type"))
            if "product" not in types:
                continue
            rating = node.get("aggregateRating") or {}
            if isinstance(rating, dict):
                out.setdefault("rating", _num(rating.get("ratingValue")))
                out.setdefault("review_count", _int(
                    rating.get("reviewCount") or rating.get("ratingCount")))
            category = _named_value(node.get("category"))
            if category:
                out.setdefault("category", category)
            images = _image_urls(node.get("image"))
            if images:
                out.setdefault("images", images)
    return out


def _shopify_hydration_details(html: str) -> dict:
    out: dict = {}
    count_keys = (
        "okendoProductReviewCount",
        "productReviewCount",
        "MetafieldLooxCount",
        "MetafieldYotpoCount",
        "judgemeReviewCount",
    )
    count_pattern = (
        rf"(?:{'|'.join(re.escape(key) for key in count_keys)})"
        r"['\"]?\s*[:=]\s*['\"]?([\d,]+)"
    )
    for match in re.finditer(
            count_pattern,
            html,
            re.I):
        value = _int(match.group(1))
        if value is not None:
            out.setdefault("review_count", value)
            break
    rating_keys = (
        "okendoProductReviewAverageValue",
        "productReviewAverageValue",
        "MetafieldLooxRating",
        "MetafieldYotpoRating",
        "judgemeAverageRating",
    )
    rating_pattern = (
        rf"(?:{'|'.join(re.escape(key) for key in rating_keys)})"
        r"['\"]?\s*[:=]\s*['\"]?([\d.]+)"
    )
    for match in re.finditer(
            rating_pattern,
            html,
            re.I):
        value = _num(match.group(1))
        if value is not None:
            out.setdefault("rating", value)
            break
    return out


def _dom_review_details(html: str, tree: HTMLParser) -> tuple[int | None, float | None]:
    review_count = None
    rating = None
    selectors = (
        "[data-rating-count]",
        "[data-review-count]",
        "[data-reviews-count]",
        "[data-raters]",
        "[class*=review]",
        "[id*=review]",
        ".jdgm-prev-badge",
        ".loox-rating",
        ".yotpo",
        ".okeReviews",
    )
    for selector in selectors:
        for node in tree.css(selector)[:50]:
            for attr in (
                "data-rating-count",
                "data-review-count",
                "data-reviews-count",
                "data-number-of-reviews",
                "data-raters",
                "data-count",
                "aria-label",
                "title",
            ):
                value = node.attributes.get(attr)
                if value and review_count is None:
                    if attr.startswith("data-") and (
                            "count" in attr or "reviews" in attr or attr == "data-raters"):
                        review_count = _int(value)
                    else:
                        review_count = _review_count_from_text(value)
            for attr in (
                "data-rating",
                "data-average-rating",
                "data-score",
                "data-oke-star-rating",
            ):
                value = node.attributes.get(attr)
                if value and rating is None:
                    rating = _num(value)
            text = node.text(separator=" ", strip=True)
            if text and review_count is None:
                review_count = _review_count_from_text(text)
            if text and rating is None:
                rating = _rating_from_text(text)
            if review_count is not None and rating is not None:
                return review_count, rating
    return review_count, rating


def _dom_breadcrumb(tree: HTMLParser) -> str | None:
    crumbs = []
    for node in tree.css(
            ".breadcrumbs a, .breadcrumb a, [class*=breadcrumb] a, "
            "nav[aria-label*=breadcrumb] a, [itemtype*=BreadcrumbList] [itemprop=name]"):
        text = re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()
        if text and text.lower() not in {"home", "shop", "products"}:
            if text in crumbs:
                continue
            crumbs.append(text)
    if len(crumbs) > 1:
        return "/".join(crumbs[:-1][:4])
    if crumbs:
        return crumbs[0]
    return None


def _walk_jsonld(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_jsonld(item)
        return
    if not isinstance(value, dict):
        return
    yield value
    for key in ("@graph", "mainEntity", "itemListElement"):
        child = value.get(key)
        if key == "itemListElement" and isinstance(child, list):
            for item in child:
                if isinstance(item, dict) and "item" in item:
                    yield from _walk_jsonld(item.get("item"))
                yield from _walk_jsonld(item)
            continue
        yield from _walk_jsonld(child)


def _jsonld_types(value) -> set[str]:
    raw = value if isinstance(value, list) else [value]
    return {
        str(item).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
        for item in raw if item
    }


def _named_value(value) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, str):
        return value
    return None


def _image_urls(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        url = item.get("url") if isinstance(item, dict) else item
        if isinstance(url, str) and url and url not in out:
            out.append(url)
    return out


def _review_count_from_text(value: str) -> int | None:
    text = str(value or "")
    patterns = (
        r"([\d,]+)\s*(?:reviews?|ratings?|avis|bewertungen|reseñas|recensioni)",
        r"(?:reviews?|ratings?|avis|bewertungen|reseñas|recensioni)\D{0,20}([\d,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _int(match.group(1))
    return None


def _rating_from_text(value: str) -> float | None:
    match = re.search(r"([0-5](?:\.\d+)?)\s*(?:/|out of)\s*5", str(value or ""), re.I)
    return _num(match.group(1)) if match else None


def _int(value) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d[\d,\s]*", str(value))
    if not match:
        return None
    try:
        return int(match.group().replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None
