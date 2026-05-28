"""Unit tests for Facebook search/pages parser (synthetic fixture)."""
from __future__ import annotations

import os

import pytest

from app.influencers.fb_discover import extract_pages_from_search_html


pytestmark = pytest.mark.unit


def test_extracts_pages_from_synthetic_search_html(fixture_text):
    html = fixture_text("fb_search_synthetic.html")
    pages = extract_pages_from_search_html(html)
    handles = sorted(p["username"] for p in pages)
    assert handles == ["amzpro", "fbaqueen", "sellerjoe"]  # dedup + nav noise filtered
    for p in pages:
        assert p["url"].startswith("https://www.facebook.com/")


def test_empty_html_returns_empty():
    assert extract_pages_from_search_html("") == []


@pytest.mark.smoke
@pytest.mark.skipif(
    not os.environ.get("FB_COOKIES_PATH"),
    reason="FB_COOKIES_PATH not set",
)
def test_smoke_facebook_search_returns_real_pages():
    from app.influencers.fb_discover import fetch_query
    pages = fetch_query("amazon fba", limit=5)
    assert len(pages) >= 1
    assert pages[0]["username"]
