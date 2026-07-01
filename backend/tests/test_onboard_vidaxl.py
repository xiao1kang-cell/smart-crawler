"""TDD test: vidaxl crawler 批C 收编验证。

验证三路径计数：
- 路径1 API curl 路径: make_fetcher().get() → api_calls += 1 per page
- stealth 兜底路径 _fetch_via_stealth: count_browser_fetch 包裹 → browser_opens += 1
- stealth 失败不计 browser_opens

批C 收编规则（vidaxl 特殊）：
- API curl 段：make_fetcher(kind="api", source="vidaxl").get() 替代 sess.get()
  字段映射: res.status → resp.status_code / res.text → resp.text / res.json() → resp.json()
- storefront sitemap 段：make_fetcher(kind="sitemap", source="vidaxl").get() 替代 sess.get()
- stealth 兜底段：StealthyFetcher.fetch 用 count_browser_fetch 包裹；kw/profile 不动
- 多路径决策(API vs storefront) + proxy precheck 逻辑保留不动
- _try_fetch 中 proxy_pool 手动管理保留；成功时 self.counter.api_calls += 1
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_API_PRODUCT = {
    "sku": "VX123",
    "title": "Vidaxl Test Chair",
    "description": "A nice chair",
    "images": ["https://cdn.vidaxl.com/chair.jpg"],
    "category": "Furniture",
    "price": "99.99",
    "srp": "129.99",
    "currency": "EUR",
    "ean": "5059340100000",
    "stock": 10,
    "brand": "vidaXL",
    "url": "https://www.vidaxl.nl/e/vidaxl-chair/5059340100000.html",
}

_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.vidaxl.nl/sitemap-custom-product-1.xml</loc></sitemap>"
    "</sitemapindex>"
)

_SKU = "5059340100000"
_PDP_URL = "https://www.vidaxl.nl/e/vidaxl-chair/5059340100000.html"

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "vidaXL Chair",
    "sku": _SKU,
    "mpn": _SKU,
    "description": "A comfortable chair",
    "image": ["https://cdn.vidaxl.com/chair.jpg"],
    "brand": {"@type": "Brand", "name": "vidaXL"},
    "offers": {
        "@type": "Offer",
        "price": "99.99",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.2",
        "reviewCount": "87",
    },
}

_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    + "</head><body>Product page content</body></html>"
)

_JSONLD_ITEMPAGE = {
    "@context": "https://schema.org",
    "@type": "ItemPage",
    "name": "vidaXL Chair",
    "breadcrumb": {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "item": {
                    "@type": "WebPage",
                    "@id": "https://www.vidaxl.nl",
                    "name": "Frontpage",
                },
            },
            {
                "@type": "ListItem",
                "position": 2,
                "item": {
                    "@type": "CollectionPage",
                    "@id": "https://www.vidaxl.nl/g/436/meubelen",
                    "name": "Meubelen",
                },
            },
            {
                "@type": "ListItem",
                "position": 3,
                "item": {
                    "@type": "CollectionPage",
                    "@id": "https://www.vidaxl.nl/g/443/stoelen",
                    "name": "Stoelen",
                },
            },
            {
                "@type": "ListItem",
                "position": 4,
                "item": {
                    "@type": "ItemPage",
                    "@id": _PDP_URL,
                    "name": "vidaXL Chair",
                },
            },
        ],
    },
    "mainEntity": {**_JSONLD_PRODUCT, "category": "Stoelen"},
}

_PDP_HTML_ITEMPAGE = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_JSONLD_ITEMPAGE)
    + "</script>"
    + "</head><body>Product page content</body></html>"
)

_SITEMAP_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    f"<url><loc>{_PDP_URL}</loc></url>"
    "</urlset>"
)
_PDP_URL_2 = "https://www.vidaxl.nl/e/vidaxl-table/5059340100001.html"
_PDP_URL_3 = "https://www.vidaxl.nl/e/vidaxl-broken/5059340100002.html"
_SITEMAP_XML_THREE = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset>"
    f"<url><loc>{_PDP_URL}</loc></url>"
    f"<url><loc>{_PDP_URL_2}</loc></url>"
    f"<url><loc>{_PDP_URL_3}</loc></url>"
    "</urlset>"
)
_CATEGORY_URL = "https://www.vidaxl.nl/g/6368/outdoor-seating"
_CATEGORY_AJAX_URL = (
    "https://www.vidaxl.nl/on/demandware.store/Sites-vidaxl-nl-Site/"
    "nl_NL/Search-ShowAjax?cgid=6368&srule=Default%20sort&start=0&sz=20"
)
_CATEGORY_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Outdoor Seating",
        "mainEntity": {
            "@type": "OfferCatalog",
            "numberOfItems": 3,
            "itemListElement": [],
        },
    })
    + "</script>"
    f'<div data-url="{_CATEGORY_AJAX_URL}"></div>'
    "</head></html>"
)
_CATEGORY_AJAX_HTML_1 = (
    f'<a href="{_PDP_URL}">Chair</a>'
    f'<a href="{_PDP_URL_2}">Table</a>'
    f'<a href="{_PDP_URL_3}">Bench</a>'
)


def _site(country: str = "NL", proxy_tier: str = "none") -> Site:
    s = Site()
    s.site = "vidaxl_nl"
    s.url = "https://www.vidaxl.nl"
    s.country = country
    s.proxy_tier = proxy_tier
    s.platform = "vidaxl"
    s.brand = "vidaXL"
    return s


def _api_site() -> Site:
    s = Site()
    s.site = "vidaxl_api"
    s.url = "https://www.vidaxl.nl"
    s.country = "NL"
    s.proxy_tier = "none"
    s.platform = "vidaxl"
    s.brand = "vidaXL"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls and dispatches url_map."""
    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )
    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Test 1: 路径1 API curl → make_fetcher().get() → api_calls per page
