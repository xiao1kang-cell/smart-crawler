"""批1试点收编集成测试 —— article / sephora 经 CrawlerFetcher 统一入口抓取。

测试策略：
- 完全不触网：monkeypatch crawler.make_fetcher，返回假 fetcher。
- 假 fetcher 内部给 crawler.counter.api_calls += 1，模拟统一入口计数。
- 用精确 fixture HTML 命中解析路径，断言能解析出 ≥1 商品（或 counter 累计）。
"""
from __future__ import annotations

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 共用 helper
# ---------------------------------------------------------------------------

def _site(name: str, url: str, country: str = "US") -> Site:
    return Site(
        site=name,
        url=url,
        country=country,
        proxy_tier="none",
        platform=name,
    )


# ---------------------------------------------------------------------------
# article.py 测试
# ---------------------------------------------------------------------------

# Fixture：sitemap XML（只含一个 /product/ URL）
_ARTICLE_SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://www.article.com/product/42/test-sofa</loc></url>
</urlset>
"""

# Fixture：PDP HTML，含 JSON-LD Product + 价格 DOM
_ARTICLE_PDP = """\
<html>
<head>
  <script type="application/ld+json">
  {
    "@type": "Product",
    "name": "Test Sofa",
    "sku": "P42",
    "description": "A great sofa.",
    "offers": {
      "@type": "Offer",
      "price": "299",
      "availability": "http://schema.org/InStock"
    }
  }
  </script>
</head>
<body>
  <span class="newPrice">$249</span>
  <span class="originalPrice">$299</span>
</body>
</html>
"""


def test_article_routes_through_make_fetcher_and_counts(monkeypatch):
    """article crawl 经统一入口计数，且解析出 ≥1 商品。"""
    from app.crawlers.article import ArticleCrawler

    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [])
    monkeypatch.setattr("app.crawlers.base.get_settings", lambda: {})
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.sleep", lambda self: None)
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.snapshot", lambda self, *a, **kw: None)

    crawler = ArticleCrawler(_site("article", "https://www.article.com"))
    crawler.limit = 1  # 只跑一个 URL，加快测试

    make_fetcher_called = {"n": 0}

    def fake_get(url: str, **kw) -> FetchResult:
        # 每次成功 get 给 counter +1（模拟统一入口行为）
        crawler.counter.api_calls += 1
        if "sitemap" in url:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_ARTICLE_SITEMAP,
                content=_ARTICLE_SITEMAP.encode(),
                final_url=url,
                fetcher="curl_cffi",
            )
        # PDP：final_url 保持 /product/，表示在售
        final = "https://www.article.com/product/42/test-sofa"
        return FetchResult(
            ok=True, url=url, status=200,
            text=_ARTICLE_PDP,
            content=_ARTICLE_PDP.encode(),
            final_url=final,
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            return fake_get(url, **kw)

    def _make_fetcher(**kw):
        make_fetcher_called["n"] += 1
        return _FakeFetcher()

    monkeypatch.setattr(crawler, "make_fetcher", _make_fetcher)

    result = crawler.crawl()

    # 1. make_fetcher 被调用（即走了统一入口）
    assert make_fetcher_called["n"] >= 1, "make_fetcher 未被调用"

    # 2. counter 累加（至少 sitemap + 1 个 PDP = 2 次）
    assert crawler.counter.api_calls >= 2, (
        f"counter.api_calls={crawler.counter.api_calls}，期望 ≥2"
    )

    # 3. 解析出 ≥1 商品
    assert isinstance(result.products, list)
    assert len(result.products) >= 1, (
        f"解析出 0 个商品，notes={result.notes}"
    )

    # 4. 基本字段正确
    p = result.products[0]
    assert p["title"] == "Test Sofa"
    assert p["sku"] == "P42"
    assert p["sale_price"] == 249.0


def test_article_discontinued_does_not_add_product(monkeypatch):
    """final_url 落在 /browse/ 时应计入 discontinued，不产出商品。"""
    from app.crawlers.article import ArticleCrawler

    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [])
    monkeypatch.setattr("app.crawlers.base.get_settings", lambda: {})
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.sleep", lambda self: None)
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.snapshot", lambda self, *a, **kw: None)

    crawler = ArticleCrawler(_site("article", "https://www.article.com"))
    crawler.limit = 1

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "sitemap" in url:
            return FetchResult(
                ok=True, url=url, status=200,
                text=_ARTICLE_SITEMAP,
                content=_ARTICLE_SITEMAP.encode(),
                final_url=url, fetcher="curl_cffi",
            )
        # 301 → /browse → 停售
        return FetchResult(
            ok=True, url=url, status=200,
            text="<html><body>Browse</body></html>",
            content=b"<html><body>Browse</body></html>",
            final_url="https://www.article.com/browse/all",
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    result = crawler.crawl()

    assert len(result.products) == 0, "停售 URL 不应产出商品"
    # discontinued 计入 notes
    notes_text = " ".join(result.notes)
    assert "停售" in notes_text or "discontinued" in notes_text.lower() or "0/" in notes_text


def test_article_guard_called_with_res_status(monkeypatch):
    """guard(res.status or 0, ...) 被正确调用（status=200 时不抛异常）。"""
    from app.crawlers.article import ArticleCrawler

    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [])
    monkeypatch.setattr("app.crawlers.base.get_settings", lambda: {})
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.sleep", lambda self: None)
    monkeypatch.setattr("app.crawlers.article.ArticleCrawler.snapshot", lambda self, *a, **kw: None)

    crawler = ArticleCrawler(_site("article", "https://www.article.com"))
    crawler.limit = 1

    guard_calls: list[tuple] = []
    original_guard = crawler.guard

    def tracking_guard(status: int, where: str = "") -> None:
        guard_calls.append((status, where))
        # 不真的熔断，仅记录

    monkeypatch.setattr(crawler, "guard", tracking_guard)

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        if "sitemap" in url:
            return FetchResult(ok=True, url=url, status=200,
                               text=_ARTICLE_SITEMAP, content=_ARTICLE_SITEMAP.encode(),
                               final_url=url, fetcher="curl_cffi")
        return FetchResult(ok=True, url=url, status=200,
                           text=_ARTICLE_PDP, content=_ARTICLE_PDP.encode(),
                           final_url="https://www.article.com/product/42/test-sofa",
                           fetcher="curl_cffi")

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    crawler.crawl()

    # guard 应被调用，且传入的是整数 status
    assert len(guard_calls) >= 1
    for status, where in guard_calls:
        assert isinstance(status, int), f"guard 收到非 int status: {status!r}"


# ---------------------------------------------------------------------------
# sephora.py 测试
# ---------------------------------------------------------------------------

# Fixture：含 data-product-id 卡片的 HTML（sephora.fr 风格）
_SEPHORA_FR_HTML = """\
<html>
<body>
  <div data-product-id="SEP001">
    <a href="/p/test-product-SEP001">
      <h3>Sephora Brand</h3>
      <span>Beautiful Serum</span>
      <span>Anti-age formula</span>
      <img src="https://cdn.sephora.fr/img/sep001.jpg" />
      <p data-testid="productTile__txt__price">32,99&nbsp;€</p>
    </a>
  </div>
