from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.admin_spine import (
    _field_fix_template_payload,
    _product_quality_issues,
    _promotion_template_payload,
    _review_history_template_payload,
    _sales_template_payload,
    _sku_target_template_payload,
    _validate_field_fix_rows,
    _validate_promotion_rows,
    _validate_review_history_rows,
    _validate_sales_rows,
    _validate_sku_target_rows,
    admin_aosen_acceptance_action_plan,
    admin_aosen_field_quality_acceptance,
    admin_data_quality_products,
    admin_product_field_fixes_import,
    admin_promotion_signals_import,
    admin_review_history_import,
    admin_sku_targets_import,
)
from app.api.routes import _build_data_quality_payload
from app.db import Base
from app.models import PriceHistory, Product, Promotion, Site, Workspace, WorkspaceSite

pytestmark = pytest.mark.unit


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def test_validate_sales_rows_accepts_external_sales_and_revenue():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Chair",
                   product_url="https://example.com/h-1", sale_price=50))
    db.commit()

    out = _validate_sales_rows(db, [{
        "site": "homary_us",
        "sku": "H-1",
        "date": "2026-06-28",
        "thirty_day_sales": "12",
        "thirty_day_revenue": "$600.00",
    }])

    assert out["valid"] is True
    assert out["valid_rows"] == 1
    assert out["valid_items"][0]["thirty_day_sales"] == 12
    assert out["valid_items"][0]["thirty_day_revenue"] == 600.0


def test_validate_review_history_rows_accepts_review_snapshot():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Chair",
                   product_url="https://example.com/h-1", sale_price=50))
    db.commit()

    out = _validate_review_history_rows(db, [{
        "site": "homary_us",
        "sku": "H-1",
        "date": "2026-06-21",
        "review_count": "1,234",
        "sale_price": "$50.00",
    }])

    assert out["valid"] is True
    assert out["valid_rows"] == 1
    assert out["valid_items"][0]["review_count"] == 1234
    assert out["valid_items"][0]["sale_price"] == 50.0


def test_review_history_import_recomputes_sales_from_review_delta():
    db = _session()
    today = date.today()
    prior = today - timedelta(days=7)
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Chair",
                   product_url="https://example.com/h-1", sale_price=50,
                   review_count=12))
    db.add(PriceHistory(site="homary_us", sku="H-1", date=today,
                        sale_price=50, review_count=12))
    db.commit()

    out = admin_review_history_import({
        "rows": [{
            "site": "homary_us",
            "sku": "H-1",
            "date": prior.isoformat(),
            "review_count": 10,
            "sale_price": 50,
        }]
    }, user="admin", db=db, ip="")

    product = db.query(Product).filter(Product.site == "homary_us").one()

    assert out["rows"] == 1
    assert out["created"] == 1
    assert out["by_site"]["homary_us"]["estimated_skus"] == 1
    assert product.thirty_day_sales == 80
    assert product.thirty_day_revenue == 4000.0


def test_review_history_template_excludes_deferred_sites_by_default():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="homary_us", brand="Homary", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="homary_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_us", enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="homary_us", sku="H-1", title="Chair",
                product_url="https://example.com/h-1", sale_price=50,
                review_count=12),
        Product(site="vidaxl_us", sku="V-1", title="Deferred",
                product_url="https://example.com/v-1", sale_price=50,
                review_count=12),
    ])
    db.commit()

    out = _review_history_template_payload(db, limit=10)

    assert out["deferred_sites"] == ["vidaxl_ca", "vidaxl_us"]
    assert [item["site"] for item in out["items"]] == ["homary_us"]
    assert out["items"][0]["current_review_count"] == 12


def test_validate_promotion_rows_accepts_external_coupon():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Chair",
                   product_url="https://example.com/h-1", sale_price=50,
                   image_urls=["https://example.com/h-1.jpg"]))
    db.commit()

    out = _validate_promotion_rows(db, [{
        "site": "homary_us",
        "sku": "H-1",
        "promotion_name": "Save 10% with code HOME10",
        "coupon_code": "HOME10",
        "discount_percent": "10%",
        "promotion_price": "£45.00",
    }])

    assert out["valid"] is True
    assert out["valid_rows"] == 1
    assert out["valid_items"][0]["promotion_type"] == "coupon"
    assert out["valid_items"][0]["discount_percent"] == 10
    assert out["valid_items"][0]["promotion_price"] == 45.0


