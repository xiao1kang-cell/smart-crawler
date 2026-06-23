"""TDD test: cratebarrel crawler 批C 收编验证。

验证两段计数：
- curl 路径：sitemap_index + sub_sitemap GET → make_fetcher().get() → api_calls += 1 each
- stealth 路径：_enrich_from_pdp 内 StealthyFetcher.fetch → count_browser_fetch 包裹
  → browser_opens += 1（成功），0（失败 403）

批C 收编规则：
- curl_cffi 段：make_fetcher(source="cratebarrel").get() 替代 sess.get()，res.status/res.text 对齐
- stealth 段：StealthyFetcher.fetch 调用用 count_browser_fetch 包裹，kw/solve_cloudflare 等参数原样
- success 标准：cratebarrel 原标准 — status == 200 且 html_content 非空 且不是 Akamai 挑战页
- _session() 改 _headers()，删 proxy 自管，删 creq import
"""
from __future__ import annotations

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SKU = "507728"
_SLUG = "lowell-upholstered-bed"
_PDP_URL = f"https://www.crateandbarrel.com/{_SLUG}/s{_SKU}/"
_SKU_2 = "507729"
_PDP_URL_2 = "https://www.crateandbarrel.com/wood-storage-bench/s507729/"

# Minimal sitemap_index pointing at one PDP sitemap
_SITEMAP_INDEX_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<sitemapindex>"
    "<sitemap><loc>https://www.crateandbarrel.com/assets/sitemap-pdp.xml</loc></sitemap>"
    "<sitemap><loc>https://www.crateandbarrel.com/assets/sitemap-nla-pdp.xml</loc></sitemap>"
    "</sitemapindex>"
)

# Minimal PDP sitemap with one entry
_SITEMAP_PDP_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>"
    f"<url>"
    f"<loc>{_PDP_URL}</loc>"
    f"<image:image>"
    f"<image:loc>https://cb.scene7.com/is/image/Crate/LowellBed</image:loc>"
    f"<image:title>Lowell Upholstered King Bed - image 0 of 6</image:title>"
    f"</image:image>"
    f"</url>"
    "</urlset>"
)

_SITEMAP_PDP_TWO_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<urlset xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>"
    f"<url>"
    f"<loc>{_PDP_URL}</loc>"
    f"<image:image>"
    f"<image:loc>https://cb.scene7.com/is/image/Crate/LowellBed</image:loc>"
    f"<image:title>Lowell Upholstered King Bed - image 0 of 6</image:title>"
    f"</image:image>"
    f"</url>"
    f"<url>"
    f"<loc>{_PDP_URL_2}</loc>"
    f"<image:image>"
    f"<image:loc>https://cb.scene7.com/is/image/Crate/WoodBench</image:loc>"
    f"<image:title>Wood Storage Bench - image 0 of 4</image:title>"
    f"</image:image>"
    f"</url>"
    "</urlset>"
)


def _site() -> Site:
    s = Site()
    s.site = "cratebarrel"
    s.url = "https://www.crateandbarrel.com"
    s.country = "US"
    s.proxy_tier = "none"
    s.platform = "cratebarrel"
    s.brand = "Crate & Barrel"
    return s


# ---------------------------------------------------------------------------
# Shared fake fetcher factory
# ---------------------------------------------------------------------------

def _make_fake_fetcher(crawler, url_map: dict):
    """Fake CrawlerFetcher whose .get() increments api_calls."""
    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            if url in url_map:
                return url_map[url]
            return FetchResult(
                ok=False, url=url, status=404,
                text="", content=b"", final_url=url, fetcher="curl_cffi",
            )
    return _FakeFetcher()


# ---------------------------------------------------------------------------
# Test: curl path — sitemap_index + sub_sitemap GET counts api_calls
# ---------------------------------------------------------------------------

