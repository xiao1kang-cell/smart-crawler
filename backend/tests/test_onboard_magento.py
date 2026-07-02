"""TDD test: verify magento crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Key cases:
  - sitemap discovery via robots.txt (text response)
  - sitemap .xml.gz (gzip content → res.content must be used)
  - product page with JSON-LD → product parsed out
  - counter.api_calls accumulated across all fetches
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture HTML / XML helpers
# ---------------------------------------------------------------------------

_PRODUCT_URL = "https://www.example.com/products/widget-pro.html"
_SITEMAP_URL = "https://www.example.com/sitemap.xml"
_SITEMAP_GZ_URL = "https://www.example.com/sitemap.xml.gz"

_ROBOTS_TXT = f"User-agent: *\nDisallow: /private\nSitemap: {_SITEMAP_URL}\n"

# Sitemap XML listing a single product URL
_SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_PRODUCT_URL}</loc></url>
</urlset>
"""

# Gzip-compressed sitemap for the .gz variant test
_SITEMAP_XML_GZ = gzip.compress(_SITEMAP_XML.encode("utf-8"))

_PRODUCT_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Widget Pro",
    "description": "A high-quality widget.",
    "image": ["https://www.example.com/images/widget-pro.jpg"],
    "brand": {"@type": "Brand", "name": "WidgetCo"},
    "offers": {
        "@type": "Offer",
        "price": "49.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "sku": "WP-001",
}

_PRODUCT_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD)
    + "</script>"
    "</head><body><h1>Widget Pro</h1></body></html>"
)

_PRODUCT_JSONLD_2 = {
    **_PRODUCT_JSONLD,
    "name": "Widget Mini",
    "sku": "WM-002",
    "offers": {
        "@type": "Offer",
        "price": "29.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
}
_PRODUCT_HTML_2 = (
    "<html><head>"
    '<script type="application/ld+json">'
    + json.dumps(_PRODUCT_JSONLD_2)
    + "</script>"
    "</head><body><h1>Widget Mini</h1></body></html>"
)
_CATEGORY_HTML = (
    "<html><head><title>Category</title></head>"
    "<body><h1>Category</h1></body></html>"
)


def _site() -> Site:
    s = Site()
    s.site = "example_magento"
    s.url = "https://www.example.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "magento"
    s.brand = "WidgetCo"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict[str, FetchResult]):
    """Return a fake fetcher whose .get() looks up url_map and increments counter."""

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            # Match by exact URL or prefix
            if url in url_map:
                return url_map[url]
            # Default: 404
            return FetchResult(ok=False, url=url, status=404,
                               text="", content=b"", final_url=url, fetcher="curl_cffi")

    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_magento_routes_through_make_fetcher_plain_sitemap(monkeypatch):
    """Sitemap via text/XML: counter increments and product is parsed."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    # Provide sitemap_hint to skip _discover_sitemap (simplifies fixture)
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    # Manually init since __init__ calls get_sites() which needs DB
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML,
            content=_SITEMAP_XML.encode("utf-8"),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML,
            content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    calls_before = crawler.counter.api_calls
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert crawler.counter.api_calls > calls_before, (
        f"api_calls did not increase (still {crawler.counter.api_calls})"
    )
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {result.products}. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["title"] == "Widget Pro"
    assert p["sku"] == "WP-001"
    assert p["sale_price"] == 49.99
    assert p["site"] == "example_magento"


def test_magento_gzip_sitemap_uses_res_content(monkeypatch):
    """Gzip sitemap: _sitemap_locs must use res.content (not res.text) to decompress."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_GZ_URL   # .gz url triggers gzip path
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_GZ_URL: FetchResult(
            ok=True, url=_SITEMAP_GZ_URL, status=200,
            text="",                          # text is empty / garbage for gzip
            content=_SITEMAP_XML_GZ,          # real gzip bytes in .content
            final_url=_SITEMAP_GZ_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML,
            content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    # Gzip decompression must have worked → product found
    assert len(result.products) >= 1, (
        f"Gzip sitemap not decompressed correctly. Notes: {result.notes}"
    )
    assert result.products[0]["title"] == "Widget Pro"


def test_magento_falls_back_to_positive_dom_price_when_meta_price_is_zero():
    """Costway pages can expose og price=0 while data-price-amount has the real price."""
    from app.crawlers.magento import MagentoCrawler
    from app.crawlers.base import BaseCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100
    html = """
    <html>
      <head>
        <meta property="og:title" content="Camping Mat" />
        <meta property="product:price:amount" content="0" />
      </head>
      <body>
        <span id="product-price-2554" data-price-amount="160.99"
              data-price-type="finalPrice" class="price-wrapper">
          <span class="price">£0.00</span>
        </span>
        <meta x-itemprop="price" content="160.99" />
      </body>
    </html>
    """
    url_map = {
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200, text=html,
            content=html.encode("utf-8"), final_url=_PRODUCT_URL,
            fetcher="curl_cffi",
        ),
    }
    crawler._fetcher = _make_fake_fetcher(crawler, url_map)

    row = crawler._fetch_one(_PRODUCT_URL)

    assert row is not None
    assert row["sale_price"] == 160.99
    assert row["original_price"] == 160.99
    assert row["review_count"] == 0


