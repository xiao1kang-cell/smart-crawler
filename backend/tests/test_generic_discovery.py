from __future__ import annotations

import pytest

from app.crawlers.base import CrawlResult
from app.crawlers.generic import GenericCrawler
from app.models import Site

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")


class _FakeSession:
    def __init__(self, pages: dict[str, _Resp]):
        self.pages = pages

    def get(self, url: str, timeout=30):
        return self.pages.get(url, _Resp(404, ""))


def _crawler(monkeypatch):
    monkeypatch.setattr("app.crawlers.generic.get_sites", lambda: [])
    site = Site(site="x", url="https://x.com", country="US",
                platform="generic", proxy_tier="none")
    return GenericCrawler(site)


def test_generic_discovers_sitemap_from_robots(monkeypatch):
    crawler = _crawler(monkeypatch)
    sess = _FakeSession({
        "https://x.com/robots.txt": _Resp(
            text="Sitemap: https://x.com/custom-sitemap.xml\n"),
        "https://x.com/custom-sitemap.xml": _Resp(
            text="<urlset><url><loc>https://x.com/products/widget</loc></url></urlset>"),
    })

    urls = crawler._discover_product_urls(sess, CrawlResult())

    assert urls == ["https://x.com/products/widget"]


def test_generic_falls_back_to_entry_page_links(monkeypatch):
    crawler = _crawler(monkeypatch)
    sess = _FakeSession({
        "https://x.com": _Resp(
            text='<html><a href="/products/widget">Widget</a></html>'),
    })

    urls = crawler._discover_product_urls(sess, CrawlResult())

    assert urls == ["https://x.com/products/widget"]
