from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Product
from app import pipeline
from app.pipeline import normalize, to_price, upsert_products
from app.runner import _detect_promotions

pytestmark = pytest.mark.unit


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def test_normalize_keeps_original_none_when_missing():
    p = normalize({"sku": "S1", "title": "t", "product_url": "u",
                   "site": "x", "sale_price": 10})
    assert p["original_price"] is None


def test_normalize_preserves_real_original():
    p = normalize({"sku": "S1", "title": "t", "product_url": "u",
                   "site": "x", "sale_price": 10, "original_price": 20})
    assert p["original_price"] == 20.0


def test_to_price_handles_common_locale_separators():
    assert to_price("$1,299.00") == 1299.0
    assert to_price("€1.299,00") == 1299.0
    assert to_price("1 299,50 zł") == 1299.5
    assert to_price("RM 1,299") == 1299.0
    assert to_price("19,99") == 19.99


def test_normalize_uses_common_price_aliases():
    p = normalize({
        "sku": "S1",
        "title": "Alias priced product",
        "product_url": "u",
        "site": "x",
        "price": "€49,99",
        "was_price": "€79,99",
    })
    assert p["sale_price"] == 49.99
    assert p["original_price"] == 79.99


def test_normalize_backfills_currency_from_site_market():
    p = normalize({"sku": "S1", "title": "t", "product_url": "u",
                   "site": "vidaxl_ca", "sale_price": 10})
    assert p["currency"] == "CAD"


def test_normalize_corrects_symbol_currency_to_site_market():
    p = normalize({"sku": "S1", "title": "t", "product_url": "u",
                   "site": "costway_de", "sale_price": "€49,99",
                   "currency": "$"})
    assert p["currency"] == "EUR"


def test_detect_promotions_only_fires_on_real_discount():
    db = _session()
    # A: 有真实折扣 original 20 > sale 10
    db.add(Product(site="x", sku="A", title="A", sale_price=10.0,
                   original_price=20.0, status="on_sale"))
    # B: 仅 sale，无 original（回填已删 → original 应为 None，不算促销）
    db.add(Product(site="x", sku="B", title="B", sale_price=10.0,
                   original_price=None, status="on_sale"))
    db.commit()
    n = _detect_promotions(db, "x")
    db.flush()  # _detect_promotions adds via session.add(); flush makes them visible to query
    assert n == 1
    from app.models import Promotion
    skus = [r.sku for r in db.query(Promotion).filter(Promotion.site == "x").all()]
    assert skus == ["A"]


def test_detect_promotions_from_attributes_coupon_text():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=10.0,
                   original_price=None, status="on_sale",
                   attributes={"coupon": "Save 15% with code HOME15"}))
    db.add(Product(site="x", sku="B", title="B", sale_price=10.0,
                   original_price=None, status="on_sale",
                   attributes={"color": "black"}))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    rows = db.query(Promotion).filter(Promotion.site == "x").all()
    assert n == 1
    assert [(r.sku, r.promotion_name, r.discount_percent) for r in rows] == [
        ("A", "Save 15% with code HOME15", 15)
    ]


def test_detect_promotions_from_dom_badge_list_attribute():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=120.0,
                   original_price=None, status="on_sale",
                   attributes={"promotions": ["Save 20% with code WORK20"]}))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    row = db.query(Promotion).filter(Promotion.site == "x").one()
    assert n == 1
    assert row.promotion_name == "Save 20% with code WORK20"
    assert row.promotion_type == "coupon"
    assert row.discount_percent == 20


def test_detect_promotions_from_localized_discount_text():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=29.99,
                   original_price=29.99, status="on_sale",
                   attributes={
                       "promotions": [
                           "Achtung: 20 % Rabatt auf alle Produkte "
                           "Rabatt wird automatisch im Warenkorb eingelöst."
                       ],
                   }))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    row = db.query(Promotion).filter(Promotion.site == "x").one()
    assert n == 1
    assert row.sku == "A"
    assert row.promotion_type == "price_promotion"
    assert row.discount_percent == 20
    assert "20 % Rabatt" in row.promotion_name


