from __future__ import annotations

import pytest
from fastapi import HTTPException
from datetime import date, datetime, timedelta
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.apikey import hash_key
from app.auth import hash_password, hash_secret, make_token
from app.db import Base
from app.models import (ApiKey, CrawlJob, InviteCode, PriceHistory, Product,
                        Promotion, Review, Site, User, Workspace,
                        WorkspaceMember, WorkspaceSite, Trend)


pytestmark = pytest.mark.unit


def _session():
    from app.api.routes import _COVERAGE_CACHE

    _COVERAGE_CACHE.clear()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def _workspace(db, name: str, slug: str) -> Workspace:
    row = Workspace(name=name, slug=slug, type="customer", status="active")
    db.add(row)
    db.flush()
    return row


def _user(db, username: str, workspace: Workspace, *,
          role: str = "user", global_role: str | None = None) -> User:
    row = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("Password1"),
        role=role,
        global_role=global_role,
        status="active",
        default_workspace_id=workspace.id,
    )
    db.add(row)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=row.id,
                           role="owner" if role == "admin" else "member",
                           status="active"))
    db.flush()
    return row


def _site(db, code: str, brand: str) -> Site:
    row = Site(site=code, brand=brand, country="US",
               url=f"https://{code}.example.com", platform="generic")
    db.add(row)
    db.flush()
    return row


def _workspace_site(db, workspace: Workspace, site: str, *,
                    enabled: bool = True) -> WorkspaceSite:
    row = WorkspaceSite(workspace_id=workspace.id, site=site,
                        display_name=site, enabled=enabled,
                        hidden=False, sort_order=0)
    db.add(row)
    db.flush()
    return row


def _product(db, site: str, sku: str) -> Product:
    row = Product(site=site, brand=site.split("_", 1)[0], sku=sku,
                  title=f"{sku} title", sale_price=10.0,
                  category_path="Storage", status="on_sale")
    db.add(row)
    db.flush()
    return row


def _seed_two_workspaces(db):
    ws_a = _workspace(db, "Workspace A", "workspace-a")
    ws_b = _workspace(db, "Workspace B", "workspace-b")
    _site(db, "site_a", "A")
    _site(db, "site_b", "B")
    _workspace_site(db, ws_a, "site_a")
    _workspace_site(db, ws_b, "site_b")
    alice = _user(db, "alice", ws_a)
    bob = _user(db, "bob", ws_b)
    admin = _user(db, "admin", ws_a, role="admin",
                  global_role="super_admin")
    _product(db, "site_a", "A-1")
    _product(db, "site_b", "B-1")
    db.add(Promotion(site="site_b", sku="B-1", promotion_type="coupon",
                     promotion_name="B promo"))
    db.commit()
    return ws_a, ws_b, alice, bob, admin


def test_workspace_views_filter_global_warehouse_by_enabled_sites():
    from app.api.routes import list_products, list_sites

    db = _session()
    ws_a, ws_b, alice, bob, _admin = _seed_two_workspaces(db)

    alice_sites = list_sites(user="alice", x_workspace_id=str(ws_a.id), db=db)
    assert [s["site"] for s in alice_sites] == ["site_a"]
    alice_products = list_products(user="alice", x_workspace_id=str(ws_a.id),
                                   db=db)
    assert [p["sku"] for p in alice_products["items"]] == ["A-1"]

    bob_products = list_products(user="bob", x_workspace_id=str(ws_b.id), db=db)
    assert [p["sku"] for p in bob_products["items"]] == ["B-1"]

    with pytest.raises(HTTPException) as exc:
        list_products(site="site_b", user="alice",
                      x_workspace_id=str(ws_a.id), db=db)
    assert exc.value.status_code == 404
    assert db.query(Product).count() == 2
    assert not hasattr(Product, "workspace_id")


def test_members_can_switch_only_to_joined_workspaces():
    from app.api.routes import list_sites

    db = _session()
    ws_a, ws_b, alice, bob, _admin = _seed_two_workspaces(db)
    db.add(WorkspaceMember(workspace_id=ws_b.id, user_id=alice.id,
                           role="viewer", status="active"))
    db.commit()

    switched = list_sites(user="alice", x_workspace_id=str(ws_b.id), db=db)
    assert [s["site"] for s in switched] == ["site_b"]

    with pytest.raises(HTTPException) as exc:
        list_sites(user="bob", x_workspace_id=str(ws_a.id), db=db)
    assert exc.value.status_code == 403


