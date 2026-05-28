"""Apify-compatible output schema + per-platform raw→CreatorRecord mappers.

Discovery adapters return Apify-shaped raw dicts (matching the contracts the
internal Node caller already speaks). This module turns those into the unified
CreatorRecord the HTTP API returns.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


@dataclass
class CreatorRecord:
    channelId: str
    name: str | None
    platform: str
    profileUrl: str
    handle: str | None
    followerCount: int | None
    email: str | None
    websiteUrl: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _first_email(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = _EMAIL_RE.search(t)
        if m:
            return m.group(0)
    return None


def _first_nonempty(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return None


def map_tiktok(raw: dict) -> CreatorRecord | None:
    a = (raw or {}).get("authorMeta") or {}
    uid = a.get("uniqueId") or a.get("name")
    if not uid:
        return None
    return CreatorRecord(
        channelId=f"@{uid}",
        name=a.get("nickName") or a.get("name"),
        platform="TikTok",
        profileUrl=f"https://www.tiktok.com/@{uid}",
        handle=uid,
        followerCount=_first_nonempty(
            a.get("fans"), a.get("followers"), a.get("followerCount"),
        ),
        email=_first_email(a.get("signature"), a.get("bioLink")),
        websiteUrl=a.get("bioLink"),
    )


def map_instagram(raw: dict) -> CreatorRecord | None:
    uid = raw.get("ownerUsername") or raw.get("username")
    if not uid:
        return None
    public_email = raw.get("publicEmail")
    bio = raw.get("ownerBiography") or raw.get("biography")
    return CreatorRecord(
        channelId=f"ig:{uid}",
        name=raw.get("ownerFullName") or raw.get("fullName"),
        platform="Instagram",
        profileUrl=f"https://www.instagram.com/{uid}/",
        handle=uid,
        followerCount=_first_nonempty(
            raw.get("ownerFollowersCount"), raw.get("followersCount"),
        ),
        email=public_email or _first_email(bio),
        websiteUrl=raw.get("ownerExternalUrl") or raw.get("externalUrl"),
    )


def map_facebook(raw: dict) -> CreatorRecord | None:
    uid = raw.get("username") or raw.get("pageId")
    if not uid:
        return None
    url = raw.get("url") or f"https://www.facebook.com/{uid}"
    return CreatorRecord(
        channelId=f"fb:{uid}",
        name=raw.get("title") or raw.get("name"),
        platform="Facebook",
        profileUrl=url,
        handle=str(uid),
        followerCount=_first_nonempty(
            raw.get("followers"), raw.get("followersCount"),
            raw.get("likes"), raw.get("fanCount"),
        ),
        email=raw.get("email") or _first_email(raw.get("about"), raw.get("description")),
        websiteUrl=raw.get("website"),
    )


def map_youtube_about(_url: str, parsed: dict) -> dict:
    return {
        "email": parsed.get("email"),
        "websiteUrl": parsed.get("websiteUrl"),
    }
