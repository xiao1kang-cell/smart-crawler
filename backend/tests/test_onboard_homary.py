"""TDD test: verify homary crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Key cases:
  - sitemap .xml.gz: must use res.content for gzip.decompress
  - plain sitemap fallback (bad gzip → decode as text)
  - product page parsed correctly from meta/DOM
  - counter.api_calls accumulated across sitemap + product fetches
"""
from __future__ import annotations

import gzip

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.homary.com"
_SITEMAP_ITEM_GZ = f"{_BASE_URL}/sitemaps/google_sitemap_item_us.xml.gz"
_SITEMAP_BEST_GZ = f"{_BASE_URL}/sitemaps/google_sitemap_best_sellers_us.xml.gz"
_PRODUCT_URL = f"{_BASE_URL}/item/modern-sofa-12345.html"

_SITEMAP_ITEM_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_PRODUCT_URL}</loc></url>
</urlset>
"""

_SITEMAP_BEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.homary.com/item/modern-sofa-12345.html</loc></url>
</urlset>
"""

_SITEMAP_ITEM_GZ_BYTES = gzip.compress(_SITEMAP_ITEM_XML.encode("utf-8"))
_SITEMAP_BEST_GZ_BYTES = gzip.compress(_SITEMAP_BEST_XML.encode("utf-8"))

_PRODUCT_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Modern Sofa | Homary" />
  <meta property="og:description" content="A comfortable modern sofa." />
  <meta property="og:image" content="https://su-cdn.com/images/sofa.jpg" />
</head>
<body>
  <nav class="breadcrumb"><a href="/living-room">Living Room</a><a href="/sofas">Sofas</a></nav>
  <span class="price">$499.99</span>
  <img src="https://su-cdn.com/images/sofa.jpg" />
</body>
</html>
"""


def _site() -> Site:
    s = Site()
    s.site = "homary_us"
    s.url = _BASE_URL
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "nuxt"
    s.brand = "Homary"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict[str, FetchResult]):
    """Return a fake fetcher whose .get() looks up url_map and increments counter."""

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(ok=False, url=url, status=404,
                               text="", content=b"", final_url=url, fetcher="curl_cffi")

    return _FakeFetcher()


def _make_crawler(site: Site, limit: int = 1):
    from app.crawlers.homary import HomaryCrawler
    from app.crawlers.base import BaseCrawler

    crawler = HomaryCrawler.__new__(HomaryCrawler)
    BaseCrawler.__init__(crawler, site)
    crawler.limit = limit
    crawler.cc = site.country.lower()
    return crawler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_homary_gzip_sitemap_uses_res_content(monkeypatch):
    """Sitemap .xml.gz: _sitemap_urls must use res.content (not res.text) to decompress."""
    site = _site()
    crawler = _make_crawler(site, limit=1)

    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text="",                         # text is garbage for raw gzip
            content=_SITEMAP_ITEM_GZ_BYTES,  # real gzip bytes in .content
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="",
            content=_SITEMAP_BEST_GZ_BYTES,
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert len(result.products) >= 1, (
        f"Gzip sitemap not decompressed correctly. Notes: {result.notes}"
    )
    p = result.products[0]
    assert p["sku"] == "12345"
    assert "sofa" in p["title"].lower()


def test_homary_counter_accumulates(monkeypatch):
    """counter.api_calls must increase: sitemap(x2) + product(x1) = >= 3."""
    site = _site()
    crawler = _make_crawler(site, limit=1)

    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text="", content=_SITEMAP_ITEM_GZ_BYTES,
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="", content=_SITEMAP_BEST_GZ_BYTES,
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    calls_before = crawler.counter.api_calls
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    crawler.crawl()

    assert crawler.counter.api_calls > calls_before, (
        f"api_calls did not increase (still {crawler.counter.api_calls})"
    )
    # sitemap_item + sitemap_best_sellers + product page = 3
    assert crawler.counter.api_calls >= 3, (
        f"Expected >= 3 api_calls, got {crawler.counter.api_calls}"
    )


def test_homary_product_parsed_correctly(monkeypatch):
    """Product fields: sku, title, sale_price, currency, site, brand."""
    site = _site()
    crawler = _make_crawler(site, limit=1)

    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text="", content=_SITEMAP_ITEM_GZ_BYTES,
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="", content=_SITEMAP_BEST_GZ_BYTES,
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert len(result.products) >= 1, f"No products parsed. Notes: {result.notes}"
    p = result.products[0]
    assert p["sku"] == "12345"
    assert p["sale_price"] == 499.99
    assert p["currency"] == "USD"
    assert p["site"] == "homary_us"
    assert p["brand"] == "Homary"
    assert p["product_url"] == _PRODUCT_URL


def test_homary_concurrency_requires_proxy_lease_config():
    site = _site()
    site.crawler_config = {"detail_concurrency": 8}
    crawler = _make_crawler(site, limit=1)

    assert crawler._detail_concurrency() == 1
    assert crawler._failed_product_retry_concurrency() == 1
    assert crawler._proxy_lease_ttl_sec(default=0) == 0
    assert crawler._rate_interval_sec() is None

    site.crawler_config = {
        "proxy_lease_ttl_sec": 300,
        "detail_concurrency": 8,
        "failed_product_retry_concurrency": 5,
        "rate_interval_sec": 0.05,
    }

    assert crawler._detail_concurrency() == 8
    assert crawler._failed_product_retry_concurrency() == 5
    assert crawler._proxy_lease_ttl_sec(default=0) == 300
    assert crawler._rate_interval_sec() == 0.05


def test_homary_parallel_pdp_uses_proxy_lease_context(monkeypatch):
    site = _site()
    site.crawler_config = {
        "proxy_lease_ttl_sec": 300,
        "detail_concurrency": 2,
        "rate_interval_sec": 0.05,
    }
    crawler = _make_crawler(site, limit=10)
    second_product_url = f"{_BASE_URL}/item/modern-chair-67890.html"
    item_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_PRODUCT_URL}</loc></url>
  <url><loc>{second_product_url}</loc></url>
</urlset>
"""
    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text="", content=gzip.compress(item_xml.encode("utf-8")),
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="", content=_SITEMAP_BEST_GZ_BYTES,
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
        second_product_url: FetchResult(
            ok=True, url=second_product_url, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=second_product_url, fetcher="curl_cffi",
        ),
    }
    make_fetcher_calls: list[dict] = []

    def fake_make_fetcher(**kw):
        make_fetcher_calls.append(kw)
        return _make_fake_fetcher(crawler, url_map)

    monkeypatch.setattr(crawler, "make_fetcher", fake_make_fetcher)
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert {p["sku"] for p in result.products} == {"12345", "67890"}
    product_calls = [kw for kw in make_fetcher_calls if kw["kind"] == "product"]
    assert len(product_calls) == 2
    assert all(kw["proxy_lease_ttl_sec"] == 300 for kw in product_calls)
    assert all(kw["rate_interval_sec"] == 0.05 for kw in product_calls)
    assert any("并发 2" in note for note in result.notes)


