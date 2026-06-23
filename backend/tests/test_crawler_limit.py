from __future__ import annotations

import pytest

from app.crawlers.overstock import OverstockCrawler, DEFAULT_LIMIT
from app.models import Site

pytestmark = pytest.mark.unit


def _site():
    return Site(site="x", url="https://x.com", country="US",
                platform="overstock", proxy_tier="dc")


def test_overstock_ignores_sites_yaml_max_products_for_full_crawl(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    c = OverstockCrawler(_site())
    assert c.limit == DEFAULT_LIMIT


def test_resolve_limit_falls_back_to_default(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [{"site": "x"}])
    c = OverstockCrawler(_site())
    assert c.limit == DEFAULT_LIMIT


def test_bol_ignores_max_products_for_full_sitemap_crawl(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 7}])
    from app.crawlers.bol import BolCrawler, DEFAULT_LIMIT
    c = BolCrawler(Site(site="x", url="https://x.com", country="NL",
                        platform="bol", proxy_tier="dc"))
    assert c.limit == DEFAULT_LIMIT


def test_idealo_reads_max_products(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 9}])
    from app.crawlers.idealo import IdealoCrawler
    c = IdealoCrawler(Site(site="x", url="https://x.com", country="DE",
                           platform="idealo", proxy_tier="dc"))
    assert c.limit == 9


def test_vidaxl_ignores_max_products_for_full_feed_crawl(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 11}])
    from app.crawlers.vidaxl import VidaxlCrawler
    c = VidaxlCrawler(Site(site="x", url="https://x.com", country="US",
                           platform="vidaxl", proxy_tier="dc"))
    assert c.limit > 11


def test_vidaxl_zero_max_products_means_full_storefront(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 0}])
    from app.crawlers.vidaxl import STOREFRONT_LIMIT, VidaxlCrawler
    c = VidaxlCrawler(Site(site="x", url="https://x.com", country="US",
                           platform="vidaxl", proxy_tier="dc"))
    assert c.limit == STOREFRONT_LIMIT


def test_vidaxl_default_limit_is_full_storefront(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [{"site": "x"}])
    from app.crawlers.vidaxl import STOREFRONT_LIMIT, VidaxlCrawler
    c = VidaxlCrawler(Site(site="x", url="https://x.com", country="US",
                           platform="vidaxl", proxy_tier="dc"))
    assert c.limit == STOREFRONT_LIMIT


def test_explicit_limit_param_beats_hints(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    from app.crawlers.cratebarrel import CrateBarrelCrawler
    c = CrateBarrelCrawler(Site(site="x", url="https://x.com", country="US",
                                platform="cratebarrel", proxy_tier="dc"), limit=3)
    assert c.limit == 3


def test_cratebarrel_ignores_hints_when_no_param(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 5}])
    from app.crawlers.cratebarrel import CrateBarrelCrawler
    c = CrateBarrelCrawler(Site(site="x", url="https://x.com", country="US",
                                platform="cratebarrel", proxy_tier="dc"))
    assert c.limit > 5


def test_generic_ignores_max_products_for_full_crawl(monkeypatch):
    monkeypatch.setattr("app.crawlers.base.get_sites",
                        lambda: [{"site": "x", "max_products": 8,
                                  "sitemap": "https://x.com/sitemap.xml"}])
    from app.crawlers.generic import GenericCrawler
    c = GenericCrawler(Site(site="x", url="https://x.com", country="US",
                            platform="generic", proxy_tier="dc"))
    assert c.limit > 8


def test_generic_block_detection_ignores_cloudflare_marketing_copy():
    from app.crawlers.generic import GenericCrawler
    from selectolax.parser import HTMLParser

    html = """
    <html><head><title>smart-crawler</title></head>
    <body>Supports Cloudflare-aware crawling workflows.</body></html>
    """

    assert GenericCrawler._looks_blocked_page(HTMLParser(html), html) is False


def test_generic_block_detection_keeps_cloudflare_challenge_marker():
    from app.crawlers.generic import GenericCrawler
    from selectolax.parser import HTMLParser

    html = """
    <html><head><title>Just a moment...</title></head>
    <body><script src="/cdn-cgi/challenge-platform/h/b/scripts/jsd/main.js"></script></body></html>
    """

    assert GenericCrawler._looks_blocked_page(HTMLParser(html), html) is True


def test_fetching_block_detection_ignores_cloudflare_vendor_copy():
    from app.fetching import _looks_like_anti_bot

    html = """
    <html><head><title>smart-crawler</title></head>
    <body>Supports Cloudflare-aware crawling workflows.</body></html>
    """

    assert _looks_like_anti_bot(html) is False


def test_fetching_block_detection_keeps_active_cloudflare_challenge():
    from app.fetching import _looks_like_anti_bot

    html = """
    <html><head><title>Just a moment...</title></head>
    <body><script>window._cf_chl_opt = {}; cf-chl-bypass</script></body></html>
    """

    assert _looks_like_anti_bot(html) is True


def test_fetching_block_detection_ignores_datadome_vendor_copy():
    from app.fetching import _looks_like_anti_bot

    html = """
    <html><head><title>smart-crawler</title></head>
    <body>Handles Cloudflare, Akamai, PerimeterX, DataDome with ease.</body></html>
    """

    assert _looks_like_anti_bot(html) is False


def test_fetching_block_detection_keeps_datadome_challenge():
    from app.fetching import _looks_like_anti_bot

    html = """
    <html><body><script src="https://geo.captcha-delivery.com/captcha.js"></script></body></html>
    """

    assert _looks_like_anti_bot(html) is True
