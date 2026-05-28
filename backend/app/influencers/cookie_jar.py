"""Cookie loader for IG / FB adapters.

Cookies are JSON arrays of {name, value, domain, path} entries (Playwright
context.cookies() format). File paths come from env vars; the loader caches
parsed cookies until invalidate() is called (e.g. after a 401).

Never log cookie values — use redact() before emitting any string that may
contain them.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_ENV_KEY = {
    "instagram": "IG_COOKIES_PATH",
    "facebook": "FB_COOKIES_PATH",
}

_cache: dict[str, dict[str, str]] = {}
_lock = threading.RLock()


class CookieExpiredError(RuntimeError):
    """Raised when a cookie jar is missing, unreadable, or rejected by the platform."""

    def __init__(self, platform: str, detail: str = ""):
        self.platform = platform
        msg = f"cookies_expired_{platform}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


def load_cookies(platform: str) -> dict[str, str]:
    """Return {name: value} for the platform. Raises CookieExpiredError if missing."""
    with _lock:
        cached = _cache.get(platform)
        if cached is not None:
            return cached
        env_key = _ENV_KEY.get(platform)
        if not env_key:
            raise CookieExpiredError(platform, f"no env key for {platform}")
        path = os.environ.get(env_key)
        if not path:
            raise CookieExpiredError(platform, f"env {env_key} unset")
        p = Path(path)
        if not p.is_file():
            raise CookieExpiredError(platform, f"file {path} not found")
        try:
            entries = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise CookieExpiredError(platform, f"parse error: {e}") from e
        if not isinstance(entries, list):
            raise CookieExpiredError(platform, "expected JSON array")
        jar = {str(e["name"]): str(e["value"]) for e in entries if "name" in e and "value" in e}
        if not jar:
            raise CookieExpiredError(platform, "empty jar")
        _cache[platform] = jar
        return jar


def invalidate(platform: str) -> None:
    """Drop the cached jar so the next load_cookies() re-reads the file."""
    with _lock:
        _cache.pop(platform, None)


def redact(text: str, jar: dict[str, str]) -> str:
    """Replace every cookie value in `text` with [REDACTED]. For log scrubbing."""
    out = text
    for v in jar.values():
        if v:
            out = out.replace(v, "[REDACTED]")
    return out