# ---------------------------------------------------------------------------

def test_vidaxl_api_path_counts_api_calls(monkeypatch):
    """路径1 官方 API：单页 → api_calls >= 1，解析出 product。"""
    monkeypatch.setenv("VIDAXL_API_EMAIL", "test@example.com")
    monkeypatch.setenv("VIDAXL_API_TOKEN", "testtoken")

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_api_site())

    api_response = FetchResult(
        ok=True,
        url="https://b2b.vidaxl.com/api_customer/products",
        status=200,
        text=json.dumps([_API_PRODUCT]),
        content=json.dumps([_API_PRODUCT]).encode(),
        final_url="https://b2b.vidaxl.com/api_customer/products",
        fetcher="curl_cffi",
    )
    # Override json() to return proper data
    api_response_data = [_API_PRODUCT]

    class _FakeAPIFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            res = FetchResult(
                ok=True,
                url=url,
                status=200,
                text=json.dumps(api_response_data),
                content=json.dumps(api_response_data).encode(),
                final_url=url,
                fetcher="curl_cffi",
            )
            return res

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _FakeAPIFetcher())
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert crawler.counter.api_calls >= 1, (
        f"Expected api_calls >= 1 for API path, got {crawler.counter.api_calls}. "
        f"Notes: {result.notes}"
    )
    assert len(result.products) >= 1, (
        f"Expected >= 1 product, got {len(result.products)}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == "VX123"
    assert p["title"] == "Vidaxl Test Chair"
    assert p["currency"] == "EUR"
    assert p["site"] == "vidaxl_api"
    assert result.total_product_count == 1


def test_vidaxl_feed_path_reads_local_csv(monkeypatch, tmp_path):
    """无 API 凭据时，VIDAXL_US_FEED_URL 可直接作为 US fallback 数据源。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    feed = tmp_path / "vidaxl_us.csv"
    feed.write_text(
        "sku,title,price,srp,currency,stock,image_url,category,url\n"
        "US123,vidaXL US Patio Chair,49.99,69.99,USD,12,"
        "https://cdn.example.com/us123.jpg,Patio,"
        "https://www.vidaxl.com/e/us123.html\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDAXL_US_FEED_URL", str(feed))

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(Site(
        site="vidaxl_us",
        brand="Vidaxl",
        country="US",
        url="https://www.vidaxl.com/",
        platform="vidaxl",
        proxy_tier="residential",
    ))

    result = crawler.crawl()

    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "US123"
    assert row["title"] == "vidaXL US Patio Chair"
    assert row["currency"] == "USD"
    assert row["sale_price"] == 49.99
    assert row["original_price"] == 69.99
    assert row["inventory"] == 12
    assert row["site"] == "vidaxl_us"
    assert "官方 Feed" in " ".join(result.notes)


def test_vidaxl_feed_path_reads_site_crawler_config(monkeypatch, tmp_path):
    """站点 crawler_config.feed_url 可作为后台配置的 vidaXL feed 入口。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    monkeypatch.delenv("VIDAXL_US_FEED_URL", raising=False)
    monkeypatch.delenv("VIDAXL_FEED_URL", raising=False)
    feed = tmp_path / "vidaxl_us_config.csv"
    feed.write_text(
        "ean,name,price,currency,quantity,image,category\n"
        "CFG123,Configured Feed Chair,39.50,USD,7,"
        "https://cdn.example.com/cfg123.jpg,Outdoor\n",
        encoding="utf-8",
    )

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(Site(
        site="vidaxl_us",
        brand="Vidaxl",
        country="US",
        url="https://www.vidaxl.com/",
        platform="vidaxl",
        proxy_tier="residential",
        crawler_config={"feed_url": str(feed)},
    ))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.products[0]["sku"] == "CFG123"
    assert result.products[0]["inventory"] == 7
    assert result.products[0]["category_path"] == "Outdoor"


def test_vidaxl_category_hint_fills_pdp_without_jsonld_category():
    """Category crawls must carry listing context into PDP rows."""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    url = "https://www.vidaxl.nl/e/vidaxl-chair/5059340100000.html"
    listing_html = """
    <html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"CollectionPage","name":"Garden Chairs"}
      </script>
    </head></html>
    """
    row = {"site": "vidaxl_nl", "sku": "5059340100000", "category_path": None}

    crawler._remember_category_hint(url, "https://www.vidaxl.nl/g/6368/garden-chairs", listing_html)
    crawler._apply_category_hint(row, url)

    assert row["category_path"] == "Garden Chairs"


def test_vidaxl_pdp_collects_html_promotion_labels():
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    html = _PDP_HTML.replace(
        "</body>",
        """
        <div class="shipping-message">Free delivery on this item</div>
        <div class="deal-badge">Bundle deal save 15%</div>
        </body>
        """,
    )

    row = crawler._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["has_free_shipping"] is True
    assert row["attributes"]["free_shipping_label"] == "Free shipping"
    assert any("Bundle deal save 15%" in label for label in row["attributes"]["promotions"])


def test_vidaxl_pdp_collects_portuguese_free_shipping_labels():
    from app.crawlers.vidaxl import VidaxlCrawler

    site = _site(country="PT")
    site.site = "vidaxl_pt"
    site.url = "https://www.vidaxl.pt"
    crawler = VidaxlCrawler(site)
    html = _PDP_HTML.replace(
        "</body>",
        """
        <div class="usp-message">Envio grátis desde 80 € (PT continental)</div>
        <div class="delivery-message">Entrega grátis em todos os produtos</div>
        </body>
        """,
    )

    row = crawler._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["has_free_shipping"] is True
    assert row["attributes"]["free_shipping_label"] == "Free shipping"
    assert any("Envio grátis" in label for label in row["attributes"]["promotions"])


def test_vidaxl_breadcrumb_falls_back_to_collection_url_slug_when_name_missing():
    from app.crawlers.vidaxl import VidaxlCrawler

    site = _site(country="ES")
    site.site = "vidaxl_es"
    site.url = "https://www.vidaxl.es"
    crawler = VidaxlCrawler(site)
    itempage = {
        **_JSONLD_ITEMPAGE,
        "breadcrumb": {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "item": {
                        "@type": "WebPage",
                        "@id": "https://www.vidaxl.es",
                        "name": "Frontpage",
                    },
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "item": {
                        "@type": "CollectionPage",
                        "@id": "https://www.vidaxl.es/g/bunk-beds-nl",
                        "name": None,
                    },
                },
                {
                    "@type": "ListItem",
                    "position": 3,
                    "item": {
                        "@type": "ItemPage",
                        "@id": _PDP_URL,
                        "name": "vidaXL Litera",
                    },
                },
            ],
        },
        "mainEntity": {**_JSONLD_PRODUCT, "category": None},
    }
    html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(itempage)
        + "</script></head><body>Product page content</body></html>"
    )

    row = crawler._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["category_path"] == "Bunk Beds Nl"