def test_disabled_membership_cannot_fall_back_to_default_workspace():
    from app.api.routes import list_sites

    db = _session()
    ws_a, _ws_b, alice, _bob, _admin = _seed_two_workspaces(db)
    member = (db.query(WorkspaceMember)
              .filter(WorkspaceMember.workspace_id == ws_a.id,
                      WorkspaceMember.user_id == alice.id)
              .first())
    member.status = "disabled"
    db.commit()

    sites = list_sites(user="alice", db=db)
    assert sites == []


def test_tracking_write_permissions_use_workspace_role():
    from app.api.tracking import add_tracking

    db = _session()
    ws = _workspace(db, "Workspace A", "workspace-a")
    owner = _user(db, "owner_user", ws, role="user")
    admin_account = _user(db, "admin_account", ws, role="admin")
    owner_member = (db.query(WorkspaceMember)
                    .filter(WorkspaceMember.workspace_id == ws.id,
                            WorkspaceMember.user_id == owner.id)
                    .first())
    admin_member = (db.query(WorkspaceMember)
                    .filter(WorkspaceMember.workspace_id == ws.id,
                            WorkspaceMember.user_id == admin_account.id)
                    .first())
    owner_member.role = "owner"
    admin_member.role = "member"
    db.commit()

    with patch("app.api.tracking.detect_platform",
               return_value=("shopify", "https://newbrand.example.com")), \
         patch("app.api.tracking.enqueue", return_value=1):
        created = add_tracking(
            {"url": "https://newbrand.example.com/x", "brand": "NewBrand", "country": "US"},
            user="owner_user",
            x_workspace_id=str(ws.id),
            db=db,
        )
    assert created["source"] == "user"
    assert created["creator"] == "owner_user"

    with pytest.raises(HTTPException) as exc:
        add_tracking(
            {"url": "https://blocked.example.com"},
            user="admin_account",
            x_workspace_id=str(ws.id),
            db=db,
        )
    assert exc.value.status_code == 403


def test_super_admin_can_manage_cross_workspace_api_keys_and_usage_views():
    from app.api.routes import create_key, list_keys

    db = _session()
    ws_a, ws_b, _alice, _bob, admin = _seed_two_workspaces(db)

    created = create_key({"name": "workspace-b-key",
                          "workspace_id": ws_b.id,
                          "scopes": ["crawler:read"]},
                         user="admin", x_workspace_id=str(ws_a.id), db=db)

    assert created["workspace_id"] == ws_b.id
    assert [k["name"] for k in list_keys(user="admin",
                                         x_workspace_id=str(ws_a.id),
                                         db=db)] == []
    assert [k["name"] for k in list_keys(user="admin",
                                         x_workspace_id=str(ws_b.id),
                                         db=db)] == ["workspace-b-key"]

    stored = db.get(ApiKey, created["id"])
    stored.key_hash = hash_key("sck_workspace_b")
    db.commit()


def test_null_workspace_api_keys_are_not_listed_after_tenant_cutover():
    from app.api.routes import billing_usage, list_keys

    db = _session()
    ws_a, _ws_b, _alice, _bob, _admin = _seed_two_workspaces(db)
    db.add(ApiKey(name="legacy-null", key_prefix="sck_null",
                  key_hash=hash_key("sck_null_secret"), active=True,
                  workspace_id=None))
    db.commit()

    assert list_keys(user="admin", x_workspace_id=str(ws_a.id), db=db) == []
    assert billing_usage(user="admin", x_workspace_id=str(ws_a.id),
                         db=db)["keys"] == []


def test_admin_cannot_assign_api_key_owner_outside_workspace():
    from app.api.routes import create_key, update_key

    db = _session()
    ws_a, ws_b, alice, bob, _admin = _seed_two_workspaces(db)

    with pytest.raises(HTTPException) as exc:
        create_key({"name": "bad-owner", "owner_user_id": bob.id},
                   user="admin", x_workspace_id=str(ws_a.id), db=db)
    assert exc.value.status_code == 400

    created = create_key({"name": "alice-key", "owner_user_id": alice.id},
                         user="admin", x_workspace_id=str(ws_a.id), db=db)
    with pytest.raises(HTTPException) as exc:
        update_key(created["id"], {"workspace_id": ws_b.id},
                   user="admin", x_workspace_id=str(ws_a.id), db=db)
    assert exc.value.status_code == 400