def test_promotion_signals_import_creates_promotion_and_refreshes_metrics():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Chair",
                   product_url="https://example.com/h-1", sale_price=50,
                   original_price=60, image_urls=["https://example.com/h-1.jpg"]))
    db.commit()

    out = admin_promotion_signals_import({
        "csv": (
            "site,sku,promotion_name,coupon_code,discount_percent\n"
            "homary_us,H-1,Save 10% with code HOME10,HOME10,10\n"
        )
    }, user="admin", db=db)

    promo = db.query(Promotion).filter(Promotion.site == "homary_us").one()
    assert out["rows"] == 1
    assert out["created"] == 1
    assert promo.promotion_type == "coupon"
    assert promo.promotion_name == "Save 10% with code HOME10"
    assert promo.discount_percent == 10


def test_sales_template_excludes_deferred_vidaxl_sites_by_default():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="homary_us", brand="Homary", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
        Site(site="vidaxl_ca", brand="Vidaxl", country="CA"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="homary_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_ca", enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="homary_us", sku="H-1", title="Chair",
                product_url="https://example.com/h-1", sale_price=50),
        Product(site="vidaxl_us", sku="V-US", title="US",
                product_url="https://example.com/us", sale_price=20),
        Product(site="vidaxl_ca", sku="V-CA", title="CA",
                product_url="https://example.com/ca", sale_price=20),
    ])
    db.commit()

    out = _sales_template_payload(db, day=date(2026, 6, 28))

    sites = {item["site"] for item in out["items"]}
    assert sites == {"homary_us"}
    assert out["total_count"] == 1
    assert out["limit"] == 5000
    assert out["deferred_sites"] == ["vidaxl_ca", "vidaxl_us"]


def test_promotion_template_excludes_deferred_vidaxl_sites_by_default():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="homary_us", brand="Homary", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
        Site(site="vidaxl_ca", brand="Vidaxl", country="CA"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="homary_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_ca", enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="homary_us", sku="H-1", title="Chair",
                product_url="https://example.com/h-1", sale_price=50),
        Product(site="vidaxl_us", sku="V-US", title="US",
                product_url="https://example.com/us", sale_price=20),
        Product(site="vidaxl_ca", sku="V-CA", title="CA",
                product_url="https://example.com/ca", sale_price=20),
    ])
    db.commit()

    out = _promotion_template_payload(db, limit=1)

    sites = {item["site"] for item in out["items"]}
    assert sites == {"homary_us"}
    assert out["total_count"] == 1
    assert out["limit"] == 1
    assert out["deferred_sites"] == ["vidaxl_ca", "vidaxl_us"]


def test_signal_templates_can_skip_expensive_total_count_for_previews():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(WorkspaceSite(workspace_id=1, site="homary_us",
                         enabled=True, hidden=False))
    db.add_all([
        Product(site="homary_us", sku="H-1", title="Chair 1",
                product_url="https://example.com/h-1", sale_price=50),
        Product(site="homary_us", sku="H-2", title="Chair 2",
                product_url="https://example.com/h-2", sale_price=60),
    ])
    db.commit()

    promo = _promotion_template_payload(
        db, limit=1, include_total_count=False)
    sales = _sales_template_payload(
        db, day=date(2026, 6, 28), limit=1, include_total_count=False)

    assert promo["total_count"] is None
    assert promo["count"] == 1
    assert promo["has_more"] is True
    assert sales["total_count"] is None
    assert sales["count"] == 1
    assert sales["has_more"] is True


def test_field_fix_template_excludes_deferred_sites_and_flags_bad_fields():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="homary_us", brand="Homary", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="homary_us", enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_us", enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="homary_us", sku="H-1", title="H-1",
                product_url="https://example.com/h-1", sale_price=0,
                currency="", category_path="", image_urls=[]),
        Product(site="vidaxl_us", sku="V-1", title="V-1",
                product_url="https://example.com/v-1", sale_price=0,
                currency="", category_path="", image_urls=[]),
    ])
    db.commit()

    out = _field_fix_template_payload(db, limit=10)

    assert out["deferred_sites"] == ["vidaxl_ca", "vidaxl_us"]
    assert [item["site"] for item in out["items"]] == ["homary_us"]
    assert "title_weak" in out["items"][0]["note"]
    assert "category_missing" in out["items"][0]["note"]