def test_vidaxl_title_fallback_fills_product_only_breadcrumb_category():
    from app.crawlers.vidaxl import VidaxlCrawler

    product = {
        **_JSONLD_PRODUCT,
        "name": "vidaXL Colchão de espuma 90x200 cm 7 zonas branco",
        "category": None,
    }
    doc = {
        "@context": "https://schema.org",
        "@type": "ItemPage",
        "breadcrumb": {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "item": {
                        "@type": "WebPage",
                        "@id": "https://www.vidaxl.pt",
                        "name": "Frontpage",
                    },
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "item": {
                        "@type": "ItemPage",
                        "@id": _PDP_URL,
                        "name": product["name"],
                    },
                },
            ],
        },
        "mainEntity": product,
    }
    html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(doc)
        + "</script></head></html>"
    )

    row = VidaxlCrawler(_site(country="PT"))._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["category_path"] == "Mobiliário/Camas e acessórios/Colchões"


def test_vidaxl_title_fallback_fills_gabion_category():
    from app.crawlers.vidaxl import VidaxlCrawler

    product = {
        **_JSONLD_PRODUCT,
        "name": "vidaXL Cesto gabião com cobertura 200x50x150 cm ferro galvanizado",
        "category": None,
    }
    html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps({"@context": "https://schema.org", "@type": "Product", **product})
        + "</script></head></html>"
    )

    row = VidaxlCrawler(_site(country="PT"))._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["category_path"] == "Hardware/Cercas e barreiras/Gabiões"