def test_invite_registration_joins_invite_workspace():
    from app.api.routes import auth_register

    db = _session()
    ws_a = _workspace(db, "Workspace A", "workspace-a")
    ws_b = _workspace(db, "Workspace B", "workspace-b")
    _user(db, "admin", ws_a, role="admin", global_role="super_admin")
    raw_code = "workspace-b-invite"
    db.add(InviteCode(code_prefix=raw_code[:10],
                      code_hash=hash_secret(raw_code),
                      workspace_id=ws_b.id,
                      max_uses=1,
                      used_count=0,
                      active=True,
                      default_role="user"))
    db.commit()

    registered = auth_register({
        "username": "carol",
        "email": "carol@example.com",
        "password": "Password1",
        "confirm_password": "Password1",
        "invite_code": raw_code,
    }, request=None, db=db)

    carol = db.query(User).filter(User.username == "carol").first()
    assert registered["username"] == "carol"
    assert carol.default_workspace_id == ws_b.id
    assert (db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == ws_b.id,
                    WorkspaceMember.user_id == carol.id)
                    .count()) == 1


def test_token_export_preview_honors_requested_workspace_and_site_scoped_details():
    from app.api.routes import export_preview

    db = _session()
    ws_a, ws_b, _alice, _bob, admin = _seed_two_workspaces(db)
    db.add(PriceHistory(site="site_a", sku="SHARED", date=date.today(),
                        sale_price=10, original_price=12))
    db.add(PriceHistory(site="site_b", sku="SHARED", date=date.today(),
                        sale_price=20, original_price=24))
    db.add(Review(platform="trustpilot", site="site_a", sku="SHARED",
                  review_id="ra", review_date=datetime.utcnow(),
                  content="A"))
    db.add(Review(platform="trustpilot", site="site_b", sku="SHARED",
                  review_id="rb", review_date=datetime.utcnow(),
                  content="B"))
    _product(db, "site_a", "SHARED")
    _product(db, "site_b", "SHARED")
    db.commit()
    token = make_token(admin.username)

    preview_a = export_preview(token=token, workspace_id=ws_a.id,
                               include_price_history=True, include_voc=True,
                               db=db)
    preview_b = export_preview(token=token, workspace_id=ws_b.id,
                               include_price_history=True, include_voc=True,
                               db=db)

    assert preview_a["sku_count"] == 2
    assert preview_b["sku_count"] == 2
    assert preview_a["price_history_rows"] == 1
    assert preview_b["price_history_rows"] == 1
    assert preview_a["review_count"] == 1
    assert preview_b["review_count"] == 1

    with pytest.raises(HTTPException) as exc:
        export_preview(token=token, site="site_b", workspace_id=ws_a.id, db=db)
    assert exc.value.status_code == 404


def test_revoked_session_token_cannot_use_public_export_preview():
    from app.api.routes import auth_login, auth_logout, export_preview

    db = _session()
    ws_a, _ws_b, _alice, _bob, _admin = _seed_two_workspaces(db)
    logged_in = auth_login({"identifier": "admin", "password": "Password1"},
                           request=None, db=db)

    auth_logout(authorization=f"Bearer {logged_in['token']}", db=db)

    with pytest.raises(HTTPException) as exc:
        export_preview(token=logged_in["token"], workspace_id=ws_a.id, db=db)
    assert exc.value.status_code == 401


def test_export_builders_do_not_leak_same_sku_across_sites():
    from app.export import price_history_df, reviews_voc_df, sites_overview_df

    db = _session()
    _ws_a, _ws_b, _alice, _bob, _admin = _seed_two_workspaces(db)
    _product(db, "site_a", "SHARED")
    _product(db, "site_b", "SHARED")
    db.add(PriceHistory(site="site_a", sku="SHARED", date=date.today(),
                        sale_price=10, original_price=12))
    db.add(PriceHistory(site="site_b", sku="SHARED", date=date.today(),
                        sale_price=20, original_price=24))
    db.add(Review(platform="trustpilot", site="site_a", sku="SHARED",
                  review_id="ra", review_date=datetime.utcnow(),
                  content="A"))
    db.add(Review(platform="trustpilot", site="site_b", sku="SHARED",
                  review_id="rb", review_date=datetime.utcnow(),
                  content="B"))
    db.commit()

    assert set(price_history_df(db, ["site_a"])["site"]) == {"site_a"}
    assert set(reviews_voc_df(db, ["site_a"])["site"]) == {"site_a"}
    assert set(sites_overview_df(db, ["site_a"])["site"]) == {"site_a"}


