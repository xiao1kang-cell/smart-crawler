from __future__ import annotations

import pytest

from app.url_filters import is_obvious_non_product_url


pytestmark = pytest.mark.unit


def test_system_and_challenge_urls_are_not_product_candidates():
    blocked = [
        "https://shop.example.com/cdn-cgi",
        "https://shop.example.com/cdn-cgi/challenge-platform/h/b/scripts/jsd",
        "https://shop.example.com/.well-known/baleen/challengejs/check?x=y",
        "https://shop.example.com/assets/app.js",
        "https://shop.example.com/login",
    ]
    assert all(is_obvious_non_product_url(url) for url in blocked)


def test_normal_product_like_slug_is_allowed():
    assert not is_obvious_non_product_url(
        "https://shop.example.com/bambusowa-szafka-lazienkowa"
    )