@pytest.mark.parametrize(
    ("country", "title", "expected"),
    [
        (
            "DE",
            "vidaXL Outdoor-Sofagarnitur mit Kissen 5 pcs Natur und Creme",
            "Garden & Outdoor/Outdoor Furniture/Outdoor Sofas & Sets",
        ),
        (
            "ES",
            "vidaXL Tocador con LEDs y armario madera contrachapada roble ahumado",
            "Furniture/Bedroom/Vanities",
        ),
        (
            "FR",
            "vidaXL Coiffeuse avec LED Chêne fumé 74,5x40x141 cm",
            "Furniture/Bedroom/Vanities",
        ),
    ],
)
def test_vidaxl_title_fallback_fills_multilingual_categories(country, title, expected):
    from app.crawlers.vidaxl import VidaxlCrawler

    product = {**_JSONLD_PRODUCT, "name": title, "category": None}
    html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps({"@context": "https://schema.org", "@type": "Product", **product})
        + "</script></head></html>"
    )

    row = VidaxlCrawler(_site(country=country))._parse_jsonld(html, _PDP_URL)

    assert row is not None
    assert row["category_path"] == expected


def test_vidaxl_html_promotion_does_not_match_ideal_as_deal():
    from app.crawlers.vidaxl import _promotion_attributes_from_html

    attrs = _promotion_attributes_from_html(
        "Ideal para diferentes espaços interiores. Material ideal para uso diário."
    )

    assert attrs == {}


