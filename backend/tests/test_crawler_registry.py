from __future__ import annotations

import pytest

from app.crawlers.generic import GenericCrawler
from app.crawlers.registry import get_crawler
from app.models import Site

pytestmark = pytest.mark.unit


def test_unknown_platform_falls_back_to_generic():
    site = Site(site="x", url="https://x.com", country="US",
                platform="new_platform", proxy_tier="none")

    crawler = get_crawler(site)

    assert isinstance(crawler, GenericCrawler)