def test_detect_promotions_extracts_common_campaign_metadata():
    db = _session()
    db.add(Product(
        site="x", sku="A", title="A", sale_price=80.0,
        original_price=100.0, status="on_sale",
        attributes={
            "campaign_name": "Summer coupon",
            "promo_type": "coupon",
            "coupon": "Save 20% on orders over $100 with code SUMMER20",
            "minimum_order": "orders over $100",
            "valid_from": "2026-06-01T00:00:00",
            "valid_until": "2026-06-30 23:59:00",
        },
    ))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    promo = db.query(Promotion).filter(Promotion.site == "x", Promotion.sku == "A").one()
    assert n == 1
    assert promo.promotion_name == "Summer coupon"
    assert promo.promotion_type == "coupon"
    assert promo.discount_percent == 20
    assert promo.threshold == "orders over $100"
    assert promo.start_time.isoformat() == "2026-06-01T00:00:00"
    assert promo.end_time.isoformat() == "2026-06-30T23:59:00"


def test_detect_promotions_expands_multiple_explicit_offers_per_sku():
    db = _session()
    db.add(Product(
        site="x", sku="A", title="A", sale_price=80.0,
        original_price=100.0, status="on_sale",
        attributes={
            "offers": [
                {
                    "promotion_name": "Summer coupon",
                    "promotion_type": "coupon",
                    "discount_percent": 15,
                    "threshold": "orders over $50",
                    "valid_from": "2026-06-01",
                    "valid_until": "2026-06-15",
                },
                {
                    "name": "Bundle save",
                    "type": "bundle",
                    "discount": "20% off",
                    "minimum_order": "orders over $100",
                },
            ],
        },
    ))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    rows = (db.query(Promotion)
            .filter(Promotion.site == "x", Promotion.sku == "A")
            .order_by(Promotion.id)
            .all())
    assert n == 2
    assert [r.promotion_name for r in rows] == ["Summer coupon", "Bundle save"]
    assert [r.promotion_type for r in rows] == ["coupon", "bundle"]
    assert [r.discount_percent for r in rows] == [15, 20]
    assert [r.threshold for r in rows] == ["orders over $50", "orders over $100"]
    assert rows[0].start_time.isoformat() == "2026-06-01T00:00:00"
    assert rows[0].end_time.isoformat() == "2026-06-15T00:00:00"


def test_detect_promotions_uses_explicit_offer_without_product_discount():
    db = _session()
    db.add(Product(
        site="x", sku="A", title="A", sale_price=100.0,
        original_price=100.0, status="on_sale",
        attributes={
            "offers": [
                {
                    "@type": "Offer",
                    "price": "100.00",
                    "priceCurrency": "USD",
                },
                {
                    "name": "Save 10% with code HOME10",
                    "promotion_type": "coupon",
                    "discount": "10% off",
                    "validThrough": "2026-06-30",
                },
            ],
        },
    ))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    rows = db.query(Promotion).filter(Promotion.site == "x").all()
    assert n == 1
    assert rows[0].promotion_name == "Save 10% with code HOME10"
    assert rows[0].promotion_type == "coupon"
    assert rows[0].discount_percent == 10
    assert rows[0].end_time.isoformat() == "2026-06-30T00:00:00"


def test_detect_promotions_from_free_shipping_flag():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=100.0,
                   original_price=100.0, status="on_sale",
                   has_free_shipping=True))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    row = db.query(Promotion).filter(Promotion.site == "x").one()
    assert n == 1
    assert row.promotion_type == "free_shipping"
    assert row.promotion_name == "Free shipping"


def test_detect_promotions_from_multibuy_bundle_text():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=100.0,
                   original_price=100.0, status="on_sale",
                   attributes={"promotions": ["Multi-buy bundle: buy 2 save 15%"]}))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    row = db.query(Promotion).filter(Promotion.site == "x").one()
    assert n == 1
    assert row.promotion_type == "bundle"
    assert row.discount_percent == 15