def test_vidaxl_feed_total_not_shrunk_by_limit(monkeypatch, tmp_path):
    """Feed 分母必须是完整去重商品数，不能被本次产出上限截断。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    feed = tmp_path / "vidaxl_limit.csv"
    feed.write_text(
        "sku,title,price,currency,stock\n"
        "A,Chair A,10.00,EUR,1\n"
        "B,Chair B,20.00,EUR,1\n",
        encoding="utf-8",
    )

    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(Site(
        site="vidaxl_nl",
        brand="Vidaxl",
        country="NL",
        url="https://www.vidaxl.nl/",
        platform="vidaxl",
        proxy_tier="none",
        crawler_config={"feed_url": str(feed)},
    ))
    crawler.limit = 1

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_detail_parse"


def test_vidaxl_storefront_total_uses_full_target_count(monkeypatch):
    """storefront 路径的总量是全量目标 URL，不是成功解析商品数。"""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    monkeypatch.delenv("VIDAXL_RUN_TARGET_LIMIT", raising=False)
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "vidaxl_nl", "max_products": 0}])

    from app.crawlers import vidaxl as vidaxl_mod
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    url_map = {
        "https://www.vidaxl.nl/sitemap_index.xml": FetchResult(
            ok=True, url="https://www.vidaxl.nl/sitemap_index.xml",
            status=200, text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(),
            final_url="https://www.vidaxl.nl/sitemap_index.xml",
            fetcher="curl_cffi",
        ),
        "https://www.vidaxl.nl/sitemap-custom-product-1.xml": FetchResult(
            ok=True, url="https://www.vidaxl.nl/sitemap-custom-product-1.xml",
            status=200, text=_SITEMAP_XML_THREE,
            content=_SITEMAP_XML_THREE.encode(),
            final_url="https://www.vidaxl.nl/sitemap-custom-product-1.xml",
            fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(vidaxl_mod, "_persist_sitemap_total",
                        lambda site, total: None)
    registered = []
    monkeypatch.setattr(vidaxl_mod, "_register_frontier_targets",
                        lambda site, urls: registered.extend(urls))
    monkeypatch.setattr(vidaxl_mod, "_log_fetched", lambda *args, **kw: None)

    class _FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

    class _FakeSession:
        proxies = None

        def get(self, url: str, timeout: int = 30):
            if url == _PDP_URL_3:
                return _FakeResponse(200, "<html>No JSON-LD</html>")
            product = dict(_JSONLD_PRODUCT)
            product["sku"] = "5059340100001" if url == _PDP_URL_2 else _SKU
            product["mpn"] = product["sku"]
            html = (
                "<html><head><script type=\"application/ld+json\">"
                + json.dumps(product)
                + "</script></head></html>"
            )
            return _FakeResponse(200, html)

    monkeypatch.setattr(vidaxl_mod.creq, "Session",
                        lambda impersonate=None: _FakeSession())
    import app.proxy_pool as proxy_pool
    monkeypatch.setattr(proxy_pool, "get_proxy", lambda *args, **kw: None)
    monkeypatch.setattr(proxy_pool, "report_success", lambda *args, **kw: None)
    monkeypatch.setattr(proxy_pool, "report_failure", lambda *args, **kw: None)

    result = crawler.crawl()

    assert registered == [_PDP_URL, _PDP_URL_2, _PDP_URL_3]
    assert result.total_product_count == 3
    assert len(result.products) == 2
    assert any("本次全量分母 3" in note for note in result.notes)


def test_vidaxl_storefront_category_urls_skip_full_sitemap(monkeypatch):
    """category_urls configured means storefront only discovers that category."""
    monkeypatch.delenv("VIDAXL_API_EMAIL", raising=False)
    monkeypatch.delenv("VIDAXL_API_TOKEN", raising=False)
    monkeypatch.delenv("VIDAXL_RUN_TARGET_LIMIT", raising=False)
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "vidaxl_nl", "max_products": 0}])

    from app.crawlers import vidaxl as vidaxl_mod
    from app.crawlers.vidaxl import VidaxlCrawler

    site = _site()
    site.crawler_config = {
        "category_urls": [_CATEGORY_URL],
        "category_page_size": 20,
        "storefront_concurrency": 8,
    }
    crawler = VidaxlCrawler(site)
    ajax_url_1 = VidaxlCrawler._ajax_page_url(
        _CATEGORY_AJAX_URL,
        start=0,
        size=20,
    )
    url_map = {
        _CATEGORY_URL: FetchResult(
            ok=True, url=_CATEGORY_URL, status=200,
            text=_CATEGORY_HTML, content=_CATEGORY_HTML.encode(),
            final_url=_CATEGORY_URL, fetcher="curl_cffi",
        ),
        ajax_url_1: FetchResult(
            ok=True, url=ajax_url_1, status=200,
            text=_CATEGORY_AJAX_HTML_1,
            content=_CATEGORY_AJAX_HTML_1.encode(),
            final_url=ajax_url_1, fetcher="curl_cffi",
        ),
    }
    requested: list[str] = []

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            requested.append(url)
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(vidaxl_mod, "_persist_sitemap_total",
                        lambda site, total: None)
    registered = []
    monkeypatch.setattr(vidaxl_mod, "_register_frontier_targets",
                        lambda site, urls: registered.extend(urls))
    monkeypatch.setattr(vidaxl_mod, "_log_fetched", lambda *args, **kw: None)

    class _FakeResponse:
        status_code = 200

        def __init__(self, url: str):
            if url in url_map:
                item = url_map[url]
                self.status_code = item.status or 0
                self.text = item.text
                return
            product = dict(_JSONLD_PRODUCT)
            product["sku"] = url.rstrip("/").split("/")[-1].replace(".html", "")
            product["mpn"] = product["sku"]
            self.text = (
                "<html><head><script type=\"application/ld+json\">"
                + json.dumps(product)
                + "</script></head></html>"
            )

    class _FakeSession:
        proxies = None

        def get(self, url: str, timeout: int = 30):
            return _FakeResponse(url)

    monkeypatch.setattr(vidaxl_mod.creq, "Session",
                        lambda impersonate=None: _FakeSession())
    import app.proxy_pool as proxy_pool
    monkeypatch.setattr(proxy_pool, "get_proxy", lambda *args, **kw: None)
    monkeypatch.setattr(proxy_pool, "report_success", lambda *args, **kw: None)
    monkeypatch.setattr(proxy_pool, "report_failure", lambda *args, **kw: None)

    result = crawler.crawl()

    assert "https://www.vidaxl.nl/sitemap_index.xml" not in requested
    assert registered == []
    assert result.total_product_count == 3
    assert len(result.products) == 3
    assert any("storefront/category stream" in note for note in result.notes)


def test_vidaxl_category_grid_uses_visible_href_page_size(monkeypatch):
    """Grid discovery estimates pages from visible product cards, not hidden data-url links."""
    monkeypatch.delenv("VIDAXL_CATEGORY_DISCOVERY_CONCURRENCY", raising=False)

    from app.crawlers.vidaxl import VidaxlCrawler

    site = _site()
    site.crawler_config = {
        "category_grid_page_size": 20,
        "category_discovery_concurrency": 1,
        "category_rate_interval_sec": 0,
    }
    crawler = VidaxlCrawler(site)
    grid_url = (
        "https://www.vidaxl.nl/on/demandware.store/Sites-vidaxl-nl-Site/"
        "nl_NL/Search-UpdateGrid?cgid=436&page=1"
    )

    def grid_html(page: int, *, hidden: int = 0) -> str:
        visible = "".join(
            f'<a href="/e/visible-{page}-{idx}/10{page:02d}{idx:02d}.html">P</a>'
            for idx in range(20)
        )
        hidden_links = "".join(
            f'<button data-url="/e/hidden-{page}-{idx}/20{page:02d}{idx:02d}.html"></button>'
            for idx in range(hidden)
        )
        return visible + hidden_links

    first_html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps({
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "mainEntity": {
                "@type": "OfferCatalog",
                "numberOfItems": 100,
            },
        })
        + "</script>"
        '<a href="/on/demandware.store/Sites-vidaxl-nl-Site/nl_NL/'
        'Search-UpdateGrid?cgid=436&page=3">3</a>'
        "</head></html>"
    )
    pages = {page: grid_html(page, hidden=60 if page == 1 else 0)
             for page in range(1, 6)}
    requested_pages: list[int] = []

    def fake_fetch(_session, url: str, *, timeout: int = 40):
        page = int(url.rsplit("page=", 1)[1])
        requested_pages.append(page)
        return 200, pages.get(page, "")

    monkeypatch.setattr(crawler, "_fetch_category_listing_html", fake_fetch)
    monkeypatch.setattr(crawler, "_category_listing_session", lambda: object())

    seen: set[str] = set()
    targets: list[str] = []
    found = crawler._collect_category_grid_targets(
        object(), grid_url, first_html, _CATEGORY_URL, seen, targets)

    assert requested_pages == [1, 2, 3, 4, 5]
    assert found == 100
    assert len(targets) == 100
    assert all("/visible-" in url for url in targets)
    assert not any("/hidden-" in url for url in targets)


# ---------------------------------------------------------------------------
# Test 2: _map_api 直接单元测试（解析不退化）
# ---------------------------------------------------------------------------

def test_vidaxl_map_api_parse():
    """_map_api 直接解析 API product dict，确认字段不退化。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_api_site())
    row = crawler._map_api(_API_PRODUCT)

    assert row is not None
    assert row["sku"] == "VX123"
    assert row["title"] == "Vidaxl Test Chair"
    assert row["gtin"] == "5059340100000"
    assert row["inventory"] == 10
    assert row["status"] == "on_sale"
    assert row["site"] == "vidaxl_api"


