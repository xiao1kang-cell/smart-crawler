"""Discover orchestrator — dispatches to per-platform adapters, dedupes results.

Each adapter exposes `run(params, limit) -> list[dict]` returning items shaped
for the per-platform mapper in discover_models.py. Adapters added incrementally
(Task 6+): tt_discover, ig_discover, fb_discover.
"""
from __future__ import annotations

from . import fb_discover, ig_discover, tt_discover, yt_about
from .discover_models import (
    CreatorRecord,
    map_facebook,
    map_instagram,
    map_tiktok,
    map_youtube_about,
)


def dedupe(items: list[dict]) -> list[dict]:
    """Drop dups keyed by (platform, handle)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        key = (it.get("platform", ""), it.get("handle") or it.get("channelId", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _yt_about_run(params: dict, limit: int) -> list[dict]:
    urls = list(params.get("urls") or [])[:limit]
    out = []
    for url in urls:
        parsed = yt_about.fetch_about(url)
        out.append(map_youtube_about(url, parsed))
    return out


_ADAPTERS = {
    "youtube_about": _yt_about_run,
    "tiktok": tt_discover.run,
    "instagram": ig_discover.run,
    "facebook": fb_discover.run,
}


def _to_dicts(records: list[CreatorRecord | dict | None]) -> list[dict]:
    out = []
    for r in records:
        if r is None:
            continue
        if isinstance(r, CreatorRecord):
            out.append(r.to_dict())
        else:
            out.append(r)
    return out


def dispatch(platform: str, params: dict, limit: int) -> list[dict]:
    fn = _ADAPTERS.get(platform)
    if fn is None:
        raise ValueError(f"unknown platform: {platform}")
    raw = fn(params, limit)
    items = _to_dicts(raw)
    if platform in ("tiktok", "instagram", "facebook"):
        items = dedupe(items)
    return items


__all__ = ["dispatch", "dedupe", "map_tiktok", "map_instagram", "map_facebook"]
