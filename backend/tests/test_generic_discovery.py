from __future__ import annotations

import pytest

from app.crawlers.base import CrawlResult
from app.crawlers.generic import GenericCrawler
from app.models import Site

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")


class _FakeSession:
    def __init__(self, pages: dict[str, _Resp]):
        self.pages = pages

    def get(self, url: str, timeout=30):
        return self.pages.get(url, _Resp(404, ""))


def _crawler(monkeypatch):
    monkeypatch.setattr("app.crawlers.generic.get_sites", lambda: [])
    site = Site(site="x", url="https://x.com", country="US",
                platform="generic", proxy_tier="none")
    return GenericCrawler(site)


def _crawler_for(site_code: str, country: str, monkeypatch):
    monkeypatch.setattr("app.crawlers.generic.get_sites", lambda: [])
    site = Site(site=site_code, url="https://x.com", country=country,
                platform="generic", proxy_tier="none")
    return GenericCrawler(site)


def test_generic_discovers_sitemap_from_robots(monkeypatch):
    crawler = _crawler(monkeypatch)
    sess = _FakeSession({
        "https://x.com/robots.txt": _Resp(
            text="Sitemap: https://x.com/custom-sitemap.xml\n"),
        "https://x.com/custom-sitemap.xml": _Resp(
            text="<urlset><url><loc>https://x.com/products/widget</loc></url></urlset>"),
    })

    urls = crawler._discover_product_urls(sess, CrawlResult())

    assert urls == ["https://x.com/products/widget"]


def test_generic_falls_back_to_entry_page_links(monkeypatch):
    crawler = _crawler(monkeypatch)
    sess = _FakeSession({
        "https://x.com": _Resp(
            text='<html><a href="/products/widget">Widget</a></html>'),
    })

    urls = crawler._discover_product_urls(sess, CrawlResult())

    assert urls == ["https://x.com/products/widget"]


def test_generic_keeps_product_without_price(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Widget Chair","sku":"W-1","offers":{}}
        </script>
      </head>
      <body>Widget Chair</body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/widget": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/widget")

    assert row is not None
    assert row["sku"] == "W-1"
    assert row["title"] == "Widget Chair"
    assert row["sale_price"] is None
    assert row["original_price"] is None


def test_generic_parses_h1_product_without_structured_data(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head><title>Fallback Title</title></head>
      <body><h1>Simple Product Name</h1></body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/simple-chair": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/simple-chair")

    assert row is not None
    assert row["sku"] == "simple-chair"
    assert row["title"] == "Simple Product Name"
    assert row["product_url"] == "https://x.com/products/simple-chair"


def test_generic_prefers_complete_page_title_over_weak_jsonld_name(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <meta property="og:title" content="Premium Walnut Storage Cabinet with Sliding Doors" />
        <script type="application/ld+json">
        {"@type":"Product","name":"Product","sku":"CAB-1","offers":{"price":"149.99","priceCurrency":"USD"}}
        </script>
      </head>
      <body><h1>Cabinet</h1></body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/cabinet": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/cabinet")

    assert row is not None
    assert row["sku"] == "CAB-1"
    assert row["title"] == "Premium Walnut Storage Cabinet with Sliding Doors"
    assert row["sale_price"] == 149.99


def test_generic_uses_best_dom_product_title_candidate(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <title>Shop now | Example</title>
        <meta property="product:price:amount" content="$59.99" />
      </head>
      <body>
        <h1>Chair</h1>
        <div data-testid="product-title">
          Modern Boucle Accent Chair with Solid Wood Legs
        </div>
      </body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/accent-chair": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/accent-chair")

    assert row is not None
    assert row["sku"] == "accent-chair"
    assert row["title"] == "Modern Boucle Accent Chair with Solid Wood Legs"
    assert row["sale_price"] == 59.99