def test_vidaxl_feed_mapping_accepts_category_and_shipping_promo_aliases():
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    row = crawler._map_feed({
        "sku": "VX-FEED-1",
        "product_name": "Feed Chair",
        "category_name": "Garden|Patio Chairs",
        "price": "49.99",
        "rrp": "69.99",
        "currency": "EUR",
        "image_url": "https://cdn.vidaxl.com/feed-chair.jpg",
        "stock": "8",
        "delivery_label": "Free delivery over €50",
        "coupon_code": "GARDEN10",
        "campaign_name": "Garden bundle sale",
    })

    assert row is not None
    assert row["category_path"] == "Garden/Patio Chairs"
    assert row["has_free_shipping"] is True
    assert row["attributes"]["free_shipping"] is True
    assert row["attributes"]["coupon"] == "GARDEN10"
    assert "Garden bundle sale" in row["attributes"]["promotions"]


def test_vidaxl_api_mapping_accepts_nested_category_list():
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_api_site())
    row = crawler._map_api({
        "sku": "VX-API-2",
        "name": "API Table",
        "categories": [{"name": "Furniture"}, {"name": "Tables"}],
        "price": "89.99",
        "currency": "EUR",
        "stock": 3,
        "shipping_text": "Shipping included",
    })

    assert row is not None
    assert row["category_path"] == "Furniture/Tables"
    assert row["has_free_shipping"] is True