def test_detect_promotions_expands_multiple_string_labels_and_free_shipping():
    db = _session()
    db.add(Product(
        site="x", sku="A", title="A", sale_price=100.0,
        original_price=100.0, status="on_sale",
        has_free_shipping=True,
        attributes={
            "promotions": [
                "Save 10% with code HOME10",
                "Multi-buy bundle: buy 2 save 15%",
            ],
        },
    ))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    rows = (db.query(Promotion)
            .filter(Promotion.site == "x", Promotion.sku == "A")
            .order_by(Promotion.promotion_type, Promotion.promotion_name)
            .all())
    assert n == 3
    assert {
        (row.promotion_type, row.promotion_name, row.discount_percent)
        for row in rows
    } == {
        ("bundle", "Multi-buy bundle: buy 2 save 15%", 15),
        ("coupon", "Save 10% with code HOME10", 10),
        ("free_shipping", "Free shipping", None),
    }


def test_detect_promotions_dedupes_free_shipping_flag_and_label():
    db = _session()
    db.add(Product(
        site="x", sku="A", title="A", sale_price=100.0,
        original_price=100.0, status="on_sale",
        has_free_shipping=True,
        attributes={"promotions": ["Free shipping on orders over $99"]},
    ))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    rows = db.query(Promotion).filter(Promotion.site == "x", Promotion.sku == "A").all()
    assert n == 1
    assert len(rows) == 1
    assert rows[0].promotion_type == "free_shipping"
    assert rows[0].promotion_name == "Free shipping on orders over $99"


def test_detect_promotions_from_localized_free_delivery_text():
    db = _session()
    db.add(Product(site="x", sku="A", title="A", sale_price=100.0,
                   original_price=100.0, status="on_sale",
                   attributes={"delivery_label": "Livraison gratuite dès 50€"}))
    db.commit()

    n = _detect_promotions(db, "x")
    db.flush()

    from app.models import Promotion
    row = db.query(Promotion).filter(Promotion.site == "x").one()
    assert n == 1
    assert row.promotion_type == "free_shipping"


def test_upsert_preserves_better_existing_title_from_weak_update():
    db = _session()
    upsert_products(db, "x", [{
        "site": "x",
        "sku": "SKU-1",
        "title": "Premium Walnut Storage Cabinet with Doors",
        "product_url": "https://example.com/products/sku-1",
        "sale_price": "€1.299,00",
    }])
    db.commit()

    upsert_products(db, "x", [{
        "site": "x",
        "sku": "SKU-1",
        "title": "SKU-1",
        "product_url": "https://example.com/products/sku-1",
        "sale_price": "$1,399.00",
    }])
    db.commit()

    row = db.query(Product).filter(Product.site == "x", Product.sku == "SKU-1").one()
    assert row.title == "Premium Walnut Storage Cabinet with Doors"
    assert row.sale_price == 1399.0
    assert row.original_price is None


def test_upsert_products_chunks_existing_sku_lookup(monkeypatch):
    db = _session()
    monkeypatch.setattr(pipeline, "_EXISTING_PRODUCT_LOOKUP_CHUNK_SIZE", 2)
    db.add_all([
        Product(site="x", sku="SKU-1", title="Old 1",
                product_url="https://example.com/old-1"),
        Product(site="x", sku="SKU-3", title="Old 3",
                product_url="https://example.com/old-3"),
    ])
    db.commit()

    stats = upsert_products(db, "x", [
        {"site": "x", "sku": f"SKU-{idx}", "title": f"New {idx}",
         "product_url": f"https://example.com/new-{idx}"}
        for idx in range(1, 6)
    ])
    db.commit()

    assert stats["updated"] == 2
    assert stats["inserted"] == 3
    rows = {p.sku: p.title for p in db.query(Product).filter(Product.site == "x")}
    assert rows == {
        "SKU-1": "New 1",
        "SKU-2": "New 2",
        "SKU-3": "New 3",
        "SKU-4": "New 4",
        "SKU-5": "New 5",
    }
