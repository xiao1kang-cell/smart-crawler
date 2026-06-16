"""TDD test: verify reviews_io crawler routes through BaseCrawler.make_fetcher
and increments counter.api_calls via the unified fetch layer.

Before migration: ReviewsIoCrawler uses raw curl_cffi Session directly;
  make_fetcher is never called; counter.api_calls stays 0.
After migration: every HTTP GET goes through the unified CrawlerFetcher;
  counter.api_calls is incremented on each successful fetch.

Reviews.io API: public, no proxy required.
  GET https://api.reviews.io/merchant/reviews?store={store}&per_page=100&page=N
  Returns {stats, reviews:[...], total_pages}
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture data — aligned with real reviews.io API structure
# ---------------------------------------------------------------------------

def _review(rid: int, rating: int = 5) -> dict:
    return {
        "store_review_id": rid,
        "rating": rating,
        "title": f"Great product #{rid}",
        "comments": f"This is review {rid}",
        "date_created": "2024-03-15T10:00:00+00:00",
        "order_number": f"ORD-{rid}",
        "tags": ["quality", "fast shipping"],
        "replies": [],
        "reviewer": {
            "first_name": "Jane",
            "last_name": "Doe",
        },
    }


_PAGE1_RESPONSE = {
    "stats": {
        "total_reviews": 3,
        "average_rating": 4.7,
    },
    "reviews": [_review(101), _review(102)],
    "total_pages": 2,
}

_PAGE2_RESPONSE = {
    "stats": {
        "total_reviews": 3,
        "average_rating": 4.7,
    },
    "reviews": [_review(103, rating=4)],
    "total_pages": 2,
}

_EMPTY_RESPONSE = {
    "stats": {},
    "reviews": [],
    "total_pages": 1,
}


def _channel() -> dict:
    return {
        "platform": "reviews_io",
        "store": "aosom-uk",
        "site": "reviews_io_test",
        "max_pages": 10,
    }


# ---------------------------------------------------------------------------
# Helper to build FetchResult from a dict
# ---------------------------------------------------------------------------

def _ok_result(url: str, body: dict) -> FetchResult:
    text = json.dumps(body)
    return FetchResult(
        ok=True,
        url=url,
        status=200,
        text=text,
        content=text.encode(),
        final_url=url,
        fetcher="curl_cffi",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reviews_io_routes_through_make_fetcher_and_counts(monkeypatch):
    """After migration, all HTTP GETs go through make_fetcher → counter increments."""
    from app.crawlers.reviews_io import ReviewsIoCrawler

    crawler = ReviewsIoCrawler(_channel())

    calls: list[str] = []
    page_hits: dict[int, int] = {}

    def fake_get(url: str, **kw) -> FetchResult:
        calls.append(url)
        crawler.counter.api_calls += 1

        # Extract page param from kwargs params dict
        params = kw.get("params") or {}
        page = int(params.get("page", 1))
        page_hits[page] = page_hits.get(page, 0) + 1

        if page == 1:
            return _ok_result(url, _PAGE1_RESPONSE)
        elif page == 2:
            return _ok_result(url, _PAGE2_RESPONSE)
        else:
            return _ok_result(url, _EMPTY_RESPONSE)

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())

    reviews = crawler.crawl()

    # Must have gone through make_fetcher (counter incremented)
    assert crawler.counter.api_calls >= 1, (
        f"Expected >=1 api_calls, got {crawler.counter.api_calls}. "
        f"URLs fetched: {calls}"
    )

    # Must have parsed reviews from both pages
    assert isinstance(reviews, list)
    assert len(reviews) == 3, (
        f"Expected 3 reviews (2 on page1 + 1 on page2), got {len(reviews)}. "
        f"Notes: {crawler.notes}"
    )

    # Verify review structure from _map
    first = reviews[0]
    assert first["review_id"] == "101"
    assert first["platform"] == "reviews_io"
    assert first["site"] == "reviews_io_test"
    assert first["reviewer_name"] == "Jane Doe"
    assert first["rating"] == 5
    assert first["title"] == "Great product #101"
    assert first["content"] == "This is review 101"
    assert first["order_id"] == "ORD-101"
    assert first["reply_content"] is None  # no replies


def test_reviews_io_pagination_terminates_on_total_pages(monkeypatch):
    """Pagination terminates when page >= total_pages (not relying on empty list)."""
    from app.crawlers.reviews_io import ReviewsIoCrawler

    crawler = ReviewsIoCrawler(_channel())

    page_hits: list[int] = []

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        params = kw.get("params") or {}
        page = int(params.get("page", 1))
        page_hits.append(page)

        if page == 1:
            return _ok_result(url, _PAGE1_RESPONSE)   # total_pages=2
        elif page == 2:
            return _ok_result(url, _PAGE2_RESPONSE)   # total_pages=2, page==total_pages → break
        else:
            # Should never reach here — loop should break at page==total_pages
            return _ok_result(url, _EMPTY_RESPONSE)

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    reviews = crawler.crawl()

    # Should have fetched exactly 2 pages
    assert page_hits == [1, 2], (
        f"Expected pages [1, 2] fetched (terminates at total_pages=2), got {page_hits}"
    )
    assert len(reviews) == 3


def test_reviews_io_stops_on_empty_reviews(monkeypatch):
    """Pagination also terminates when reviews list is empty (before total_pages)."""
    from app.crawlers.reviews_io import ReviewsIoCrawler

    crawler = ReviewsIoCrawler(_channel())

    page_hits: list[int] = []

    # Single page: total_pages=5 but reviews empty on page1 → immediate break
    _SINGLE_PAGE = {
        "stats": {"total_reviews": 0},
        "reviews": [],
        "total_pages": 5,
    }

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        params = kw.get("params") or {}
        page = int(params.get("page", 1))
        page_hits.append(page)
        return _ok_result(url, _SINGLE_PAGE)

    class _F:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _F())

    reviews = crawler.crawl()

    # Should stop after page 1 because reviews is empty
    assert page_hits == [1], (
        f"Expected only page 1 (empty reviews → break), got {page_hits}"
    )
    assert reviews == []


def test_reviews_io_use_proxy_false(monkeypatch):
    """make_fetcher must be called with use_proxy=False (reviews.io is public API)."""
    from app.crawlers.reviews_io import ReviewsIoCrawler

    crawler = ReviewsIoCrawler(_channel())

    captured_kwargs: list[dict] = []

    def fake_get(url: str, **kw) -> FetchResult:
        crawler.counter.api_calls += 1
        params = kw.get("params") or {}
        page = int(params.get("page", 1))
        # Return page1 data, then empty to terminate on page 2
        if page == 1:
            body = {
                "stats": {"total_reviews": 1},
                "reviews": [_review(201)],
                "total_pages": 1,  # only 1 page → terminates after page 1
            }
        else:
            body = _EMPTY_RESPONSE
        return _ok_result(url, body)

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

    def spying_make_fetcher(**kw):
        captured_kwargs.append(kw)
        return _FakeFetcher()

    monkeypatch.setattr(crawler, "make_fetcher", spying_make_fetcher)

    crawler.crawl()

    assert len(captured_kwargs) == 1, "make_fetcher should be called exactly once"
    assert captured_kwargs[0].get("use_proxy") is False, (
        f"Expected use_proxy=False (public API, no proxy needed), "
        f"got: {captured_kwargs[0]}"
    )