# ---------------------------------------------------------------------------
# Test 3: _parse_jsonld 直接单元测试（解析不退化）
# ---------------------------------------------------------------------------

def test_vidaxl_parse_jsonld():
    """_parse_jsonld 对合法 Product JSON-LD 正确解析。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    row = crawler._parse_jsonld(_PDP_HTML, _PDP_URL)

    assert row is not None, "_parse_jsonld should not return None for valid Product JSON-LD"
    assert row["sku"] == _SKU
    assert row["title"] == "vidaXL Chair"
    assert row["sale_price"] == 99.99
    assert row["currency"] == "EUR"
    assert row["status"] == "on_sale"
    assert row["site"] == "vidaxl_nl"
    assert row["product_url"] == _PDP_URL


def test_vidaxl_parse_jsonld_reads_itempage_breadcrumb():
    """Vidaxl storefront PDP uses ItemPage.breadcrumb for category_path."""
    from app.crawlers.vidaxl import VidaxlCrawler

    row = VidaxlCrawler(_site())._parse_jsonld(_PDP_HTML_ITEMPAGE, _PDP_URL)

    assert row is not None
    assert row["category_path"] == "Meubelen/Stoelen"


# ---------------------------------------------------------------------------
# Test 4: stealth 兜底 _fetch_via_stealth 成功 → browser_opens += 1
# ---------------------------------------------------------------------------

def test_vidaxl_stealth_success_counts_browser_opens(monkeypatch):
    """_fetch_via_stealth 成功(status=200)时，browser_opens 增 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    验证计数路径完整。
    """
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())

    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    def _fake_fetch(url, **kw):
        return _FakePage()

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _fake_fetch(url, **kw)

    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    assert crawler.counter.browser_opens == 0

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html == _PDP_HTML


# ---------------------------------------------------------------------------
# Test 5: stealth 兜底失败(status=403) → browser_opens 不增
# ---------------------------------------------------------------------------

def test_vidaxl_stealth_failure_does_not_count(monkeypatch):
    """_fetch_via_stealth 失败(status=403)时，browser_opens 保持 0。"""
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageBlocked()

    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    html = crawler._fetch_via_stealth(_PDP_URL)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure, "
        f"got {crawler.counter.browser_opens}"
    )
    assert html is None


def test_vidaxl_failed_product_retry_reuses_storefront_pdp_executor(monkeypatch):
    """失败商品重抓只抓传入 PDP URL，不触发 sitemap 发现。"""
    from app.crawlers import vidaxl as vidaxl_mod
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    logged: list[tuple[str, int]] = []

    monkeypatch.setattr(
        crawler,
        "_try_fetch_storefront_pdp",
        lambda url: vidaxl_mod._PdpFetchResult(200, _PDP_HTML),
    )
    monkeypatch.setattr(
        vidaxl_mod,
        "_log_fetched",
        lambda site, url, status_code, **kw: logged.append((url, status_code)),
    )

    result = crawler.crawl_failed_products([_PDP_URL])

    assert len(result.products) == 1
    assert result.products[0]["sku"] == _SKU
    assert crawler.counter.api_calls == 1
    assert logged == [(_PDP_URL, 200)]
    assert "失败商品重抓" in result.notes[-1]


def test_vidaxl_failed_product_retry_excludes_404_from_total(monkeypatch):
    """404 PDPs are stale sitemap URLs and should not keep retries partial."""
    from app.crawlers import vidaxl as vidaxl_mod
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site())
    gone_url = "https://www.vidaxl.nl/e/gone/5059340199999.html"
    logged: list[tuple[str, int, bool]] = []

    def fake_fetch(url: str):
        if url == gone_url:
            return vidaxl_mod._PdpFetchResult(404, "")
        return vidaxl_mod._PdpFetchResult(200, _PDP_HTML)

    monkeypatch.setattr(crawler, "_try_fetch_storefront_pdp", fake_fetch)
    monkeypatch.setattr(
        vidaxl_mod,
        "_log_fetched",
        lambda site, url, status_code, **kw: logged.append(
            (url, status_code, bool(kw.get("skipped")))
        ),
    )

    result = crawler.crawl_failed_products([_PDP_URL, gone_url])

    assert len(result.products) == 1
    assert result.total_product_count == 1
    assert "404=1" in result.notes[-1]
    assert (gone_url, 404, True) in logged


def test_vidaxl_collection_page_is_redirected_non_product():
    from app.crawlers.vidaxl import VidaxlCrawler

    html = """
    <html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"CollectionPage",
       "name":"Garden Furniture","url":"https://www.vidaxl.ro/g/123/furniture"}
      </script>
    </head><body><h1>Garden Furniture</h1></body></html>
    """

    assert VidaxlCrawler._is_redirected_non_product_page(html) is True
    assert VidaxlCrawler(_site())._parse_jsonld(
        html,
        "https://www.vidaxl.ro/e/old-product/123.html",
    ) is None


def test_vidaxl_full_pdp_fetch_uses_proxy_lease(monkeypatch):
    """全站 PDP 抓取的单次请求必须租用并释放代理。"""
    from app import proxy_pool
    from app.crawlers.vidaxl import VidaxlCrawler

    crawler = VidaxlCrawler(_site(proxy_tier="residential"))
    lease_calls = []
    release_calls = []
    success_calls = []

    monkeypatch.setattr(
        proxy_pool,
        "lease_proxy",
        lambda tier, **kw: lease_calls.append((tier, kw)) or SimpleNamespace(
            url="http://proxy.local:3128",
            endpoint_id=1,
            lease_token="lease-token",
        ),
    )
    monkeypatch.setattr(
        proxy_pool,
        "release_proxy",
        lambda token, **kw: release_calls.append((token, kw)),
    )
    monkeypatch.setattr(
        proxy_pool,
        "report_success",
        lambda url: success_calls.append(url),
    )
    monkeypatch.setattr(proxy_pool, "report_failure", lambda *a, **kw: None)

    class _Resp:
        status_code = 200
        text = _PDP_HTML

    class _Session:
        def __init__(self, impersonate=None):
            self.proxies = {}

        def get(self, url, timeout):
            assert self.proxies == {
                "http": "http://proxy.local:3128",
                "https": "http://proxy.local:3128",
            }
            return _Resp()

    monkeypatch.setattr("app.crawlers.vidaxl.creq.Session", _Session)

    result = crawler._try_fetch_storefront_pdp(_PDP_URL)

    assert result.status == 200
    assert lease_calls[0][0] == "residential"
    assert lease_calls[0][1]["site"] == "vidaxl_nl"
    assert release_calls == [("lease-token", {"success": True, "failure_code": None})]
    assert success_calls == ["http://proxy.local:3128"]
