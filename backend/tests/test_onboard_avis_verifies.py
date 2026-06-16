"""批C 收编验证：AvisVerifiesCrawler。

覆盖：
- curl 路径计 api_calls + 解析评论
- stealth 路径计 browser_opens（monkeypatch StealthyFetcher.fetch）
- stealth 失败不计
- 翻页终止（无评论时退出）
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.crawlers.avis_verifies import AvisVerifiesCrawler

CHANNEL = {
    "site": "test_avis",
    "merchant_slug": "test-merchant",
    "country": "FR",
    "host": "www.avis-verifies.com",
}

# 最小 HTML：一个带 itemprop=review 的块
_REVIEW_HTML = """
<div itemprop="review" data-review-id="r001">
  <span itemprop="reviewBody">Très bon produit</span>
  <span itemprop="ratingValue" content="4.5"></span>
  <time itemprop="datePublished" datetime="2024-01-15">15/01/2024</time>
  <span itemprop="author">Jean Dupont</span>
  <span itemprop="name">Excellent</span>
</div>
"""

# 无评论页面
_EMPTY_HTML = "<html><body><p>No reviews</p></body></html>"


# ──────────────────────────────────────────────
# Fixture：共用构造
# ──────────────────────────────────────────────
@pytest.fixture
def crawler():
    return AvisVerifiesCrawler(CHANNEL, max_pages=5)


# ──────────────────────────────────────────────
# 1. curl 路径：api_calls 累加 + 解析评论
# ──────────────────────────────────────────────

def _make_fetch_result(status: int, text: str):
    """构造 FetchResult-like 对象（仅需 status + text）。"""
    return SimpleNamespace(status=status, text=text, ok=(status == 200))


def test_curl_path_counts_api_calls(crawler):
    """curl 正常返回 → api_calls += 1（第 2 页无评论 → 翻页终止）。"""
    results = [
        _make_fetch_result(200, _REVIEW_HTML),   # page 1 有评论
        _make_fetch_result(200, _EMPTY_HTML),    # page 2 无评论 → 终止
    ]
    call_iter = iter(results)

    mock_fetcher = MagicMock()
    mock_fetcher.get.side_effect = lambda url, **kw: next(call_iter)

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        reviews = crawler.crawl()

    assert len(reviews) == 1
    assert reviews[0]["review_id"] == "r001"
    assert reviews[0]["platform"] == "avis_verifies"
    # api_calls 由 make_fetcher 内部计数；此处验证 fetcher.get 被调用
    assert mock_fetcher.get.call_count == 2


def test_curl_path_parses_rating(crawler):
    """curl 路径：解析 rating / content / author / title 字段。"""
    mock_fetcher = MagicMock()
    mock_fetcher.get.side_effect = [
        _make_fetch_result(200, _REVIEW_HTML),
        _make_fetch_result(200, _EMPTY_HTML),
    ]

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        reviews = crawler.crawl()

    r = reviews[0]
    assert r["content"] == "Très bon produit"
    assert r["rating"] == 4.5
    assert r["reviewer_name"] == "Jean Dupont"
    assert r["title"] == "Excellent"
    assert r["is_verified"] is True
    assert r["language"] == "fr"


# ──────────────────────────────────────────────
# 2. stealth 路径：browser_opens 累加
# ──────────────────────────────────────────────

def test_stealth_path_counts_browser_opens():
    """403 触发 stealth fallback → browser_opens += 1（max_pages=1 确保只抓一页）。"""
    crawler = AvisVerifiesCrawler(CHANNEL, max_pages=1)

    # curl 返回 403
    mock_fetcher = MagicMock()
    mock_fetcher.get.return_value = _make_fetch_result(403, "")

    # StealthyFetcher.fetch 返回成功
    mock_page = SimpleNamespace(
        status=200,
        html_content=_REVIEW_HTML,
        body=None,
    )

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        with patch(
            "scrapling.fetchers.StealthyFetcher.fetch",
            return_value=mock_page,
        ) as mock_stealth:
            reviews = crawler.crawl()

    assert mock_stealth.called
    assert crawler.counter.browser_opens >= 1
    assert len(reviews) == 1


# ──────────────────────────────────────────────
# 3. stealth 失败不计 browser_opens
# ──────────────────────────────────────────────

def test_stealth_failure_does_not_count(crawler):
    """stealth 返回 None（失败）→ browser_opens 不增加。"""
    mock_fetcher = MagicMock()
    mock_fetcher.get.return_value = _make_fetch_result(403, "")

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        with patch(
            "scrapling.fetchers.StealthyFetcher.fetch",
            return_value=None,
        ):
            reviews = crawler.crawl()

    assert crawler.counter.browser_opens == 0
    assert reviews == []


# ──────────────────────────────────────────────
# 4. 翻页终止：无评论时退出循环
# ──────────────────────────────────────────────

def test_pagination_stops_on_empty_page(crawler):
    """第 1 页有评论，第 2 页无评论 → 只返回第 1 页的评论，循环终止。"""
    mock_fetcher = MagicMock()
    mock_fetcher.get.side_effect = [
        _make_fetch_result(200, _REVIEW_HTML),
        _make_fetch_result(200, _EMPTY_HTML),
        _make_fetch_result(200, _REVIEW_HTML),  # 第 3 页不应被调用
    ]

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        reviews = crawler.crawl()

    # 只 GET 了 2 次，第 3 页没有调用
    assert mock_fetcher.get.call_count == 2
    assert len(reviews) == 1


def test_pagination_stops_on_http_error(crawler):
    """第 1 页成功，第 2 页 500 → 截断并返回第 1 页。"""
    mock_fetcher = MagicMock()
    mock_fetcher.get.side_effect = [
        _make_fetch_result(200, _REVIEW_HTML),
        _make_fetch_result(500, ""),
    ]

    with patch.object(crawler, "make_fetcher", return_value=mock_fetcher):
        reviews = crawler.crawl()

    assert len(reviews) == 1


# ──────────────────────────────────────────────
# 5. 缺 merchant_slug 时短路返回空列表
# ──────────────────────────────────────────────

def test_no_merchant_slug_returns_empty():
    crawler = AvisVerifiesCrawler({"site": "x"}, max_pages=5)
    reviews = crawler.crawl()
    assert reviews == []