def test_magento_sitemap_only_rows_skip_empty_price_history():
    """URL-only sitemap rows should not create empty daily price history."""
    from app.crawlers.magento import MagentoCrawler
    from app.crawlers.base import BaseCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    BaseCrawler.__init__(crawler, site)
    crawler._sitemap_meta = {
        _PRODUCT_URL: {
            "title": "Widget Pro Premium Desk",
            "images": ["https://www.example.com/images/widget-pro.jpg"],
            "lastmod": "2026-06-29T00:00:00+00:00",
        }
    }

    row = crawler._row_from_sitemap(_PRODUCT_URL)

    assert row is not None
    assert row["sku"] == "widget-pro"
    assert row["_skip_price_history_if_no_price"] is True
    assert "sale_price" not in row
    assert "review_count" not in row
    assert row["status"] == "discovered"


def test_magento_expands_all_sitemap_index_children(monkeypatch):
    """Sitemap index expansion must not stop at the first 12 child sitemaps."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 20
    crawler.scan_cap = 100

    child_urls = [f"https://www.example.com/sitemap-{i}.xml" for i in range(1, 14)]
    index_xml = "<sitemapindex>" + "".join(
        f"<sitemap><loc>{url}</loc></sitemap>" for url in child_urls
    ) + "</sitemapindex>"
    product_13 = "https://www.example.com/products/from-child-13.html"

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=index_xml, content=index_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        product_13: FetchResult(
            ok=True, url=product_13, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=product_13, fetcher="curl_cffi",
        ),
    }
    for child in child_urls[:-1]:
        url_map[child] = FetchResult(
            ok=True, url=child, status=200,
            text="<urlset></urlset>", content=b"<urlset></urlset>",
            final_url=child, fetcher="curl_cffi",
        )
    url_map[child_urls[-1]] = FetchResult(
        ok=True, url=child_urls[-1], status=200,
        text=f"<urlset><url><loc>{product_13}</loc></url></urlset>",
        content=f"<urlset><url><loc>{product_13}</loc></url></urlset>".encode(),
        final_url=child_urls[-1], fetcher="curl_cffi",
    )

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.products[0]["title"] == "Widget Pro"


def test_magento_counter_accumulates_across_sitemap_and_products(monkeypatch):
    """Smoke: counter.api_calls >= 2 (sitemap fetch + product fetch)."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML, content=_SITEMAP_XML.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    crawler.crawl()

    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (sitemap + product), got {crawler.counter.api_calls}"
    )


def test_magento_prioritizes_costway_product_urls():
    from app.crawlers.magento import _candidate_priority

    urls = [
        "https://www.costway.de/garten.html",
        "https://www.costway.de/subscribe-newsletter",
        "https://www.costway.de/garten/gartenmobel.html",
        "https://www.costway.de/costway-kunstpflanze-22-x-88-cm-grun.html",
    ]

    ordered = sorted(urls, key=_candidate_priority)

    assert ordered[0].endswith("costway-kunstpflanze-22-x-88-cm-grun.html")
    assert ordered[-1].endswith("subscribe-newsletter")