def test_site_overview_aggregates_trends_by_granularity_and_period():
    from app.api.routes import site_overview

    db = _session()
    ws_a, _ws_b, _alice, _bob, admin = _seed_two_workspaces(db)
    db.add_all([
        Trend(site="site_a", date=date(2026, 5, 30), sku_count=10,
              new_product_count=1, estimated_sales=20,
              estimated_revenue=200, avg_rating=4.1, review_total=100),
        Trend(site="site_a", date=date(2026, 5, 31), sku_count=12,
              new_product_count=2, estimated_sales=30,
              estimated_revenue=300, avg_rating=4.2, review_total=120),
        Trend(site="site_a", date=date(2026, 6, 15), sku_count=20,
              new_product_count=3, estimated_sales=50,
              estimated_revenue=500, avg_rating=4.4, review_total=180),
    ])
    db.commit()

    monthly = site_overview("site_a", user=admin.username,
                            x_workspace_id=str(ws_a.id), db=db,
                            granularity="month")
    assert [row["date"] for row in monthly["trends"]] == ["2026-05", "2026-06"]
    assert monthly["trends"][0]["source_date"] == "2026-05-31"
    assert monthly["trend_summary"]["current_period"]["estimated_sales"] == 50
    assert monthly["trend_summary"]["previous_period"]["estimated_sales"] == 30

    filtered = site_overview("site_a", user=admin.username,
                             x_workspace_id=str(ws_a.id), db=db,
                             granularity="week",
                             date_from="2026-06-01", date_to="2026-06-30")
    assert len(filtered["trends"]) == 1
    assert filtered["trends"][0]["date"].startswith("2026-W")
    assert filtered["trend_summary"]["visible_points"] == 1


def test_site_overview_exposes_currency_and_real_update_time():
    from app.api.routes import site_overview

    db = _session()
    ws_a, _ws_b, _alice, _bob, admin = _seed_two_workspaces(db)
    _site(db, "songmics_de", "Songmics")
    _workspace_site(db, ws_a, "songmics_de")
    product = _product(db, "songmics_de", "DE-1")
    product.thirty_day_revenue = 99.5
    product.updated_time = datetime(2026, 6, 15, 10, 30)
    db.commit()

    out = site_overview("songmics_de", user=admin.username,
                        x_workspace_id=str(ws_a.id), db=db)
    assert out["currency"] == "EUR"
    assert out["cards"]["currency"] == "EUR"
    assert out["updated_at"] == "2026-06-15T10:30:00"


def test_product_filters_and_export_rows_share_same_scope():
    from app.api.routes import _filtered_products_query, _product_order_cols
    from app.export import products_sample_df_from_rows

    db = _session()
    ws_a, _ws_b, _alice, _bob, _admin = _seed_two_workspaces(db)
    matching = _product(db, "site_a", "MATCH-1")
    matching.spu = "PARENT-1"
    matching.title = "Matching storage cabinet"
    matching.sale_price = 20
    matching.original_price = 30
    matching.category_path = "Garage / Cabinet"
    matching.ratings = 4.8
    matching.review_count = 120
    matching.thirty_day_sales = 12
    matching.thirty_day_revenue = 240
    matching.has_video = True
    matching.has_free_shipping = True
    matching.created_time = datetime(2026, 6, 1)
    sibling = _product(db, "site_a", "MATCH-2")
    sibling.spu = "PARENT-1"
    sibling.title = "Matching storage cabinet variant"
    sibling.sale_price = 21
    sibling.original_price = 31
    sibling.category_path = "Garage / Cabinet"
    sibling.ratings = 4.9
    sibling.review_count = 150
    sibling.thirty_day_sales = 15
    sibling.thirty_day_revenue = 315
    sibling.has_video = True
    sibling.has_free_shipping = True
    sibling.created_time = datetime(2026, 6, 2)
    ignored = _product(db, "site_a", "MISS-1")
    ignored.title = "Outdoor bench"
    ignored.sale_price = 8
    ignored.category_path = "Garden"
    ignored.ratings = 3.2
    db.commit()

    q = _filtered_products_query(
        db, ["site_a"], site="site_a", search="cabinet",
        category="Garage", min_price=10, min_rating=4.5,
        min_reviews=100, min_sales=10, min_revenue=200,
        min_variants=2, max_variants=2,
        has_video=True, free_shipping=True, created_from="2026-05-01",
    )
    rows = q.order_by(*_product_order_cols("all")).all()
    df = products_sample_df_from_rows(rows)

    assert [p.sku for p in rows] == ["MATCH-1", "MATCH-2"]
    assert df["Sku"].tolist() == ["MATCH-1", "MATCH-2"]
    assert df["Sale Price"].tolist() == [20, 21]
    assert df["Free shipping"].tolist() == ["YES", "YES"]


