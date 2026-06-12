from __future__ import annotations

import pytest

from app.crawlers.overstock import OverstockCrawler, DEFAULT_LIMIT
from app.models import Site

pytestmark = pytest.mark.unit


def _site():
    return Site(site="x", url="https://x.com", country="US",
                platform="overstock", proxy_tier="dc")


def test_resolve_limit_uses_sites_yaml_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    c = OverstockCrawler(_site())
    assert c.limit == 5


def test_resolve_limit_falls_back_to_default(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [{"site": "x"}])
    c = OverstockCrawler(_site())
    assert c.limit == DEFAULT_LIMIT
