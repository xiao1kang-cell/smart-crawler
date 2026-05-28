"""Unit tests for CreatorRecord schema + raw→record mappers."""
from __future__ import annotations

import pytest

from app.influencers.discover_models import (
    CreatorRecord,
    map_facebook,
    map_instagram,
    map_tiktok,
    map_youtube_about,
)


pytestmark = pytest.mark.unit


def test_tiktok_mapper_full():
    raw = {
        "authorMeta": {
            "uniqueId": "sellerjoe",
            "nickName": "Seller Joe",
            "fans": 12345,
            "signature": "Contact: hello@sellerjoe.com",
            "bioLink": "https://sellerjoe.com",
        },
    }
    r = map_tiktok(raw)
    assert r.channelId == "@sellerjoe"
    assert r.handle == "sellerjoe"
    assert r.name == "Seller Joe"
    assert r.platform == "TikTok"
    assert r.profileUrl == "https://www.tiktok.com/@sellerjoe"
    assert r.followerCount == 12345
    assert r.email == "hello@sellerjoe.com"
    assert r.websiteUrl == "https://sellerjoe.com"


def test_tiktok_mapper_fallback_follower_keys():
    raw = {"authorMeta": {"uniqueId": "x", "followerCount": 99}}
    r = map_tiktok(raw)
    assert r.followerCount == 99


def test_tiktok_mapper_missing_required_returns_none():
    assert map_tiktok({"authorMeta": {}}) is None
    assert map_tiktok({}) is None


def test_instagram_mapper_full():
    raw = {
        "ownerUsername": "sellerjoe",
        "ownerFullName": "Seller Joe",
        "ownerFollowersCount": 12345,
        "ownerBiography": "email me at hi@sellerjoe.com",
        "ownerExternalUrl": "https://sellerjoe.com",
        "publicEmail": "direct@sellerjoe.com",
    }
    r = map_instagram(raw)
    assert r.channelId == "ig:sellerjoe"
    assert r.platform == "Instagram"
    assert r.profileUrl == "https://www.instagram.com/sellerjoe/"
    assert r.followerCount == 12345
    assert r.email == "direct@sellerjoe.com"
    assert r.websiteUrl == "https://sellerjoe.com"


def test_instagram_mapper_falls_back_to_bio_email():
    raw = {"ownerUsername": "x", "ownerBiography": "Reach: foo@bar.com"}
    r = map_instagram(raw)
    assert r.email == "foo@bar.com"


def test_facebook_mapper_full():
    raw = {
        "username": "sellerjoe",
        "url": "https://www.facebook.com/sellerjoe",
        "followers": 12345,
        "name": "Seller Joe",
        "email": "hi@sellerjoe.com",
        "website": "https://sellerjoe.com",
    }
    r = map_facebook(raw)
    assert r.channelId == "fb:sellerjoe"
    assert r.platform == "Facebook"
    assert r.profileUrl == "https://www.facebook.com/sellerjoe"
    assert r.followerCount == 12345


def test_facebook_mapper_uses_pageId_when_no_username():
    raw = {"pageId": "9988", "url": "https://www.facebook.com/9988", "name": "X"}
    r = map_facebook(raw)
    assert r.channelId == "fb:9988"
    assert r.handle == "9988"


def test_youtube_about_mapper():
    r = map_youtube_about(
        "https://www.youtube.com/@MrBeast/about",
        {"email": "biz@mrbeast.com", "websiteUrl": "https://mrbeast.com"},
    )
    assert r == {"email": "biz@mrbeast.com", "websiteUrl": "https://mrbeast.com"}


def test_youtube_about_mapper_missing():
    r = map_youtube_about("https://www.youtube.com/@x/about", {})
    assert r == {"email": None, "websiteUrl": None}