def test_generic_uses_shared_currency_mapping(monkeypatch):
    crawler = _crawler_for("generic_gb", "GB", monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html><body><h1>UK Product</h1></body></html>
    """
    sess = _FakeSession({"https://x.com/products/uk-product": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/uk-product")

    assert row["currency"] == "GBP"


def test_generic_price_parser_handles_thousands_separator(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <meta property="og:title" content="Expensive Chair" />
        <meta property="product:price:amount" content="$1,299.99" />
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/expensive-chair": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/expensive-chair")

    assert row["sale_price"] == 1299.99
    assert row["original_price"] == 1299.99


def test_generic_falls_back_to_dom_price_nodes(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head><meta property="og:title" content="DOM Priced Shelf" /></head>
      <body>
        <h1>DOM Priced Shelf</h1>
        <div class="product-price" data-price="$89.95">$89.95</div>
      </body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/dom-shelf": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/dom-shelf")

    assert row["sale_price"] == 89.95
    assert row["original_price"] == 89.95


def test_generic_collects_dom_promotion_badges(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head><meta property="og:title" content="Coupon Ready Desk" /></head>
      <body>
        <h1>Coupon Ready Desk</h1>
        <div class="product-price">$120.00</div>
        <span data-testid="promotion-badge">Save 20% with code WORK20</span>
      </body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/coupon-desk": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/coupon-desk")

    assert row is not None
    assert row["attributes"]["promotions"] == ["Save 20% with code WORK20"]


def test_generic_parses_next_hydration_product_data(monkeypatch):
    crawler = _crawler_for("generic_de", "DE", monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "product": {
                "productName": "Complete Hydrated Oak Dining Table",
                "sku": "TABLE-42",
                "salePrice": {"value": "249,90"},
                "regularPrice": {"value": "329,90"},
                "currencyCode": "EUR",
                "imageUrl": "https://x.com/table.jpg",
                "brand": {"name": "OakCo"},
                "categoryName": "Dining Room",
                "rating": 4.8,
                "reviewCount": 19
              }
            }
          }
        }
        </script>
      </head>
      <body><h1>Table</h1></body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/table": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/table")

    assert row is not None
    assert row["sku"] == "TABLE-42"
    assert row["title"] == "Complete Hydrated Oak Dining Table"
    assert row["sale_price"] == 249.9
    assert row["original_price"] == 329.9
    assert row["currency"] == "EUR"
    assert row["image_urls"] == ["https://x.com/table.jpg"]
    assert row["brand"] == "OakCo"
    assert row["category_path"] == "Dining Room"
    assert row["ratings"] == 4.8
    assert row["review_count"] == 19


def test_generic_parses_nested_hydration_price_range(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "product": {
                "title": "Nested Price Storage Bench",
                "id": "BENCH-100",
                "priceRange": {
                  "minVariantPrice": {"amount": "149.95", "currencyCode": "USD"},
                  "maxVariantPrice": {"amount": "199.95", "currencyCode": "USD"}
                },
                "regularPrice": {"current": {"amount": "199.95"}},
                "images": [{"url": "https://x.com/bench.jpg"}]
              }
            }
          }
        }
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/bench": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/bench")

    assert row is not None
    assert row["sku"] == "BENCH-100"
    assert row["title"] == "Nested Price Storage Bench"
    assert row["sale_price"] == 149.95
    assert row["original_price"] == 199.95
    assert row["image_urls"] == ["https://x.com/bench.jpg"]


def test_generic_uses_more_complete_hydrated_title_with_jsonld_price(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Chair","sku":"CHAIR-1",
         "offers":{"price":"89.99","priceCurrency":"USD"}}
        </script>
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"product":{
          "title":"Ergonomic Mesh Office Chair with Adjustable Headrest",
          "sku":"CHAIR-1"
        }}}}
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/chair": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/chair")

    assert row is not None
    assert row["title"] == "Ergonomic Mesh Office Chair with Adjustable Headrest"
    assert row["sale_price"] == 89.99
    assert row["currency"] == "USD"


def test_generic_chooses_best_jsonld_product_candidate(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@graph": [
            {"@type":"Product","name":"Chair"},
            {
              "@type":"Product",
              "name":"Executive Ergonomic Office Chair with Lumbar Support",
              "sku":"CHAIR-BEST",
              "brand":{"name":"SeatCo"},
              "image":["https://x.com/chair.jpg"],
              "offers":{"price":"129.99","priceCurrency":"USD"}
            }
          ]
        }
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/best-chair": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/best-chair")

    assert row is not None
    assert row["sku"] == "CHAIR-BEST"
    assert row["title"] == "Executive Ergonomic Office Chair with Lumbar Support"
    assert row["sale_price"] == 129.99
    assert row["brand"] == "SeatCo"


def test_generic_does_not_parse_soft_block_page(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <title>Just a moment...</title>
        <meta property="og:title" content="Access Denied - Security Check" />
        <meta property="product:price:amount" content="$49.99" />
      </head>
      <body><h1>Checking your browser</h1><div class="price">$49.99</div></body>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/blocked": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/blocked")

    assert row is None


def test_generic_parses_productgroup_aggregate_offer(monkeypatch):
    crawler = _crawler_for("generic_gb", "GB", monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@type": "WebPage",
          "mainEntity": {
            "@type": "ProductGroup",
            "name": "Modular Sofa",
            "productID": "SOFA-1",
            "image": {"url": "https://x.com/sofa.jpg"},
            "brand": {"name": "Acme"},
            "category": {"name": "Living Room"},
            "aggregateRating": {"ratingValue": "4.6", "ratingCount": "37"},
            "offers": {
              "@type": "AggregateOffer",
              "lowPrice": "1,299.99",
              "highPrice": "1,799.99",
              "priceCurrency": "GBP",
              "availability": "https://schema.org/InStock"
            }
          }
        }
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/modular-sofa": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/modular-sofa")

    assert row is not None
    assert row["sku"] == "SOFA-1"
    assert row["title"] == "Modular Sofa"
    assert row["sale_price"] == 1299.99
    assert row["original_price"] == 1799.99
    assert row["currency"] == "GBP"
    assert row["image_urls"] == ["https://x.com/sofa.jpg"]
    assert row["brand"] == "Acme"
    assert row["category_path"] == "Living Room"
    assert row["attributes"]["offers"][0]["@type"] == "AggregateOffer"
    assert row["ratings"] == 4.6
    assert row["review_count"] == 37


def test_generic_preserves_structured_promotion_offers(monkeypatch):
    crawler = _crawler(monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "Coupon Desk",
          "sku": "DESK-7",
          "offers": [
            {
              "@type": "Offer",
              "price": "199.99",
              "priceCurrency": "USD",
              "availability": "https://schema.org/InStock"
            },
            {
              "@type": "Offer",
              "name": "Save 20% with code WORK20",
              "price": "159.99",
              "priceCurrency": "USD",
              "discount": "20% off",
              "validThrough": "2026-06-30"
            }
          ]
        }
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/coupon-desk": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/coupon-desk")

    assert row is not None
    assert row["sale_price"] == 199.99
    assert row["attributes"]["offers"][1]["name"] == "Save 20% with code WORK20"


def test_generic_parses_schema_org_product_type_from_graph(monkeypatch):
    crawler = _crawler_for("generic_de", "DE", monkeypatch)
    monkeypatch.setattr(crawler, "snapshot", lambda *args, **kwargs: None)
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@graph": [
            {"@type": "BreadcrumbList"},
            {
              "@type": "https://schema.org/Product",
              "name": "Graph Desk",
              "sku": "DESK-9",
              "image": ["https://x.com/desk.jpg"],
              "brand": "DeskCo",
              "offers": {
                "priceSpecification": {
                  "price": "59,90",
                  "priceCurrency": "EUR"
                },
                "availability": "https://schema.org/OutOfStock"
              },
              "aggregateRating": {"ratingValue": "4.2", "reviewCount": "12"}
            }
          ]
        }
        </script>
      </head>
    </html>
    """
    sess = _FakeSession({"https://x.com/products/graph-desk": _Resp(text=html)})

    row = crawler._parse(sess, "https://x.com/products/graph-desk")

    assert row is not None
    assert row["sku"] == "DESK-9"
    assert row["sale_price"] == 59.9
    assert row["currency"] == "EUR"
    assert row["brand"] == "DeskCo"
    assert row["status"] == "out_of_stock"
