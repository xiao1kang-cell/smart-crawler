from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.db import Base
from app.models import (Product, Site, User, Workspace, WorkspaceMember,
                        WorkspaceSite)

pytestmark = pytest.mark.unit


def _session():
    from app.api.routes import _COVERAGE_CACHE
    _COVERAGE_CACHE.clear()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def _workspace(db, name, slug):
    row = Workspace(name=name, slug=slug, type="customer", status="active")
    db.add(row); db.flush(); return row


def _user(db, username, workspace):
    row = User(username=username, email=f"{username}@example.com",
               password_hash=hash_password("Password1"), role="user",
               status="active", default_workspace_id=workspace.id)
    db.add(row); db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=row.id,
                           role="member", status="active"))
    db.flush(); return row


def _site(db, code, brand="B"):
    row = Site(site=code, brand=brand, country="US",
               url=f"https://{code}.example.com", platform="generic")
    db.add(row); db.flush(); return row


def _workspace_site(db, workspace, site):
    row = WorkspaceSite(workspace_id=workspace.id, site=site,
                        display_name=site, enabled=True, hidden=False,
                        sort_order=0)
    db.add(row); db.flush(); return row


def _prod(db, site, sku, spu):
    db.add(Product(site=site, sku=sku, spu=spu, title=sku, sale_price=1.0,
                   status="on_sale"))


def test_spu_count_dedups_variants_and_coalesces_null():
    db = _session()
    ws = _workspace(db, "W1", "w1")
    _user(db, "alice", ws)
    # site v: 3 SKU、2 个共享 spu=P1 → spu_count 2
    _site(db, "v"); _workspace_site(db, ws, "v")
    _prod(db, "v", "v-1", "P1")
    _prod(db, "v", "v-2", "P1")
    _prod(db, "v", "v-3", "P2")
    # site n: 2 SKU 均 spu=None → coalesce(sku) 兜底 → spu_count 2
    _site(db, "n"); _workspace_site(db, ws, "n")
    _prod(db, "n", "n-1", None)
    _prod(db, "n", "n-2", None)
    db.commit()

    from app.api.routes import list_sites
    rows = list_sites(user="alice", x_workspace_id=str(ws.id), db=db)
    by = {r["site"]: r for r in rows}
    assert by["v"]["sku_count"] == 3
    assert by["v"]["spu_count"] == 2
    assert by["n"]["sku_count"] == 2
    assert by["n"]["spu_count"] == 2
