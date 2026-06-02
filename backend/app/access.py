"""API key scopes and shared access helpers."""
from __future__ import annotations

from typing import Iterable

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from .apikey import hash_key
from .models import ApiKey

DEFAULT_API_KEY_SCOPES = ["crawler:read", "crawler:scrape"]
ADMIN_SCOPE = "admin:*"


def raw_key_from_headers(authorization: str = "", x_api_key: str = "") -> str:
    """Extract an sck_ key from common API key headers."""
    if x_api_key:
        return x_api_key.strip()
    auth = authorization or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return auth.strip()


def normalize_scopes(value: object) -> list[str]:
    """Return explicit scopes, falling back to safe external defaults."""
    if value is None:
        return list(DEFAULT_API_KEY_SCOPES)
    if isinstance(value, str):
        scopes = [s.strip() for s in value.replace(",", " ").split()]
    elif isinstance(value, Iterable):
        scopes = [str(s).strip() for s in value]
    else:
        scopes = []
    return sorted({s for s in scopes if s}) or list(DEFAULT_API_KEY_SCOPES)


def api_key_scopes(key: ApiKey | None) -> list[str]:
    return normalize_scopes(getattr(key, "scopes", None))


def has_scope(scopes: Iterable[str], required: str) -> bool:
    scope_set = set(scopes)
    if ADMIN_SCOPE in scope_set or required in scope_set:
        return True
    namespace = required.split(":", 1)[0]
    return f"{namespace}:*" in scope_set


def find_api_key(db: Session, raw_key: str) -> ApiKey | None:
    if not raw_key:
        return None
    return (db.query(ApiKey)
              .filter(ApiKey.key_hash == hash_key(raw_key),
                      ApiKey.active.is_(True))
              .first())


def require_api_key_scope(key: ApiKey | None, required: str) -> None:
    """Enforce scope for API-key callers.

    Admin JWT callers pass through v2 without an ApiKey row; scope checks are
    only applied to external sck_ keys.
    """
    if key is None:
        return
    scopes = api_key_scopes(key)
    if has_scope(scopes, required):
        return
    raise HTTPException(
        403,
        {
            "error": "insufficient_scope",
            "required_scope": required,
            "granted_scopes": scopes,
            "message": f"API key lacks required scope: {required}",
        },
    )


def scope_headers(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> tuple[str, str]:
    return authorization, x_api_key
