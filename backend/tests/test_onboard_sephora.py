"""Sephora crawler onboarding tests."""
from __future__ import annotations

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

_INDEX_URL = "https://www.sephora.com/sitemap.xml"
_PRODUCTS_URL = "https://www.sephora.com/sitemaps/products-sitemap.xml"
_FR_INDEX_URL = "https://www.sephora.fr/sitemap_index.xml"
_FR_PRODUCTS_URL = "https://www.sephora.fr/sitemap-customsitemap_product_0.xml"
_PDP_URL = (
    "https://www.sephora.com/product/"
    "one-size-by-patrick-starrr-turn-up-base-buttersilk-concealer-P473741"
)
_FR_PDP_URL = (
    "https://www.sephora.fr/p/meteorites-compact---poudre-compacte-"
    "matifiante-et-fixante-P10064237.html"
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

_FR_SITEMAP_INDEX = f"""
<sitemapindex>
  <sitemap><loc>{_FR_PRODUCTS_URL}</loc></sitemap>
</sitemapindex>
"""

_FR_PRODUCTS_SITEMAP = f"""
<urlset>
  <url><loc>{_FR_PDP_URL}</loc></url>
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


def _fr_site() -> Site:
    s = Site()
    s.site = "sephora_fr_maquillage"
    s.url = "https://www.sephora.fr/maquillage"
    s.country = "FR"
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
    assert row["_skip_price_history_if_no_price"] is True


def test_sephora_us_parse_hydration_price_and_reviews():
    from app.crawlers.sephora import SephoraCrawler

    crawler = SephoraCrawler(_site(), limit=1)
    html = """
    <html>
      <script>
        window.__STATE__ = {
          "productId": "P504696",
          "displayName": "Guilty Pour Femme Eau de Parfum Travel Spray",
          "brandName": "Gucci",
          "currentSku": {
            "skuId": "2535687",
            "regularPrice": "$36.00",
            "salePrice": "$29.00"
          },
          "rating": "4.5",
          "reviews": "1,234"
        };
      </script>
    </html>
    """

    row = crawler._parse_us_pdp(
        html,
        "https://www.sephora.com/product/gucci-guilty-travel-spray-P504696",
    )

    assert row is not None
    assert row["sku"] == "P504696"
    assert row["title"] == "Guilty Pour Femme Eau de Parfum Travel Spray"
    assert row["brand"] == "Gucci"
    assert row["sale_price"] == 29.0
    assert row["original_price"] == 36.0
    assert row["currency"] == "USD"
    assert row["ratings"] == 4.5
    assert row["review_count"] == 1234
    assert row["attributes"]["source"] == "pdp_hydration"


def test_sephora_fr_parse_pdp_price_and_reviews():
    from app.crawlers.sephora import SephoraCrawler

    crawler = SephoraCrawler(_fr_site(), limit=1)
    html = """
    <html>
      <head>
        <meta property="og:title" content="Le Male Le Parfum - Eau de Parfum"/>
        <meta property="og:description" content="Parfum intense"/>
        <meta property="og:image" content="https://www.sephora.fr/img.jpg"/>
      </head>
      <body>
        <h1>Le Male Le Parfum</h1>
        <span>129,00&nbsp;€</span>
        <button>(505 avis sur le produit)</button>
      </body>
    </html>
    """

    row = crawler._parse_fr_pdp(
        html,
        "https://www.sephora.fr/p/le-male-le-parfum---eau-de-parfum-515090.html",
    )

    assert row is not None
    assert row["sku"] == "515090"
    assert row["title"] == "Le Male Le Parfum - Eau de Parfum"
    assert row["sale_price"] == 129.0
    assert row["original_price"] == 129.0
    assert row["currency"] == "EUR"
    assert row["review_count"] == 505


def test_sephora_plain_akamai_word_is_not_blocked():
    from app.crawlers.sephora import SephoraCrawler

    assert SephoraCrawler._blocked(
        "<html><body>static asset served by Akamai</body></html>"
    ) is False


def test_sephora_akamai_challenge_shell_is_blocked():
    from app.crawlers.sephora import SephoraCrawler

    assert SephoraCrawler._blocked(
        '<div id="sec-if-cpt-container">Powered and protected by Akamai</div>'
    ) is True


def test_sephora_us_sitemap_records_full_total_when_limited(monkeypatch):
    from app.crawlers.sephora import SephoraCrawler

    urls = "\n".join(
        f"<url><loc>https://www.sephora.com/product/item-{i}-P{i}</loc></url>"
        for i in range(3)
    )
    products_sitemap = f"<urlset>{urls}</urlset>"
    crawler = SephoraCrawler(_site(), limit=1)

    class _FakeFetcher:
        def get(self, url: str, **kw):
            if url == _INDEX_URL:
                return _ok(url, _SITEMAP_INDEX)
            if url == _PRODUCTS_URL:
                return _ok(url, products_sitemap)
            raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.delenv("SEPHORA_FETCH_PDP", raising=False)
    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 3
    assert result.coverage_complete is False
    assert result.coverage_stage == "sitemap"


def test_sephora_fr_defaults_to_sitemap_only(monkeypatch):
    from app.crawlers.sephora import SephoraCrawler

    crawler = SephoraCrawler(_fr_site(), limit=1)
    calls: list[str] = []

    class _FakeFetcher:
        def get(self, url: str, **kw):
            calls.append(url)
            if url == _FR_INDEX_URL:
                return _ok(url, _FR_SITEMAP_INDEX)
            if url == _FR_PRODUCTS_URL:
                return _ok(url, _FR_PRODUCTS_SITEMAP)
            raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.delenv("SEPHORA_FR_HTML", raising=False)
    monkeypatch.delenv("SEPHORA_FETCH_PDP", raising=False)
    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    assert calls == [_FR_INDEX_URL, _FR_PRODUCTS_URL]
    assert len(result.products) == 1
    row = result.products[0]
    assert row["sku"] == "P10064237"
    assert "Meteorites Compact" in row["title"]
    assert row["currency"] == "EUR"
    assert row["attributes"]["source"] == "sitemap"
