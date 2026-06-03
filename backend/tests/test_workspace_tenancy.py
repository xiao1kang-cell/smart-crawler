from __future__ import annotations

import pytest
from fastapi import HTTPException
from datetime import date, datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.apikey import hash_key
from app.auth import hash_password, hash_secret, make_token
from app.db import Base
from app.models import (ApiKey, InviteCode, PriceHistory, Product, Promotion,
                        Review, Site, User, Workspace, WorkspaceMember,
                        WorkspaceSite)


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