</body>
</html>
"""


def test_sephora_fr_routes_through_make_fetcher_and_counts(monkeypatch):
    """sephora _crawl_fr 经统一入口计数，且解析出 ≥1 商品。"""
    from app.crawlers.sephora import SephoraCrawler

    monkeypatch.setenv("SEPHORA_FR_HTML", "1")
    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [])
    monkeypatch.setattr("app.crawlers.base.get_settings", lambda: {})
    monkeypatch.setattr("app.crawlers.sephora.SephoraCrawler.snapshot", lambda self, *a, **kw: None)

    crawler = SephoraCrawler(_site("sephora_fr", "https://www.sephora.fr/maquillage/", "FR"))
    crawler.limit = 5

    make_fetcher_called = {"n": 0}

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        return FetchResult(
            ok=True, url=url, status=200,
            text=_SEPHORA_FR_HTML,
            content=_SEPHORA_FR_HTML.encode(),
            final_url=url,
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            return fake_get(url, **kw)

    def _make_fetcher(**kw):
        make_fetcher_called["n"] += 1
        return _FakeFetcher()

    monkeypatch.setattr(crawler, "make_fetcher", _make_fetcher)

    result = crawler.crawl()

    # 1. make_fetcher 被调用
    assert make_fetcher_called["n"] >= 1, "make_fetcher 未被调用"

    # 2. counter 累加
    assert crawler.counter.api_calls >= 1, (
        f"counter.api_calls={crawler.counter.api_calls}，期望 ≥1"
    )

    # 3. 解析出 ≥1 商品
    assert len(result.products) >= 1, (
        f"解析出 0 个商品，notes={result.notes}"
    )

    # 4. 基本字段正确
    p = result.products[0]
    assert p["sku"] == "SEP001"
    assert p["title"] == "Beautiful Serum"
    assert p["sale_price"] == 32.99


def test_sephora_fr_blocked_raises_error(monkeypatch):
    """sephora 返回反爬页面时应 raise BlockedError。"""
    from app.crawlers.sephora import SephoraCrawler
    from app.antiban import BlockedError

    monkeypatch.setenv("SEPHORA_FR_HTML", "1")
    monkeypatch.setattr("app.crawlers.base.get_sites", lambda: [])
    monkeypatch.setattr("app.crawlers.base.get_settings", lambda: {})

    crawler = SephoraCrawler(_site("sephora_fr", "https://www.sephora.fr/", "FR"))

    blocked_html = "<html><body>Access Denied by akamai</body></html>"

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        return FetchResult(
            ok=True, url=url, status=200,
            text=blocked_html, content=blocked_html.encode(),
            final_url=url, fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url: str, **kw) -> FetchResult:
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    with pytest.raises(BlockedError):
        crawler.crawl()