def test_cratebarrel_curl_path_counts_api(monkeypatch):
    """curl 路径：sitemap_index(1) + sitemap-pdp.xml(1) = api_calls >= 2，product 正确解析。"""
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())
    crawler.limit = 1

    url_map = {
        "https://www.crateandbarrel.com/assets/sitemap-index.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            status=200,
            text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            fetcher="curl_cffi",
        ),
        "https://www.crateandbarrel.com/assets/sitemap-pdp.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            status=200,
            text=_SITEMAP_PDP_XML,
            content=_SITEMAP_PDP_XML.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    # sitemap_index + sitemap-pdp.xml = at least 2
    assert crawler.counter.api_calls >= 2, (
        f"Expected >=2 api_calls (sitemap_index+sitemap-pdp.xml), "
        f"got {crawler.counter.api_calls}. Notes: {result.notes}"
    )
    assert isinstance(result.products, list), "result.products 应为 list"
    assert len(result.products) >= 1, (
        f"Expected >=1 product, got {len(result.products)}. Notes: {result.notes}"
    )

    p = result.products[0]
    assert p["sku"] == _SKU, f"Expected sku={_SKU!r}, got {p['sku']!r}"
    assert "Lowell" in p["title"], f"Expected 'Lowell' in title, got {p['title']!r}"
    assert p["product_url"] == _PDP_URL
    assert p["site"] == "cratebarrel"
    assert p["currency"] == "USD"


def test_cratebarrel_counts_full_sitemap_total_even_when_limited(monkeypatch):
    """limit controls emitted rows only; total_product_count must still count the full sitemap."""
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())
    crawler.limit = 1

    url_map = {
        "https://www.crateandbarrel.com/assets/sitemap-index.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            status=200,
            text=_SITEMAP_INDEX_XML,
            content=_SITEMAP_INDEX_XML.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            fetcher="curl_cffi",
        ),
        "https://www.crateandbarrel.com/assets/sitemap-pdp.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            status=200,
            text=_SITEMAP_PDP_TWO_XML,
            content=_SITEMAP_PDP_TWO_XML.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_detail_parse"


def test_cratebarrel_marks_partial_when_sub_sitemap_fails(monkeypatch):
    """A failed PDP sitemap shard means the discovered denominator is incomplete."""
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())
    crawler.limit = 5
    index_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<sitemapindex>"
        "<sitemap><loc>https://www.crateandbarrel.com/assets/sitemap-pdp.xml</loc></sitemap>"
        "<sitemap><loc>https://www.crateandbarrel.com/assets/sitemap-pdp1.xml</loc></sitemap>"
        "</sitemapindex>"
    )

    url_map = {
        "https://www.crateandbarrel.com/assets/sitemap-index.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            status=200,
            text=index_xml,
            content=index_xml.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-index.xml",
            fetcher="curl_cffi",
        ),
        "https://www.crateandbarrel.com/assets/sitemap-pdp.xml": FetchResult(
            ok=True,
            url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            status=200,
            text=_SITEMAP_PDP_XML,
            content=_SITEMAP_PDP_XML.encode(),
            final_url="https://www.crateandbarrel.com/assets/sitemap-pdp.xml",
            fetcher="curl_cffi",
        ),
        "https://www.crateandbarrel.com/assets/sitemap-pdp1.xml": FetchResult(
            ok=False,
            url="https://www.crateandbarrel.com/assets/sitemap-pdp1.xml",
            status=503,
            text="",
            content=b"",
            final_url="https://www.crateandbarrel.com/assets/sitemap-pdp1.xml",
            fetcher="curl_cffi",
        ),
    }

    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_fake_fetcher(crawler, url_map))
    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)

    result = crawler.crawl()

    assert len(result.products) == 1
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_discovery"
    assert "1/2" in result.coverage_reason


def test_cratebarrel_parse_sitemap_entry_not_degraded():
    """_parse_sitemap_entry 直接单元测试，确认 XML 节点解析不退化。"""
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())

    block = (
        f"<loc>{_PDP_URL}</loc>"
        "<image:image>"
        "<image:loc>https://cb.scene7.com/is/image/Crate/LowellBed</image:loc>"
        "<image:title>Lowell Upholstered King Bed - image 0 of 6</image:title>"
        "</image:image>"
    )
    row = crawler._parse_sitemap_entry(block)

    assert row is not None, "_parse_sitemap_entry 应成功解析合法节点"
    assert row["sku"] == _SKU
    assert row["title"] == "Lowell Upholstered King Bed"
    assert row["image_urls"] == ["https://cb.scene7.com/is/image/Crate/LowellBed"]
    assert row["product_url"] == _PDP_URL
    assert row["site"] == "cratebarrel"
    assert row["status"] == "on_sale"
    assert row["currency"] == "USD"


# ---------------------------------------------------------------------------
# Test: stealth path — _enrich_from_pdp StealthyFetcher goes through count_browser_fetch
# ---------------------------------------------------------------------------