def test_product_field_fixes_import_updates_product_and_acceptance():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(WorkspaceSite(workspace_id=1, site="homary_us",
                         enabled=True, hidden=False))
    db.add(Product(site="homary_us", sku="H-1", title="H-1",
                   product_url="https://example.com/h-1", sale_price=0,
                   currency="", category_path="", image_urls=[],
                   thirty_day_sales=1, thirty_day_revenue=50))
    db.add(Promotion(site="homary_us", sku="H-1",
                     promotion_type="coupon", promotion_name="Coupon"))
    db.commit()

    validation = _validate_field_fix_rows(db, [{
        "site": "homary_us",
        "sku": "H-1",
        "title": "Complete Homary Chair",
        "currency": "usd",
        "category_path": "Outdoor/Chairs",
        "image_urls": "https://example.com/h-1.jpg|https://example.com/h-1b.jpg",
        "sale_price": "50",
    }])
    assert validation["valid"] is True
    out = admin_product_field_fixes_import({
        "rows": [{
            "site": "homary_us",
            "sku": "H-1",
            "title": "Complete Homary Chair",
            "currency": "usd",
            "category_path": "Outdoor/Chairs",
            "image_urls": "https://example.com/h-1.jpg|https://example.com/h-1b.jpg",
            "sale_price": "50",
        }]
    }, user="admin", db=db)

    product = db.query(Product).filter(Product.site == "homary_us").one()
    acceptance = admin_aosen_field_quality_acceptance(user="admin", db=db)

    assert out["rows"] == 1
    assert product.title == "Complete Homary Chair"
    assert product.currency == "USD"
    assert product.category_path == "Outdoor/Chairs"
    assert product.image_urls == [
        "https://example.com/h-1.jpg",
        "https://example.com/h-1b.jpg",
    ]
    assert product.sale_price == 50
    assert acceptance["summary"]["pass"] == 1


def test_field_quality_flags_missing_category_and_image():
    db = _session()
    site = Site(site="homary_us", brand="Homary", country="US")
    db.add(site)
    product = Product(site="homary_us", sku="H-2", title="Dining Chair",
                      product_url="https://example.com/h-2",
                      sale_price=50, currency="USD",
                      category_path="", image_urls=[])
    db.add(product)
    db.commit()

    out = _build_data_quality_payload(db, [site])
    row = out["items"][0]

    assert "category_missing" in row["issues"]
    assert "image_missing" in row["issues"]
    assert row["category_missing_count"] == 1
    assert row["image_missing_count"] == 1
    assert row["category_signal_pct"] == 0
    assert row["image_signal_pct"] == 0
    assert out["summary"]["missing_categories"] == 1
    assert out["summary"]["missing_images"] == 1
    assert "category_missing" in _product_quality_issues(product)
    assert "image_missing" in _product_quality_issues(product)


def test_data_quality_products_lists_skus_with_insufficient_review_history():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Complete Homary Chair",
                   product_url="https://example.com/h-1", sale_price=50,
                   currency="USD", category_path="Outdoor", image_urls=["h.jpg"],
                   review_count=12, thirty_day_sales=1, thirty_day_revenue=50))
    db.add(PriceHistory(site="homary_us", sku="H-1", date=date(2026, 6, 28),
                        sale_price=50, review_count=12))
    db.commit()

    out = admin_data_quality_products(
        "homary_us",
        issue="sales_history_insufficient",
        user="admin",
        db=db,
    )

    assert out["kind"] == "product"
    assert out["total"] == 1
    assert out["issue_counts"]["sales_history_insufficient"] == 1
    assert out["items"][0]["sku"] == "H-1"
    assert "sales_history_insufficient" in out["items"][0]["issues"]


def test_data_quality_products_excludes_skus_with_two_review_snapshots():
    db = _session()
    db.add(Site(site="homary_us", brand="Homary", country="US"))
    db.add(Product(site="homary_us", sku="H-1", title="Complete Homary Chair",
                   product_url="https://example.com/h-1", sale_price=50,
                   currency="USD", category_path="Outdoor", image_urls=["h.jpg"],
                   review_count=14, thirty_day_sales=1, thirty_day_revenue=50))
    db.add_all([
        PriceHistory(site="homary_us", sku="H-1", date=date(2026, 6, 1),
                     sale_price=50, review_count=10),
        PriceHistory(site="homary_us", sku="H-1", date=date(2026, 6, 28),
                     sale_price=50, review_count=14),
    ])
    db.commit()

    out = admin_data_quality_products(
        "homary_us",
        issue="sales_history_insufficient",
        user="admin",
        db=db,
    )

    assert out["total"] == 0
    assert out["issue_counts"]["sales_history_insufficient"] == 0


