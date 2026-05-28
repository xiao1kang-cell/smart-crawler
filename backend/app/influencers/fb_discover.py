"""Facebook pages search → pages adapter — replaces apify/facebook-pages-scraper.

Facebook search response embeds page URLs in inline scripts as
"https://www.facebook.com/<handle>". Parse those, dedupe, filter platform
navigation names (search, marketplace, etc.). v1 only yields handle + url +
name (null); follower/email/website enrichment is Phase 2 (would require a
second request to /{handle}/about).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

from ..antiban import check_blocked, humanized_sleep, ip_record, rate_delay
from ..proxy import get_proxy
from ._common import http
from .cookie_jar import CookieExpiredError, invalidate, load_cookies

log = logging.getLogger(__name__)

_FB_BASE = "https://www.facebook.com"
_SEARCH = _FB_BASE + "/search/pages/?q={q}"

_PAGE_HANDLE_RE = re.compile(
    r'https://www\.facebook\.com/([A-Za-z0-9.\-]+)/?(?=["\\/?])'
)

_NAV_NOISE = {
    "search", "pages", "watch", "home", "marketplace", "groups",
    "events", "gaming", "login", "checkpoint", "help", "policies",
    "settings", "messages", "notifications", "friends",
    "profile.php", "people", "places",
}


def extract_pages_from_search_html(html: str) -> list[dict]:
    if not html:
        return []
    raw = html.replace("\\/", "/")
    out: list[dict] = []
    seen: set[str] = set()
    for m in _PAGE_HANDLE_RE.finditer(raw):
        h = m.group(1)
        if h.lower() in _NAV_NOISE or h in seen:
            continue
        seen.add(h)
        out.append({
            "username": h,
            "url": f"{_FB_BASE}/{h}",
            "name": None,
            "followers": None,
            "website": None,
            "email": None,
        })
    return out


def _fetch_search(query: str) -> str:
    jar = load_cookies("facebook")
    s = http()
    s.headers["Referer"] = f"{_FB_BASE}/"
    proxy = get_proxy("residential", site="facebook")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = s.get(_SEARCH.format(q=quote_plus(query)), cookies=jar, timeout=20,
              proxies=proxies, allow_redirects=False)
    ip_record(proxy or "direct")
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302) and ("/login" in loc or "/checkpoint/" in loc):
        invalidate("facebook")
        raise CookieExpiredError("facebook", f"redirect to {loc[:64]}")
    check_blocked(r.status_code, f"facebook:search:{query[:32]}")
    if r.status_code != 200:
        return ""
    humanized_sleep(rate_delay("facebook", 4.0))
    return r.text


def fetch_query(query: str, limit: int) -> list[dict]:
    html = _fetch_search(query)
    pages = extract_pages_from_search_html(html)
    return pages[:limit]


def run(params: dict, limit: int) -> list[dict]:
    from .discover_models import map_facebook

    queries = list(params.get("hashtags") or [])  # FB uses hashtags slot as queries
    out: list[dict] = []
    per_q = max(1, limit // max(1, len(queries))) if queries else 0
    for q in queries:
        for raw in fetch_query(q, per_q):
            rec = map_facebook(raw)
            if rec:
                out.append(rec.to_dict())
            if len(out) >= limit:
                return out
    return out
