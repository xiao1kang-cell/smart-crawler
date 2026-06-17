from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Site
from app.price_sources import enrich_products_from_site_config

pytestmark = pytest.mark.unit


def test_price_feed_supports_custom_field_mapping(tmp_path: Path):
    feed = tmp_path / "prices.csv"
    feed.write_text(
        "product_id,final,was,ccy,product_name\n"
        "SKU-1,12.5,19.9,EUR,Feed Title\n",
        encoding="utf-8",
    )
    site = Site(
        site="feed_de",
        proxy_tier="none",
        crawler_config={
            "price_source_type": "feed",
            "price_feed_url": str(feed),
            "price_feed_sku_field": "product_id",
            "price_feed_sale_price_field": "final",
            "price_feed_original_price_field": "was",
            "price_feed_currency_field": "ccy",
            "price_feed_title_field": "product_name",
        },
    )
    products = [{"sku": "SKU-1", "title": "", "product_url": "https://x/1"}]

    enriched, stats = enrich_products_from_site_config(site, products)

    assert stats["applied"] is True
    assert stats["matched"] == 1
    assert stats["updated"] == 1
    assert enriched[0]["sale_price"] == 12.5
    assert enriched[0]["original_price"] == 19.9
    assert enriched[0]["currency"] == "EUR"
    assert enriched[0]["title"] == "Feed Title"


def test_price_api_template_enriches_by_sku(tmp_path: Path):
    api = tmp_path / "SKU-2.json"
    api.write_text(
        '{"sku":"SKU-2","price":"21.00","regular_price":"25.00","currency":"USD"}',
        encoding="utf-8",
    )
    site = Site(
        site="api_us",
        proxy_tier="none",
        crawler_config={
            "price_source_type": "api",
            "pdp_price_api_url": str(tmp_path / "{sku}.json"),
        },
    )
    products = [{"sku": "SKU-2", "title": "API product",
                 "product_url": "https://x/2"}]

    enriched, stats = enrich_products_from_site_config(site, products)

    assert stats["rows"] == 1
    assert stats["matched"] == 1
    assert enriched[0]["sale_price"] == 21.0
    assert enriched[0]["original_price"] == 25.0


def test_pdp_selector_enriches_price_from_html(tmp_path: Path):
    html = tmp_path / "pdp.html"
    html.write_text(
        '<html><body><h1 class="title">PDP Title</h1>'
        '<span class="price">€33,40</span></body></html>',
        encoding="utf-8",
    )
    site = Site(
        site="pdp_de",
        proxy_tier="none",
        crawler_config={
            "price_source_type": "pdp",
            "pdp_price_selector": ".price",
            "pdp_title_selector": ".title",
            "price_source_max_items": 1,
        },
    )
    products = [{"sku": "SKU-3", "title": "", "product_url": str(html)}]

    enriched, stats = enrich_products_from_site_config(site, products)

    assert stats["rows"] == 1
    assert stats["matched"] == 1
    assert enriched[0]["sale_price"] == 33.4
    assert enriched[0]["title"] == "PDP Title"