def test_magento_costway_title_category_fallbacks_cover_common_gaps():
    from app.crawlers.magento import _category_from_title_fallback

    cases = {
        "1:10 Elektro 4-Kanal RC ferngesteuertes Auto Truck Monstertruck": (
            "Toys & Games/Remote Control Toys"
        ),
        "Elektrisches Roll-Up Keyboard mit 61 Tasten": (
            "Toys & Games/Musical Instruments"
        ),
        "2000 W Dampfreiniger Flächenreiniger Boden Dampf Reiniger": "Home Cleaning",
        "2er Set Satztisch Holz Couchtisch Beistelltisch": (
            "Furniture/Tables"
        ),
        "Outdoor Hochbeet mit Beinen Holz Pflanzkasten": (
            "Garden & Outdoor"
        ),
        "2-drzwiowa szafa z regulowanymi półkami": (
            "Furniture/Storage & Shelving"
        ),
        "13 L Soportes de Sombrilla Exterior Base para Sombrilla": (
            "Garden & Outdoor"
        ),
        "1000W Generador Eléctrico Gasolina de Estación": (
            "Tools & Home Improvement"
        ),
        "4/4 Violín de Madera con Estuche para Principiantes": (
            "Toys & Games/Musical Instruments"
        ),
        "26L Nevera Termoeléctrica Portátil": "Home Appliances",
        "266-delige universele gereedschapsset aluminium gereedschapskofferset": (
            "Tools & Home Improvement"
        ),
        "190 x 50 x 260 cm krabpaal klimboom speelboom bruin": (
            "Pet Supplies/Cat Furniture"
        ),
        "23L 800W Roestvrijstalen Magnetron Magnetron Oven": (
            "Kitchen & Dining"
        ),
        "strandparasol lichtgewicht strandtent met draagtas": (
            "Garden & Outdoor"
        ),
        "6 in 1 optrekstang deurframe optrekstang zwart": (
            "Sports & Fitness"
        ),
        "102 x 99 cm Voordak voor de Voordeur Overkapping": (
            "Garden & Outdoor"
        ),
        "3 Laags Stalen Schoenenrek Schoenen Opslag Organisator": (
            "Furniture/Storage & Shelving"
        ),
        "120 cm Kunstmatige Buxus Kunstplant Set van 2": (
            "Home Decor"
        ),
        "2 in 1 Opvouwbare Elektrische Loopband Dual LED Display": (
            "Sports & Fitness"
        ),
        "Lasmachine Ampere-lasmachine MIG 130 Elektrode-lasmachine": (
            "Tools & Home Improvement"
        ),
        "Opvouwbaar Puppyren met 8 Panelen Afsluitbare Deur": "Pet Supplies",
        "Basketbalstandaard in Hoogte Verstelbaar Binnen Buiten": (
            "Sports & Outdoor Recreation"
        ),
        "Elektrische Deken 150 x 200 cm 4 Temperatuurstanden": (
            "Home Appliances"
        ),
        "snijmachine 150 W elektrische snijmachine roestvrij staal zilver": (
            "Kitchen & Dining"
        ),
        "Vervangingsfilter HEPA-filter Anti-formaldehyde luchtzuiveringsfilter": (
            "Home Appliances"
        ),
        "3-delig servies bistro group-nature": "Kitchen & Dining",
        "Capsuledispenser voor 36 Dolce Gusto Nespresso-koffiecapsules": (
            "Kitchen & Dining"
        ),
        "Cirkelzaag 705 W 3500 omw/min Invalzaag Micro minizaag": (
            "Tools & Home Improvement"
        ),
        "versleepbaar manicurebord nagelbord studiobord met tas-wit": (
            "Beauty & Personal Care"
        ),
        "3 Pieces Kids Table and Chair Set with Chalkboard for Home": "Kids & Baby",
        "12000 BTU Portable Air Conditioner Cools Up to 46.5㎡ 5-in-1 Quiet AC Unit": (
            "Home Appliances"
        ),
        "15KG/ 24H Portable Electric Countertop Ice Cube Maker with Auto Clean Function": (
            "Kitchen & Dining"
        ),
        "101 x 101 cm Football Rebounder with 7 Adjustable Angles and 4 Ground Stakes": (
            "Sports & Outdoor Recreation"
        ),
        "Cute Hamburger Cat Bed with Padded Top and Removable Washable Cushion": (
            "Pet Supplies/Cat Furniture"
        ),
        "Industrial Floor Lamp with Adjustable Height and Lamp Head for Home Office": (
            "Lighting"
        ),
        "18KG Countertop Portable Ice Cube Making Machine for Home Office": (
            "Kitchen & Dining"
        ),
        "Mahogany Ukulele with Gig Bag and Adjustable Shoulder Strap": (
            "Toys & Games/Musical Instruments"
        ),
        "Double Size Metal Canopy Bed Frame": "Furniture/Bedroom",
        "Fuzzy Plush Rabbit Fur Bubble Blanket for Bed Armchair Sofa": (
            "Furniture/Bedroom"
        ),
        "2 Tiers Wood Nightstand with 1 Drawer and 1 Baskets for Home": (
            "Furniture/Bedroom"
        ),
        "Modern Accent Chair Linen Fabric Armchair with Solid Acacia Wood Frame": (
            "Furniture/Chairs & Seating"
        ),
        "Misting Pedestal Fan with 90° Auto Oscillation and 8 Speeds": (
            "Home Appliances"
        ),
        "15 m Automatische Schlauchaufroller Drucklufttrommel": (
            "Tools & Home Improvement"
        ),
        "Portable Badminton Net with Poles and Carrying Bag for Lawn": (
            "Sports & Outdoor Recreation"
        ),
        "Portable Toilet with 20 L Waste Tank and Flush Pump": "Bathroom",
        "Campingbett mit Vorzelt & Luftmatratze & Schlafsack & 2 Kissen für 2 Personen Feldbett": (
            "Sports & Outdoor Recreation"
        ),
        "Klavierhocker Sitzhocker Sitzbank mit Stauraum Schwarz 76 x 35 x 48 cm Holz": (
            "Toys & Games/Musical Instruments"
        ),
        "Vouwbare Rollator Voor Senioren met een Lichtgewicht Aluminium Frame": (
            "Health & Beauty/Mobility Aids"
        ),
        "Warmwaterboiler van 25 L met Dubbele Tank Elektrische Boiler": (
            "Home Appliances"
        ),
    }

    for title, category in cases.items():
        assert _category_from_title_fallback(title) == category


