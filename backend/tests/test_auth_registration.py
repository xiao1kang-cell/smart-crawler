from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.apikey import hash_key
from app.auth import hash_password, hash_secret
from app.db import Base
from app.models import ApiKey, InviteCode, User


pytestmark = pytest.mark.unit


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def _add_user(db, username="admin", email="admin@example.com",
              role="admin", password="Password1"):
    row = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=role,
        status="active",
        display_name=username,
        email_verified=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_invite(db, code="invite-code", max_uses=1, role="user"):
    row = InviteCode(
        code_prefix=code[:10],
        code_hash=hash_secret(code),
        max_uses=max_uses,
        used_count=0,
        active=True,
        default_role=role,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_invite_registration_and_email_or_username_login():
    from app.api.routes import auth_login, auth_register

    db = _session()
    invite = _add_invite(db, "internal-only-code")

    registered = auth_register({
        "username": "alice",
        "email": "Alice@Example.com",
        "password": "Password1",
        "confirm_password": "Password1",
        "invite_code": "internal-only-code",
    }, request=None, db=db)

    assert registered["username"] == "alice"
    assert registered["email"] == "alice@example.com"
    assert registered["role"] == "user"
    assert registered["token"]
    assert invite.code_hash != "internal-only-code"
    assert db.get(InviteCode, invite.id).used_count == 1

    by_username = auth_login({
        "identifier": "alice",
        "password": "Password1",
    }, request=None, db=db)
    by_email = auth_login({
        "identifier": "alice@example.com",
        "password": "Password1",
    }, request=None, db=db)

    assert by_username["username"] == "alice"
    assert by_email["username"] == "alice"

    with pytest.raises(HTTPException) as exc:
        auth_register({
            "username": "bob",
            "email": "bob@example.com",
            "password": "Password1",
            "confirm_password": "Password1",
            "invite_code": "internal-only-code",
        }, request=None, db=db)
    assert exc.value.status_code == 400


def test_logout_revokes_session_token():
    from app.api.routes import auth_login, auth_logout, require_user

    db = _session()
    _add_user(db, username="alice", email="alice@example.com", role="user")
    logged_in = auth_login({
        "identifier": "alice@example.com",
        "password": "Password1",
    }, request=None, db=db)
    token = logged_in["token"]

    assert require_user(authorization=f"Bearer {token}", db=db) == "alice"

    auth_logout(authorization=f"Bearer {token}", db=db)

    with pytest.raises(HTTPException) as exc:
        require_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


def test_production_login_requires_sc_secret(monkeypatch):
    from app.api.routes import auth_login

    db = _session()
    _add_user(db, username="alice", email="alice@example.com", role="user")
    monkeypatch.setenv("SC_ENV", "production")
    monkeypatch.delenv("SC_SECRET", raising=False)

    with pytest.raises(HTTPException) as exc:
        auth_login({
            "identifier": "alice",
            "password": "Password1",
        }, request=None, db=db)
    assert exc.value.status_code == 500


def test_change_password_revokes_other_sessions():
    from app.api.routes import auth_login, change_password, require_user

    db = _session()
    _add_user(db, username="alice", email="alice@example.com", role="user")
    first = auth_login({"identifier": "alice", "password": "Password1"},
                       request=None, db=db)
    second = auth_login({"identifier": "alice", "password": "Password1"},
                        request=None, db=db)

    change_password({
        "old_password": "Password1",
        "new_password": "Newpass123",
        "confirm_password": "Newpass123",
    }, user="alice", authorization=f"Bearer {second['token']}", db=db)

    with pytest.raises(HTTPException):
        require_user(authorization=f"Bearer {first['token']}", db=db)
    assert require_user(authorization=f"Bearer {second['token']}", db=db) == "alice"

    relogin = auth_login({"identifier": "alice@example.com", "password": "Newpass123"},
                         request=None, db=db)
    assert relogin["username"] == "alice"


def test_admin_invites_are_internal_only_and_plaintext_once():
    from app.api.routes import admin_create_invite, admin_list_invites

    db = _session()
    admin = _add_user(db, username="admin", email="admin@example.com", role="admin")
    _add_user(db, username="alice", email="alice@example.com", role="user")

    created = admin_create_invite({
        "max_uses": 2,
        "expires_in_days": 3,
        "default_role": "viewer",
    }, user="admin", db=db)

    assert created["code"].startswith("sci_")
    stored = db.get(InviteCode, created["id"])
    assert stored.code_hash != created["code"]
    assert stored.created_by_user_id == admin.id

    listed = admin_list_invites(user="admin", db=db)
    assert "code" not in listed[0]

    with pytest.raises(HTTPException) as exc:
        admin_create_invite({}, user="alice", db=db)
    assert exc.value.status_code == 403


def test_admin_can_create_update_and_reset_users():
    from app.api.routes import (admin_create_user, admin_reset_password,
                                admin_update_user, auth_login)

    db = _session()
    _add_user(db, username="admin", email="admin@example.com", role="admin")
    _add_user(db, username="alice", email="alice@example.com", role="user")

    created = admin_create_user({
        "username": "bob",
        "email": "bob@example.com",
        "display_name": "Bob",
        "role": "viewer",
    }, user="admin", db=db)

    assert created["username"] == "bob"
    assert created["temporary_password"]
    assert auth_login({
        "identifier": "bob@example.com",
        "password": created["temporary_password"],
    }, request=None, db=db)["username"] == "bob"

    updated = admin_update_user(created["id"], {
        "role": "user",
        "status": "disabled",
    }, user="admin", db=db)
    assert updated["role"] == "user"
    assert updated["status"] == "disabled"

    with pytest.raises(HTTPException) as exc:
        auth_login({
            "identifier": "bob",
            "password": created["temporary_password"],
        }, request=None, db=db)
    assert exc.value.status_code == 403

    reset = admin_reset_password(created["id"], {}, user="admin", db=db)
    assert reset["temporary_password"]

    with pytest.raises(HTTPException) as exc:
        admin_create_user({
            "username": "mallory",
            "email": "mallory@example.com",
        }, user="alice", db=db)
    assert exc.value.status_code == 403


def test_user_api_keys_are_owner_scoped_and_scope_limited():
    from app.api.routes import create_key, list_keys, update_key

    db = _session()
    admin = _add_user(db, username="admin", email="admin@example.com", role="admin")
    alice = _add_user(db, username="alice", email="alice@example.com", role="user")
    bob = _add_user(db, username="bob", email="bob@example.com", role="user")
    db.add(ApiKey(name="bob-key", key_prefix="sck_bob",
                  key_hash=hash_key("sck_bob_secret"), active=True,
                  owner_user_id=bob.id))
    db.commit()

    created = create_key({
        "name": "alice-key",
        "scopes": ["admin:*", "crawler:crawl"],
        "monthly_credit_quota": 9999,
    }, user="alice", db=db)

    assert created["owner_user_id"] == alice.id
    assert created["scopes"] == ["crawler:read", "crawler:scrape"]
    assert created["monthly_credit_quota"] is None

    alice_keys = list_keys(user="alice", db=db)
    assert [k["name"] for k in alice_keys] == ["alice-key"]
    admin_keys = list_keys(user="admin", db=db)
    assert {k["name"] for k in admin_keys} == {"alice-key"}

    updated = update_key(created["id"], {
        "scopes": ["admin:*"],
        "monthly_credit_quota": 100,
        "active": False,
    }, user="alice", db=db)
    assert updated["scopes"] == ["crawler:read", "crawler:scrape"]
    assert updated["monthly_credit_quota"] is None
    assert updated["active"] is False

    admin_updated = update_key(created["id"], {
        "scopes": ["crawler:read", "crawler:crawl"],
        "monthly_credit_quota": 100,
        "owner_user_id": admin.id,
    }, user="admin", db=db)
    assert admin_updated["scopes"] == ["crawler:crawl", "crawler:read"]
    assert admin_updated["monthly_credit_quota"] == 100
    assert admin_updated["owner_user_id"] == admin.id
