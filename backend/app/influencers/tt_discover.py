"""TikTok hashtag → creators adapter.

Replaces clockworks/tiktok-scraper *for the parsing layer*. As of 2026-05 the
public /tag/{hashtag} page no longer ships SSR JSON to unauthenticated curl,
so fetch_hashtag() will currently return [] in production. Once a Playwright
lane (or msToken-signed XHR) is wired into smart-crawler, this parser drops
in unchanged — it expects a __UNIVERSAL_DATA_FOR_REHYDRATION__ blob whose
shape matches what the real TikTok web app receives.

The parser is unit-tested against a synthetic fixture; the live fetch is
gated behind TIKTOK_SMOKE=1.
"""
from __future__ import annotations

import json
import logging
import re

from ..antiban import check_blocked, humanized_sleep, ip_record, rate_delay
from ..proxy import get_proxy
from ._common import http

log = logging.getLogger(__name__)

_TT_BASE = "https://www.tiktok.com"
_UNIVERSAL_RE = re.compile(
    r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
    re.S,
)
_SIGI_RE = re.compile(r'<script[^>]*id="SIGI_STATE"[^>]*>(.+?)</script>', re.S)


def _parse_state(html: str) -> dict:
    m = _UNIVERSAL_RE.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _SIGI_RE.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return {}


def _walk_for_items(node, found: list[dict]) -> None:
    """DFS the state tree; collect any dict that has author.uniqueId."""
    if isinstance(node, dict):
        author = node.get("author")
        if isinstance(author, dict) and author.get("uniqueId"):
            found.append(node)
            return
        for v in node.values():
            _walk_for_items(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_for_items(v, found)


def _to_author_meta(item: dict) -> dict | None:
    author = item.get("author") or {}
    stats = item.get("authorStats") or {}
    uid = author.get("uniqueId")
    if not uid:
        return None
    bio_link = author.get("bioLink")
    if isinstance(bio_link, dict):
        bio_link = bio_link.get("link")
    return {
        "authorMeta": {
            "uniqueId": uid,
            "nickName": author.get("nickname") or author.get("nickName"),
            "fans": stats.get("followerCount") or stats.get("fans"),
            "followers": stats.get("followerCount"),
            "followerCount": stats.get("followerCount"),
            "signature": author.get("signature"),
            "bioLink": bio_link,
        },
    }


def extract_creators_from_tag_html(html: str) -> list[dict]:
    """Return a list of authorMeta-shaped dicts, deduped by uniqueId."""
    if not html:
        return []
    state = _parse_state(html)
    if not state:
        return []
    items: list[dict] = []
    _walk_for_items(state, items)
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        meta = _to_author_meta(it)
        if not meta:
            continue
        uid = meta["authorMeta"]["uniqueId"]
        if uid in seen:
            continue
        seen.add(uid)
        out.append(meta)
    return out


def fetch_hashtag(hashtag: str, limit: int) -> list[dict]:
    tag = hashtag.lstrip("#")
    url = f"{_TT_BASE}/tag/{tag}"
    proxy = get_proxy("residential", site="tiktok")
    s = http()
    s.headers["Referer"] = f"{_TT_BASE}/"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = s.get(url, timeout=20, proxies=proxies)
    ip_record(proxy or "direct")
    check_blocked(r.status_code, f"tiktok:tag:{tag}")
    if r.status_code != 200:
        return []
    creators = extract_creators_from_tag_html(r.text)
    humanized_sleep(rate_delay("tiktok", 3.0))
    return creators[:limit]


def run(params: dict, limit: int) -> list[dict]:
    from .discover_models import map_tiktok

    hashtags = list(params.get("hashtags") or [])
    out: list[dict] = []
    per_tag = max(1, limit // max(1, len(hashtags))) if hashtags else 0
    for tag in hashtags:
        for raw in fetch_hashtag(tag, per_tag):
            rec = map_tiktok(raw)
            if rec:
                out.append(rec.to_dict())
            if len(out) >= limit:
                return out
    return out