def test_magento_rejects_placeholder_categories():
    from app.crawlers.magento import _first_valid_category

    assert _first_valid_category("Site Pages", "Home", "Default Category") is None
    assert _first_valid_category(
        "Site Pages",
        "Home/Furniture/Office",
    ) == "Furniture/Office"


def test_magento_costway_non_product_urls_are_filtered():
    from app.crawlers.magento import _looks_like_non_product_url

    assert _looks_like_non_product_url("https://www.costway.de/black-friday")
    assert _looks_like_non_product_url("https://www.costway.es/nuevaoferta")
    assert _looks_like_non_product_url("https://www.costway.de/track-your-order")
    assert _looks_like_non_product_url("https://www.costway.de/garten.html")
    assert _looks_like_non_product_url("https://www.costway.de/pflege-kosmetik.html")
    assert _looks_like_non_product_url("https://www.costway.fr/sante-et-beaute.html")
    assert _looks_like_non_product_url("https://www.costway.it/outdoor-e-giardino.html")
    assert _looks_like_non_product_url("https://www.costway.es/cocina.html")
    assert _looks_like_non_product_url("https://www.costway.es/juguetes-y-aficiones.html")
    assert _looks_like_non_product_url("https://www.costway.es/muebles-exteriores")
    assert _looks_like_non_product_url("https://www.costway.co.uk/sports.html")
    assert _looks_like_non_product_url("https://www.costway.es/costway-home")
    assert _looks_like_non_product_url("https://www.costway.es/vuelta-al-cole")
    assert _looks_like_non_product_url("https://www.costway.es/costway-aniversario-2020")
    assert _looks_like_non_product_url("https://www.costway.es/promo-de-verano")
    assert _looks_like_non_product_url("https://www.costway.co.uk/weee-policy")
    assert _looks_like_non_product_url("https://www.costway.co.uk/happy-womens-day")
    assert _looks_like_non_product_url("https://www.costway.co.uk/whattobuy")
    assert _looks_like_non_product_url("https://www.costway.de/room/bathroom")
    assert _looks_like_non_product_url("https://www.costway.de/geschenke-fuer-eltern")
    assert _looks_like_non_product_url("https://www.costway.de/recommended-may-like")
    assert _looks_like_non_product_url("https://www.costway.de/winter-sale")
    assert _looks_like_non_product_url("https://www.costway.es/")
    assert not _looks_like_non_product_url(
        "https://www.costway.de/costway-klappstuhl-rot.html"
    )