# PDP HTML with JSON-LD Product — must pass _is_akamai_challenge (> 10KB, no Akamai markers)
import json as _json

_JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Lowell Upholstered King Bed",
    "sku": _SKU,
    "description": "A refined upholstered bed frame.",
    "image": ["https://cb.scene7.com/is/image/Crate/LowellBed"],
    "offers": {
        "@type": "Offer",
        "price": "1299.00",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "234",
    },
}

_PDP_HTML = (
    "<html><head>"
    '<script type="application/ld+json">'
    + _json.dumps(_JSONLD_PRODUCT)
    + "</script>"
    + "</head><body>"
    + " " * 15000  # must be > 10000 chars to pass _is_akamai_challenge
    + "</body></html>"
)


def _make_akamai_fetcher(crawler):
    """Fake fetcher whose .get() always returns an Akamai challenge page
    (forces the stealth fallback path in _enrich_from_pdp).
    Increments api_calls like the real fetcher does.
    """
    _AKAMAI_TEXT = "<html>" + "x" * 5000 + "akam-sw.js</html>"

    class _AkamaiChallengeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            crawler.counter.api_calls += 1
            return FetchResult(
                ok=True, url=url, status=200,
                text=_AKAMAI_TEXT,
                content=_AKAMAI_TEXT.encode(),
                final_url=url, fetcher="curl_cffi",
            )
    return _AkamaiChallengeFetcher()


def test_cratebarrel_stealth_path_counts_browser_opens(monkeypatch):
    """_enrich_from_pdp 内 StealthyFetcher.fetch 经 count_browser_fetch 包裹后，
    成功时 browser_opens += 1。

    只 monkeypatch StealthyFetcher.fetch，不 mock count_browser_fetch，
    确认计数逻辑路径完整。make_fetcher 返回 Akamai 挑战页，强迫走 stealth 路径。
    """
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())

    # Fake page object matching what StealthyFetcher returns
    class _FakePage:
        status = 200
        html_content = _PDP_HTML
        body = None

    class _FakeStealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            return _FakePage()

    import sys
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcher
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    # Patch stealth_kwargs to avoid filesystem side effects
    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    # make_fetcher returns a fake fetcher that always returns Akamai challenge
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_akamai_fetcher(crawler))

    assert crawler.counter.browser_opens == 0

    rows = [{"product_url": _PDP_URL, "sku": _SKU, "title": "Lowell Bed",
             "image_urls": [], "description": None}]
    ok = crawler._enrich_from_pdp(rows)

    assert crawler.counter.browser_opens == 1, (
        f"Expected browser_opens=1 after successful stealth PDP fetch, "
        f"got {crawler.counter.browser_opens}"
    )
    assert ok == 1, f"Expected 1 successful PDP enrich, got {ok}"


def test_cratebarrel_stealth_failure_does_not_count_browser_opens(monkeypatch):
    """stealth 返回 403 时，browser_opens 不增加。"""
    from app.crawlers.cratebarrel import CrateBarrelCrawler

    crawler = CrateBarrelCrawler(_site())

    class _FakePageBlocked:
        status = 403
        html_content = None
        body = None

    class _FakeStealthyFetcherBlocked:
        @staticmethod
        def fetch(url, **kw):
            return _FakePageBlocked()

    import sys
    fake_scrapling_fetchers = type(sys)("scrapling.fetchers")
    fake_scrapling_fetchers.StealthyFetcher = _FakeStealthyFetcherBlocked
    monkeypatch.setitem(sys.modules, "scrapling", type(sys)("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling_fetchers)

    import app.crawlers._stealth_config as _sc
    monkeypatch.setattr(_sc, "stealth_kwargs", lambda **kw: {"headless": True})

    monkeypatch.setattr(crawler, "sleep", lambda: None)
    monkeypatch.setattr(crawler, "snapshot", lambda name, content: None)
    monkeypatch.setattr(crawler, "make_fetcher",
                        lambda **kw: _make_akamai_fetcher(crawler))

    rows = [{"product_url": _PDP_URL, "sku": _SKU, "title": "Lowell Bed",
             "image_urls": [], "description": None}]
    ok = crawler._enrich_from_pdp(rows)

    assert crawler.counter.browser_opens == 0, (
        f"Expected browser_opens=0 on stealth failure (403), "
        f"got {crawler.counter.browser_opens}"
    )
    assert ok == 0, f"Expected 0 successful PDP enrich on stealth failure, got {ok}"
