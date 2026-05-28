"""Instagram hashtag → creators adapter — replaces apify/instagram-scraper.

Uses the public-ish web JSON endpoint /api/v1/tags/web_info/?tag_name=... with
a logged-in cookie jar (IG_COOKIES_PATH env). Output dicts match the Apify
Instagram actor's shape so discover_models.map_instagram works unchanged.
"""
from __future__ import annotations

import logging

from ..antiban import check_blocked, humanized_sleep, ip_record, rate_delay
from ..proxy import get_proxy
from ._common import http
from .cookie_jar import CookieExpiredError, invalidate, load_cookies

log = logging.getLogger(__name__)

_IG_BASE = "https://www.instagram.com"
_TAG_API = _IG_BASE + "/api/v1/tags/web_info/?tag_name={tag}"


def _iter_users(node) -> list[dict]:
    found: list[dict] = []

    def walk(n):
        if isinstance(n, dict):
            u = n.get("user")
            if isinstance(u, dict) and u.get("username"):
                found.append(u)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found


def _follower_count_from(u: dict) -> int | None:
    for k in ("follower_count", "followers_count"):
        if u.get(k) is not None:
            return u[k]
    ebb = u.get("edge_followed_by")
    if isinstance(ebb, dict):
        return ebb.get("count")
    return None


def extract_creators_from_tag_json(data: dict) -> list[dict]:
    if not data:
        return []
    users = _iter_users(data)
    out: list[dict] = []
    seen: set[str] = set()
    for u in users:
        uid = u.get("username")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({
            "ownerUsername": uid,
            "ownerFullName": u.get("full_name"),
            "ownerFollowersCount": _follower_count_from(u),
            "ownerBiography": u.get("biography"),
            "ownerExternalUrl": u.get("external_url"),
            "publicEmail": u.get("public_email"),
        })
    return out


def fetch_hashtag(hashtag: str, limit: int) -> list[dict]:
    tag = hashtag.lstrip("#")
    jar = load_cookies("instagram")  # CookieExpiredError propagates up
    s = http()
    s.headers.update({
        "x-ig-app-id": "936619743392459",
        "x-asbd-id": "129477",
        "Referer": f"{_IG_BASE}/explore/tags/{tag}/",
    })
    proxy = get_proxy("residential", site="instagram")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = s.get(_TAG_API.format(tag=tag), cookies=jar, timeout=20,
              proxies=proxies, allow_redirects=False)
    ip_record(proxy or "direct")
    if r.status_code in (301, 302):
        loc = r.headers.get("Location", "")
        if "/accounts/login" in loc or "/login" in loc:
            invalidate("instagram")
            raise CookieExpiredError("instagram", f"redirect to {loc[:64]}")
    if r.status_code in (401, 403):
        invalidate("instagram")
        raise CookieExpiredError("instagram", f"status={r.status_code}")
    check_blocked(r.status_code, f"instagram:tag:{tag}")
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    creators = extract_creators_from_tag_json(data)
    humanized_sleep(rate_delay("instagram", 4.0))
    return creators[:limit]


def run(params: dict, limit: int) -> list[dict]:
    from .discover_models import map_instagram

    hashtags = list(params.get("hashtags") or [])
    out: list[dict] = []
    per_tag = max(1, limit // max(1, len(hashtags))) if hashtags else 0
    for tag in hashtags:
        for raw in fetch_hashtag(tag, per_tag):
            rec = map_instagram(raw)
            if rec:
                out.append(rec.to_dict())
            if len(out) >= limit:
                return out
    return out