def test_magento_skips_product_url_redirected_to_category():
    from app.crawlers.magento import MagentoCrawler
    from app.crawlers.base import BaseCrawler

    site = _site()
    site.site = "costway_de"
    site.url = "https://www.costway.de"
    site.country = "DE"
    site.brand = "Costway"
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")

    class _Fetcher:
        def get(self, url, **kw):
            return FetchResult(
                ok=True,
                url=url,
                status=200,
                text='<meta property="og:title" content="Category">'
                     '<meta property="product:price:amount" content="10.00">',
                content=b"",
                final_url="https://www.costway.de/garten/gartenmobel.html",
                fetcher="test",
            )

    crawler._fetcher = _Fetcher()

    assert crawler._fetch_one("https://www.costway.de/old-product.html") is None


def test_magento_costway_sitemap_only_rows(monkeypatch):
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    site.site = "costway_de"
    site.url = "https://www.costway.de"
    site.country = "DE"
    site.brand = "Costway"
    site.crawler_config = {"sitemap_only": True}
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100
    crawler._sitemap_meta = {}

    product_url = "https://www.costway.de/costway-klappstuhl-rot.html"
    product_url_2 = "https://www.costway.de/costway-tisch-blau.html"
    category_url = "https://www.costway.de/c/gartenstuehle.html"
    sitemap_xml = f"""
    <urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
      <url>
        <loc>{category_url}</loc>
        <image:image>
          <image:loc>https://www.costway.de/media/category.jpg</image:loc>
          <image:title>Gartenstuehle</image:title>
        </image:image>
      </url>
      <url>
        <loc>{product_url}</loc>
        <lastmod>2026-06-17T13:23:52+00:00</lastmod>
        <image:image>
          <image:loc>https://www.costway.de/media/chair.jpg</image:loc>
          <image:title>Klappstuhl Rot</image:title>
        </image:image>
      </url>
      <url>
        <loc>{product_url_2}</loc>
        <lastmod>2026-06-17T13:23:52+00:00</lastmod>
        <image:image>
          <image:loc>https://www.costway.de/media/table.jpg</image:loc>
          <image:title>Tisch Blau</image:title>
        </image:image>
      </url>
    </urlset>
    """

    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "costway-klappstuhl-rot"
    assert row["title"] == "Klappstuhl Rot"
    assert row["image_urls"] == ["https://www.costway.de/media/chair.jpg"]
    assert row["currency"] == "EUR"
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_detail_parse"
    assert "发现 2 个商品，实际入库 1 个" in (result.coverage_reason or "")


