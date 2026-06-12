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


def test_bol_reads_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 7}])
    from app.crawlers.bol import BolCrawler
    c = BolCrawler(Site(site="x", url="https://x.com", country="NL",
                        platform="bol", proxy_tier="dc"))
    assert c.limit == 7


def test_idealo_reads_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 9}])
    from app.crawlers.idealo import IdealoCrawler
    c = IdealoCrawler(Site(site="x", url="https://x.com", country="DE",
                           platform="idealo", proxy_tier="dc"))
    assert c.limit == 9


def test_vidaxl_reads_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 11}])
    from app.crawlers.vidaxl import VidaxlCrawler
    c = VidaxlCrawler(Site(site="x", url="https://x.com", country="US",
                           platform="vidaxl", proxy_tier="dc"))
    assert c.limit == 11


def test_explicit_limit_param_beats_hints(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    from app.crawlers.cratebarrel import CrateBarrelCrawler
    c = CrateBarrelCrawler(Site(site="x", url="https://x.com", country="US",
                                platform="cratebarrel", proxy_tier="dc"), limit=3)
    assert c.limit == 3


def test_cratebarrel_reads_hints_when_no_param(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    from app.crawlers.cratebarrel import CrateBarrelCrawler
    c = CrateBarrelCrawler(Site(site="x", url="https://x.com", country="US",
                                platform="cratebarrel", proxy_tier="dc"))
    assert c.limit == 5


def test_generic_still_reads_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 8,
                                  "sitemap": "https://x.com/sitemap.xml"}])
    from app.crawlers.generic import GenericCrawler
    c = GenericCrawler(Site(site="x", url="https://x.com", country="US",
                            platform="generic", proxy_tier="dc"))
    assert c.limit == 8