def test_promotion_filters_and_export_rows_share_same_scope():
    from app.api.routes import _filtered_promotions_query
    from app.export import promotions_sample_df_from_rows

    db = _session()
    _ws_a, _ws_b, _alice, _bob, _admin = _seed_two_workspaces(db)
    db.add(Promotion(site="site_a", sku="PROMO-1",
                     promotion_type="coupon",
                     promotion_name="Cabinet coupon",
                     product_title="Storage Cabinet",
                     original_price=40,
                     promotion_price=30,
                     detected_time=datetime(2026, 6, 2)))
    db.add(Promotion(site="site_a", sku="PROMO-2",
                     promotion_type="bundle",
                     promotion_name="Bench bundle",
                     product_title="Outdoor Bench",
                     detected_time=datetime(2026, 6, 3)))
    db.commit()

    q = _filtered_promotions_query(
        db, ["site_a"], site="site_a", search="cabinet",
        type="coupon", date_from="2026-06-01", date_to="2026-06-30",
    )
    rows = q.order_by(Promotion.detected_time.desc().nullslast(),
                      Promotion.id.desc()).all()
    df = promotions_sample_df_from_rows(rows)

    assert [p.sku for p in rows] == ["PROMO-1"]
    assert df["SKU"].tolist() == ["PROMO-1"]
    assert df["Type"].tolist() == ["coupon"]


def test_data_quality_surfaces_site_level_acceptance_gaps():
    from app.api.routes import data_quality

    db = _session()
    ws_a, ws_b, _alice, _bob, admin = _seed_two_workspaces(db)
    p = db.query(Product).filter(Product.site == "site_a", Product.sku == "A-1").first()
    p.sale_price = None
    p.original_price = None
    p.thirty_day_sales = 0
    p.thirty_day_revenue = 0
    db.add(CrawlJob(site="site_a", status="failed",
                    requested_by_workspace_id=ws_a.id,
                    failure_code="parse_none",
                    failure_stage="parse",
                    suggested_action="检查解析规则",
                    created_at=datetime(2026, 6, 1),
                    finished_at=datetime(2026, 6, 1, 0, 1)))
    db.commit()

    out = data_quality(user=admin.username, x_workspace_id=str(ws_a.id), db=db)
    rows = {row["site"]: row for row in out["items"]}

    assert set(rows) == {"site_a"}
    assert rows["site_a"]["status"] == "warning"
    assert "price_missing" in rows["site_a"]["issues"]
    assert "sales_missing" in rows["site_a"]["issues"]
    assert "revenue_missing" in rows["site_a"]["issues"]
    assert "promotions_missing" in rows["site_a"]["issues"]
    assert "latest_job_failed" in rows["site_a"]["issues"]
    assert rows["site_a"]["latest_job"]["failure_code"] == "parse_none"
    assert out["summary"]["needs_rerun"] == 0
    assert out["summary"]["missing_prices"] == 1
    assert out["summary"]["missing_sales"] == 1
    assert rows["site_a"]["price_signal_count"] == 0
    assert rows["site_a"]["price_signal_pct"] == 0
    assert rows["site_a"]["crawl_queue"]["failed"] == 1
    assert out["summary"]["failed_jobs"] == 1
    assert "价格解析" in rows["site_a"]["suggested_action"]

    p.sale_price = 10
    p.original_price = 12
    p.thirty_day_sales = 1
    p.thirty_day_revenue = 10
    promo = Promotion(site="site_a", sku="A-1", product_title="A-1 promo",
                      promotion_type="coupon", detected_time=datetime(2026, 6, 2))
    db.add(promo)
    db.commit()
    out_ok_data_failed_job = data_quality(user=admin.username,
                                          x_workspace_id=str(ws_a.id), db=db)
    row = out_ok_data_failed_job["items"][0]
    assert row["status"] == "warning"
    assert row["issues"] == ["latest_job_failed"]
    assert row["crawl_queue"]["failed"] == 1
    assert "最近任务失败" in row["suggested_action"]

    other = data_quality(user=admin.username, x_workspace_id=str(ws_b.id), db=db)
    assert {row["site"] for row in other["items"]} == {"site_b"}


