"""Unit tests for TikTok tag parser (uses a synthetic fixture — see module note)."""
from __future__ import annotations

import os

import pytest

from app.influencers.tt_discover import extract_creators_from_tag_html


pytestmark = pytest.mark.unit


def test_extracts_creators_from_synthetic_tag_page(fixture_text):
    # TikTok's real /tag/ page no longer ships SSR JSON (2026-05 lockdown);
    # parser is validated against a synthetic fixture whose shape matches the
    # data we get back via Playwright / msToken-signed paths. See README.
    html = fixture_text("tt_tag_synthetic.html")
    creators = extract_creators_from_tag_html(html)
    handles = sorted(c["authorMeta"]["uniqueId"] for c in creators)
    # dedup: sellerjoe appears twice in the fixture, must collapse
    assert handles == ["amzpro", "fbaqueen", "sellerjoe"]
    sellerjoe = next(c["authorMeta"] for c in creators if c["authorMeta"]["uniqueId"] == "sellerjoe")
    assert sellerjoe["followerCount"] == 12345
    assert sellerjoe["bioLink"] == "https://sellerjoe.com"


def test_empty_html_returns_empty_list():
    assert extract_creators_from_tag_html("") == []
    assert extract_creators_from_tag_html("<html></html>") == []


@pytest.mark.smoke
@pytest.mark.skipif(
    os.environ.get("TIKTOK_SMOKE") != "1",
    reason="TikTok HTTP path is currently blocked (challenge shell); enable with TIKTOK_SMOKE=1 once Playwright lane is wired",
)
def test_smoke_tiktok_hashtag_returns_real_creators():
    from app.influencers.tt_discover import fetch_hashtag
    creators = fetch_hashtag("amazonfba", limit=5)
    assert len(creators) >= 1
    assert creators[0]["authorMeta"].get("uniqueId")