def test_aosen_acceptance_separates_promotion_refresh_from_business_data():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="promo_gap_us", brand="PromoGap", country="US"),
        Site(site="sales_gap_us", brand="SalesGap", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="promo_gap_us",
                      enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="sales_gap_us",
                      enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="promo_gap_us", sku="P-1", title="Complete Promo Product",
                product_url="https://example.com/p-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["p.jpg"],
                thirty_day_sales=1, thirty_day_revenue=50),
        Product(site="sales_gap_us", sku="S-1", title="Complete Sales Product",
                product_url="https://example.com/s-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["s.jpg"],
                review_count=12),
    ])
    db.add(Promotion(site="sales_gap_us", sku="S-1",
                     promotion_type="coupon", promotion_name="Summer coupon"))
    db.commit()

    out = admin_aosen_field_quality_acceptance(user="admin", db=db)
    by_site = {item["site"]: item for item in out["items"]}

    assert by_site["promo_gap_us"]["status"] == "needs_refresh"
    assert by_site["promo_gap_us"]["issues"] == ["promotions_missing"]
    assert by_site["sales_gap_us"]["status"] == "needs_business_data"
    assert "sales_missing" in by_site["sales_gap_us"]["issues"]
    assert out["summary"]["needs_refresh"] == 1
    assert out["summary"]["needs_business_data"] == 1


def test_aosen_acceptance_uses_lightweight_metrics_path(monkeypatch):
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add(Site(site="complete_us", brand="Complete", country="US"))
    db.add(WorkspaceSite(workspace_id=1, site="complete_us",
                         enabled=True, hidden=False))
    db.add(Product(site="complete_us", sku="C-1", title="Complete Product",
                   product_url="https://example.com/c-1", sale_price=50,
                   currency="USD", category_path="Outdoor", image_urls=["c.jpg"],
                   thirty_day_sales=1, thirty_day_revenue=50))
    db.add(Promotion(site="complete_us", sku="C-1",
                     promotion_type="coupon", promotion_name="Complete coupon"))
    db.commit()

    def fail_if_global_quality_builder_is_used(*args, **kwargs):
        raise AssertionError("Aosen acceptance should not use global data-quality builder")

    monkeypatch.setattr(
        "app.api.admin_spine._build_data_quality_payload",
        fail_if_global_quality_builder_is_used,
    )

    out = admin_aosen_field_quality_acceptance(user="admin", db=db)

    assert out["summary"]["pass"] == 1
    assert out["items"][0]["status"] == "pass"


def test_aosen_acceptance_treats_empty_and_low_coverage_sites_as_field_failures():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="empty_us", brand="Empty", country="US"),
        Site(site="coverage_gap_us", brand="CoverageGap", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="empty_us",
                      enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="coverage_gap_us",
                      enabled=True, hidden=False, target_sku_count=10),
    ])
    db.add(Product(site="coverage_gap_us", sku="CG-1",
                   title="Coverage Gap Product",
                   product_url="https://example.com/cg-1", sale_price=50,
                   currency="USD", category_path="Outdoor", image_urls=["cg.jpg"],
                   thirty_day_sales=1, thirty_day_revenue=50))
    db.add(Promotion(site="coverage_gap_us", sku="CG-1",
                     promotion_type="coupon", promotion_name="Coverage coupon"))
    db.commit()

    out = admin_aosen_field_quality_acceptance(user="admin", db=db)
    by_site = {item["site"]: item for item in out["items"]}

    assert by_site["empty_us"]["status"] == "fail"
    assert "no_products" in by_site["empty_us"]["issues"]
    assert by_site["coverage_gap_us"]["status"] == "fail"
    assert "coverage_low" in by_site["coverage_gap_us"]["issues"]
    assert "sku_deviation_high" in by_site["coverage_gap_us"]["issues"]
    assert out["summary"]["fail"] == 2
    assert out["summary"]["no_products"] == 1
    assert out["summary"]["coverage_low"] == 1