def test_admin_data_quality_is_global_and_super_admin_only():
    from app.api.admin_spine import admin_data_quality

    db = _session()
    ws_a, ws_b, alice, _bob, admin = _seed_two_workspaces(db)
    _site(db, "site_empty", "E")
    _workspace_site(db, ws_a, "site_empty")
    db.add(CrawlJob(site="site_empty", status="failed",
                    requested_by_workspace_id=ws_a.id,
                    failure_code="zero_products",
                    failure_stage="parse",
                    created_at=datetime(2026, 6, 1),
                    finished_at=datetime(2026, 6, 1, 0, 1)))
    db.add(CrawlJob(site="site_a", status="pending",
                    requested_by_workspace_id=ws_a.id,
                    created_at=datetime.utcnow() - timedelta(hours=2)))
    db.add(CrawlJob(site="site_a", status="running",
                    requested_by_workspace_id=ws_a.id,
                    created_at=datetime.utcnow() - timedelta(hours=2),
                    started_at=datetime.utcnow() - timedelta(hours=2)))
    db.commit()

    out = admin_data_quality(user=admin.username, db=db)
    assert {row["site"] for row in out["items"]} == {"site_a", "site_b", "site_empty"}
    assert out["summary"]["workspace_count"] == 2
    site_a = next(row for row in out["items"] if row["site"] == "site_a")
    assert site_a["workspaces"] == [{"id": ws_a.id, "name": ws_a.name}]
    assert site_a["crawl_queue"]["pending"] == 1
    assert site_a["crawl_queue"]["running"] == 0
    assert site_a["crawl_queue"]["stuck"] == 1
    assert site_a["crawl_queue"]["active_count"] == 2
    assert site_a["crawl_queue"]["stale_pending"] == 1
    assert "job_pending_stale" in site_a["issues"]
    assert "排队超过30分钟" in site_a["suggested_action"]
    assert out["summary"]["pending_jobs"] == 1
    assert out["summary"]["stuck_jobs"] == 1
    assert out["summary"]["stale_pending_jobs"] == 1
    site_empty = next(row for row in out["items"] if row["site"] == "site_empty")
    assert site_empty["status"] == "critical"
    assert out["summary"]["needs_rerun"] == 1

    tenant_only = admin_data_quality(tenant=ws_b.id, user=admin.username, db=db)
    assert {row["site"] for row in tenant_only["items"]} == {"site_b"}
    assert tenant_only["summary"]["tenant_id"] == ws_b.id

    with pytest.raises(HTTPException) as exc:
        admin_data_quality(user=alice.username, db=db)
    assert exc.value.status_code == 403


