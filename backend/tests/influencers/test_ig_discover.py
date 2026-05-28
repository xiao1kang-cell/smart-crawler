"""Unit tests for Instagram tag JSON parser (synthetic fixture)."""
from __future__ import annotations

import os

import pytest

from app.influencers.ig_discover import extract_creators_from_tag_json


pytestmark = pytest.mark.unit


def test_extracts_creators_from_synthetic_tag_json(fixture_json):
    data = fixture_json("ig_tag_synthetic.json")
    creators = extract_creators_from_tag_json(data)
    usernames = sorted(c["ownerUsername"] for c in creators)
    assert usernames == ["amzpro", "fbaqueen", "sellerjoe"]  # dedup applied

    sj = next(c for c in creators if c["ownerUsername"] == "sellerjoe")
    assert sj["ownerFullName"] == "Seller Joe"
    assert sj["ownerFollowersCount"] == 12345
    assert sj["publicEmail"] == "biz@sellerjoe.com"
    assert sj["ownerExternalUrl"] == "https://sellerjoe.com"


def test_empty_json_returns_empty():
    assert extract_creators_from_tag_json({}) == []
    assert extract_creators_from_tag_json({"data": {}}) == []


@pytest.mark.smoke
@pytest.mark.skipif(
    not os.environ.get("IG_COOKIES_PATH"),
    reason="IG_COOKIES_PATH not set",
)
def test_smoke_instagram_hashtag_returns_real_creators():
    from app.influencers.ig_discover import fetch_hashtag
    creators = fetch_hashtag("amazonfba", limit=5)
    assert len(creators) >= 1
    assert creators[0]["ownerUsername"]