def test_homary_sitemap_fetches_are_not_product_frontier(monkeypatch):
    site = _site()
    crawler = _make_crawler(site, limit=10)
    invalid_item_url = f"{_BASE_URL}/item/category-landing.html"
    item_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_PRODUCT_URL}</loc></url>
  <url><loc>{invalid_item_url}</loc></url>
</urlset>
"""
    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text="", content=gzip.compress(item_xml.encode("utf-8")),
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="", content=_SITEMAP_BEST_GZ_BYTES,
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }
    requests_seen: list[tuple[str, str]] = []

    class _FakeFetcher:
        def __init__(self, kind: str):
            self.kind = kind

        def get(self, url: str, **kw) -> FetchResult:
            requests_seen.append((self.kind, url))
            crawler.counter.api_calls += 1
            return url_map[url]

    def fake_make_fetcher(**kw):
        return _FakeFetcher(kw["kind"])

    monkeypatch.setattr(crawler, "make_fetcher", fake_make_fetcher)
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert result.total_product_count == 1
    assert [p["sku"] for p in result.products] == ["12345"]
    assert ("sitemap", _SITEMAP_ITEM_GZ) in requests_seen
    assert ("sitemap", _SITEMAP_BEST_GZ) in requests_seen
    assert ("product", _PRODUCT_URL) in requests_seen
    assert all(url != invalid_item_url for _kind, url in requests_seen)


def test_homary_failed_product_retry_only_uses_given_urls(monkeypatch):
    site = _site()
    crawler = _make_crawler(site, limit=999)

    url_map = {
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }
    sitemap_kinds: list[str] = []

    def fake_sitemap(_fetcher, kind: str) -> list[str]:
        sitemap_kinds.append(kind)
        return [_PRODUCT_URL] if kind == "best_sellers" else []

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "_sitemap_urls", fake_sitemap)
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl_failed_products([_PRODUCT_URL])

    assert [p["sku"] for p in result.products] == ["12345"]
    assert result.total_product_count == 1
    assert sitemap_kinds == ["best_sellers"]


def test_homary_bad_gzip_falls_back_to_text(monkeypatch):
    """When content is not valid gzip, sitemap should fall back to raw text decode."""
    site = _site()
    crawler = _make_crawler(site, limit=1)

    # Send plain XML as content (not gzip) — should be handled by OSError fallback
    plain_bytes = _SITEMAP_ITEM_XML.encode("utf-8")

    url_map = {
        _SITEMAP_ITEM_GZ: FetchResult(
            ok=True, url=_SITEMAP_ITEM_GZ, status=200,
            text=_SITEMAP_ITEM_XML,
            content=plain_bytes,  # NOT gzip, triggers OSError → fallback
            final_url=_SITEMAP_ITEM_GZ, fetcher="curl_cffi",
        ),
        _SITEMAP_BEST_GZ: FetchResult(
            ok=True, url=_SITEMAP_BEST_GZ, status=200,
            text="", content=b"",
            final_url=_SITEMAP_BEST_GZ, fetcher="curl_cffi",
        ),
        _PRODUCT_URL: FetchResult(
            ok=True, url=_PRODUCT_URL, status=200,
            text=_PRODUCT_HTML, content=_PRODUCT_HTML.encode("utf-8"),
            final_url=_PRODUCT_URL, fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # Should still parse product via fallback text decode
    assert len(result.products) >= 1, (
        f"Bad-gzip fallback failed. Notes: {result.notes}"
    )