def test_product_trend_is_workspace_scoped_and_returns_sales_promos():
    import asyncio
    import io
    from openpyxl import load_workbook
    from app.api.routes import export_product_trend, product_trend

    db = _session()
    ws_a, ws_b, _alice, _bob, admin = _seed_two_workspaces(db)
    site = db.query(Site).filter(Site.site == "site_a").first()
    site.review_rate = 0.05
    p = _product(db, "site_a", "TREND-SKU")
    p.sale_price = 11
    p.original_price = 15
    p.currency = "USD"
    p.review_count = 15
    p.thirty_day_sales = 100
    p.thirty_day_revenue = 1100
    db.add(PriceHistory(site="site_a", sku="TREND-SKU", date=date(2026, 6, 1),
                        sale_price=10, original_price=15, review_count=10))
    db.add(PriceHistory(site="site_a", sku="TREND-SKU", date=date(2026, 6, 15),
                        sale_price=11, original_price=15, review_count=15))
    db.add(Promotion(site="site_a", sku="TREND-SKU",
                     promotion_type="price_promotion",
                     promotion_name="Summer deal",
                     original_price=15, promotion_price=11,
                     discount_percent=27,
                     detected_time=datetime(2026, 6, 15)))
    db.add(Promotion(site="site_a", sku="TREND-SKU",
                     promotion_type="coupon",
                     promotion_name="Winter coupon",
                     original_price=15, promotion_price=12,
                     discount_percent=20,
                     detected_time=datetime(2026, 6, 16, 14, 30)))
    db.commit()

    out = product_trend(pid=p.id, user=admin.username,
                        x_workspace_id=str(ws_a.id), db=db)

    assert out["product"]["sku"] == "TREND-SKU"
    assert out["summary"]["has_review_signal"] is True
    assert out["summary"]["review_rate"] == 0.05
    assert out["trend"][1]["estimated_sales"] == 100
    assert out["trend"][1]["estimated_revenue"] == 1100
    assert {p["promotion_name"] for p in out["promotions"]} == {"Summer deal", "Winter coupon"}
    assert out["summary"]["current_period"]["estimated_sales"] == 100

    filtered = product_trend(pid=p.id, user=admin.username,
                             x_workspace_id=str(ws_a.id), db=db,
                             granularity="month", date_from="2026-06-01",
                             date_to="2026-06-16",
                             promo_search="winter", promo_type="coupon")
    assert len(filtered["trend"]) == 1
    assert filtered["trend"][0]["date"] == "2026-06"
    assert filtered["trend"][0]["estimated_sales"] == 100
    assert [p["promotion_name"] for p in filtered["promotions"]] == ["Winter coupon"]
    assert filtered["summary"]["granularity"] == "month"
    assert filtered["summary"]["visible_points"] == 1
    response = export_product_trend(token=make_token(admin.username, ""),
                                    pid=p.id, workspace_id=ws_a.id,
                                    db=db, granularity="month",
                                    promo_search="winter")

    async def _read_body(iterator):
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    content = asyncio.run(_read_body(response.body_iterator))
    wb = load_workbook(io.BytesIO(content), read_only=True)
    assert wb.sheetnames == ["Sales Trends", "Sales Promotion"]
    trend_headers = [c.value for c in next(wb["Sales Trends"].iter_rows(max_row=1))]
    promo_headers = [c.value for c in next(wb["Sales Promotion"].iter_rows(max_row=1))]
    assert trend_headers[:4] == ["Date", "Sales", "Revenue", "Ratings"]
    assert "Product Title" in promo_headers

    with pytest.raises(HTTPException) as exc:
        product_trend(pid=p.id, user=admin.username,
                      x_workspace_id=str(ws_b.id), db=db)
    assert exc.value.status_code == 404


def test_workspace_admin_user_and_invite_views_are_workspace_scoped():
    from app.api.routes import (admin_create_invite, admin_list_invites,
                                admin_list_users)

    db = _session()
    ws_a, ws_b, alice, bob, _admin = _seed_two_workspaces(db)
    tenant_admin = _user(db, "tenantadmin", ws_a, role="admin")
    db.add(InviteCode(code_prefix="a", code_hash=hash_secret("invite-a"),
                      workspace_id=ws_a.id, max_uses=1, used_count=0,
                      active=True, default_role="user"))
    db.add(InviteCode(code_prefix="b", code_hash=hash_secret("invite-b"),
                      workspace_id=ws_b.id, max_uses=1, used_count=0,
                      active=True, default_role="user"))
    db.commit()

    users = admin_list_users(user=tenant_admin.username,
                             x_workspace_id=str(ws_a.id), db=db)
    assert {u["username"] for u in users} == {"alice", "admin", "tenantadmin"}
    assert "bob" not in {u["username"] for u in users}

    invites = admin_list_invites(user=tenant_admin.username,
                                 x_workspace_id=str(ws_a.id), db=db)
    assert {i["workspace_id"] for i in invites} == {ws_a.id}

    created = admin_create_invite({"workspace_id": ws_b.id},
                                  user=tenant_admin.username,
                                  x_workspace_id=str(ws_a.id),
                                  db=db)
    assert created["workspace_id"] == ws_a.id