def test_magento_sitemap_only_skips_slug_title_category_rows():
    from app.crawlers.magento import MagentoCrawler
    from app.crawlers.base import BaseCrawler

    site = _site()
    site.site = "costway_de"
    site.url = "https://www.costway.de"
    site.country = "DE"
    site.brand = "Costway"
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    BaseCrawler.__init__(crawler, site)
    crawler._sitemap_meta = {
        "https://www.costway.de/baby-kind/baby-walker.html": {
            "title": "Baby Walker",
            "images": ["https://www.costway.de/media/category.jpg"],
        },
        "https://www.costway.de/costway-klappstuhl-rot.html": {
            "title": "Klappstuhl Rot",
            "images": ["https://www.costway.de/media/chair.jpg"],
        },
    }

    assert crawler._row_from_sitemap(
        "https://www.costway.de/baby-kind/baby-walker.html"
    ) is None
    row = crawler._row_from_sitemap(
        "https://www.costway.de/costway-klappstuhl-rot.html"
    )

    assert row is not None
    assert row["sku"] == "costway-klappstuhl-rot"
    assert row["title"] == "Klappstuhl Rot"


def test_magento_total_counts_products_not_candidate_pages(monkeypatch):
    """Mixed sitemaps contain category URLs; total_product_count must count products."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 10
    crawler.scan_cap = 100

    category_url = "https://www.example.com/category/chairs.html"
    product_2 = "https://www.example.com/products/widget-mini.html"
    sitemap_xml = f"""
    <urlset>
      <url><loc>{_PRODUCT_URL}</loc></url>
      <url><loc>{category_url}</loc></url>
      <url><loc>{product_2}</loc></url>
    </urlset>
    """
    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
        product_2: FetchResult(
            ok=True, url=product_2, status=200,
            text=_PRODUCT_HTML_2, content=_PRODUCT_HTML_2.encode(),
            final_url=product_2, fetcher="curl_cffi",
        ),
        category_url: FetchResult(
            ok=True, url=category_url, status=200,
            text=_CATEGORY_HTML, content=_CATEGORY_HTML.encode(),
            final_url=category_url, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 2
    assert result.total_product_count == 2
    assert result.coverage_complete is True


def test_magento_limit_does_not_shrink_total_product_count(monkeypatch):
    """MAGENTO_LIMIT caps emitted rows, not the discovered product denominator."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = _SITEMAP_URL
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    product_2 = "https://www.example.com/products/widget-mini.html"
    sitemap_xml = f"""
    <urlset>
      <url><loc>{_PRODUCT_URL}</loc></url>
      <url><loc>{product_2}</loc></url>
    </urlset>
    """
    url_map = {
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=sitemap_xml, content=sitemap_xml.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
        product_2: FetchResult(
            ok=True, url=product_2, status=200,
            text=_PRODUCT_HTML_2, content=_PRODUCT_HTML_2.encode(),
            final_url=product_2, fetcher="curl_cffi",
        ),
    }
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert "实际入库 1 个" in (result.coverage_reason or "")


def test_magento_robots_txt_discovery(monkeypatch):
    """Without sitemap_hint, crawler discovers sitemap from robots.txt."""
    from app.crawlers.magento import MagentoCrawler

    site = _site()
    crawler = MagentoCrawler.__new__(MagentoCrawler)
    from app.crawlers.base import BaseCrawler
    BaseCrawler.__init__(crawler, site)
    crawler.base = site.url.rstrip("/")
    crawler.sitemap_hint = None   # force auto-discovery
    crawler.product_match = ""
    crawler.limit = 1
    crawler.scan_cap = 100

    robots_url = site.url.rstrip("/") + "/robots.txt"

    url_map = {
        robots_url: FetchResult(
            ok=True, url=robots_url, status=200,
            text=_ROBOTS_TXT, content=_ROBOTS_TXT.encode(),
            final_url=robots_url, fetcher="curl_cffi",
        ),
        _SITEMAP_URL: FetchResult(
            ok=True, url=_SITEMAP_URL, status=200,
            text=_SITEMAP_XML, content=_SITEMAP_XML.encode(),
            final_url=_SITEMAP_URL, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode(),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))

    result = crawler.crawl()

    assert len(result.products) >= 1, (
        f"robots.txt discovery path failed. Notes: {result.notes}"
    )