def test_sku_target_template_excludes_deferred_and_import_updates_workspace_site():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="coverage_gap_us", brand="CoverageGap", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="coverage_gap_us",
                      enabled=True, hidden=False, target_sku_count=10),
        WorkspaceSite(workspace_id=1, site="vidaxl_us",
                      enabled=True, hidden=False, target_sku_count=10),
    ])
    db.add_all([
        Product(site="coverage_gap_us", sku="CG-1",
                title="Complete Coverage Product",
                product_url="https://example.com/cg-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["cg.jpg"],
                thirty_day_sales=1, thirty_day_revenue=50),
        Product(site="vidaxl_us", sku="V-1",
                title="Deferred Product",
                product_url="https://example.com/v-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["v.jpg"]),
    ])
    db.add(Promotion(site="coverage_gap_us", sku="CG-1",
                     promotion_type="coupon", promotion_name="Coverage coupon"))
    db.commit()

    template = _sku_target_template_payload(db, limit=10)

    assert template["deferred_sites"] == ["vidaxl_ca", "vidaxl_us"]
    assert [item["site"] for item in template["items"]] == ["coverage_gap_us"]
    assert template["items"][0]["current_target_sku_count"] == 10
    assert template["items"][0]["observed_sku_count"] == 1

    validation = _validate_sku_target_rows(db, [{
        "site": "coverage_gap_us",
        "workspace_id": 1,
        "target_sku_count": "1",
        "note": "client accepted observed SKU count",
    }])
    assert validation["valid"] is True

    out = admin_sku_targets_import({
        "rows": [{
            "site": "coverage_gap_us",
            "workspace_id": 1,
            "target_sku_count": 1,
            "note": "client accepted observed SKU count",
        }],
    }, user="admin", db=db, ip="")

    row = (db.query(WorkspaceSite)
           .filter(WorkspaceSite.site == "coverage_gap_us")
           .one())
    assert out["rows"] == 1
    assert row.target_sku_count == 1
    assert row.report_config["target_sku_count_source"] == "aosen_import"


def test_aosen_action_plan_groups_gaps_and_excludes_deferred_sites():
    db = _session()
    db.add(Workspace(id=1, name="Aosen", slug="aosen", status="active"))
    db.add_all([
        Site(site="field_gap_us", brand="FieldGap", country="US"),
        Site(site="promo_gap_us", brand="PromoGap", country="US"),
        Site(site="sales_gap_us", brand="SalesGap", country="US"),
        Site(site="vidaxl_us", brand="Vidaxl", country="US"),
    ])
    db.add_all([
        WorkspaceSite(workspace_id=1, site="field_gap_us",
                      enabled=True, hidden=False, target_sku_count=10),
        WorkspaceSite(workspace_id=1, site="promo_gap_us",
                      enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="sales_gap_us",
                      enabled=True, hidden=False),
        WorkspaceSite(workspace_id=1, site="vidaxl_us",
                      enabled=True, hidden=False),
    ])
    db.add_all([
        Product(site="field_gap_us", sku="F-1", title="F-1",
                product_url="https://example.com/f-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["f.jpg"],
                thirty_day_sales=1, thirty_day_revenue=50),
        Product(site="promo_gap_us", sku="P-1", title="Complete Promo Product",
                product_url="https://example.com/p-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["p.jpg"],
                thirty_day_sales=1, thirty_day_revenue=50),
        Product(site="sales_gap_us", sku="S-1", title="Complete Sales Product",
                product_url="https://example.com/s-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["s.jpg"],
                review_count=12),
        Product(site="vidaxl_us", sku="V-1", title="Deferred Product",
                product_url="https://example.com/v-1", sale_price=50,
                currency="USD", category_path="Outdoor", image_urls=["v.jpg"]),
    ])
    db.add(Promotion(site="field_gap_us", sku="F-1",
                     promotion_type="coupon", promotion_name="Field coupon"))
    db.add(Promotion(site="sales_gap_us", sku="S-1",
                     promotion_type="coupon", promotion_name="Sales coupon"))
    db.commit()

    out = admin_aosen_acceptance_action_plan(user="admin", db=db)

    assert out["status"] == "blocked"
    assert out["verification_source"] == "runtime_database"
    assert out["final_acceptance_scope"] == "production"
    assert out["groups"]["field_fixes"]["sites"] == ["field_gap_us"]
    assert out["groups"]["promotion_refresh"]["sites"] == ["promo_gap_us"]
    assert out["groups"]["business_data"]["sites"] == ["sales_gap_us"]
    assert out["templates"]["product_field_fixes"]["total_count"] is None
    assert out["templates"]["product_field_fixes"]["items"][0]["site"] == "field_gap_us"
    assert out["templates"]["sku_targets"]["total_count"] is None
    assert out["templates"]["sku_targets"]["has_more"] is False
    assert out["templates"]["sku_targets"]["items"][0]["site"] == "field_gap_us"
    assert out["templates"]["promotion_signals"]["total_count"] is None
    assert out["templates"]["promotion_signals"]["has_more"] is False
    assert out["templates"]["promotion_signals"]["items"][0]["site"] == "promo_gap_us"
    assert out["templates"]["sales_signals"]["total_count"] is None
    assert out["templates"]["sales_signals"]["has_more"] is False
    assert out["templates"]["sales_signals"]["items"][0]["site"] == "sales_gap_us"
    assert out["templates"]["review_history"]["total_count"] is None
    assert out["templates"]["review_history"]["has_more"] is False
    assert out["templates"]["review_history"]["items"][0]["site"] == "sales_gap_us"
    all_sites = {
        site
        for group in out["groups"].values()
        for site in group["sites"]
    }
    assert "vidaxl_us" not in all_sites
