"""Sephora crawler onboarding tests."""
from __future__ import annotations

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

_INDEX_URL = "https://www.sephora.com/sitemap.xml"
_PRODUCTS_URL = "https://www.sephora.com/sitemaps/products-sitemap.xml"
_PDP_URL = (
    "https://www.sephora.com/product/"
    "one-size-by-patrick-starrr-turn-up-base-buttersilk-concealer-P473741"
)

_SITEMAP_INDEX = f"""
<sitemapindex>
  <sitemap><loc>{_PRODUCTS_URL}</loc></sitemap>
</sitemapindex>
"""

_PRODUCTS_SITEMAP = f"""
<urlset>
  <url><loc>{_PDP_URL}</loc></url>
</urlset>
"""


def _site() -> Site:
    s = Site()
    s.site = "sephora_us_makeup"
    s.url = "https://www.sephora.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "sephora"
    s.brand = "Sephora"
    return s


def _ok(url: str, text: str) -> FetchResult:
    return FetchResult(
        ok=True,
        url=url,
        status=200,
        text=text,
        content=text.encode(),
        final_url=url,
        fetcher="curl_cffi",
    )


def test_sephora_us_defaults_to_sitemap_only(monkeypatch):
    from app.crawlers.sephora import SephoraCrawler

    crawler = SephoraCrawler(_site(), limit=1)
    calls: list[str] = []

    class _FakeFetcher:
        def get(self, url: str, **kw):
            calls.append(url)
            crawler.counter.api_calls += 1
            if url == _INDEX_URL:
                return _ok(url, _SITEMAP_INDEX)
            if url == _PRODUCTS_URL:
                return _ok(url, _PRODUCTS_SITEMAP)
            raise AssertionError(f"unexpected PDP fetch: {url}")

    monkeypatch.delenv("SEPHORA_FETCH_PDP", raising=False)
    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    assert calls == [_INDEX_URL, _PRODUCTS_URL]
    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "P473741"
    assert "Turn Up Base Buttersilk Concealer" in row["title"]
    assert row["currency"] == "USD"
    assert row["product_url"] == _PDP_URL
    assert row["site"] == "sephora_us_makeup"
    assert row["attributes"]["source"] == "sitemap"


def test_sephora_sitemap_row_from_url_not_degraded():
    from app.crawlers.sephora import SephoraCrawler

    crawler = SephoraCrawler(_site(), limit=1)
    row = crawler._row_from_sitemap(_PDP_URL)

    assert row is not None
    assert row["sku"] == "P473741"
    assert row["spu"] == "P473741"
    assert row["title"] == (
        "One Size By Patrick Starrr Turn Up Base Buttersilk Concealer")
    assert row["status"] == "on_sale"
