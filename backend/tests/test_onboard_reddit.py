"""TDD test: verify reddit crawler routes requests.get through count_api_fetch
counter so each successful Reddit/Arctic-Shift API call increments api_calls.

Architecture note:
  RedditFetcher is NOT a BaseCrawler subclass; it is a standalone helper.
  The migration injects an optional `counter` (CrawlCounter) into RedditFetcher.
  _get() calls count_api_fetch-equivalent logic: on HTTP 200 → counter.api_calls += 1.
  _SLEEP, Arctic Shift fallback, pagination, proxy handling → untouched.

Before migration: counter stays None / api_calls never incremented.
After migration: every successful sess.get() (status 200) increments api_calls;
  non-200 / exceptions do NOT increment.
"""
from __future__ import annotations

import types
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake response helpers
# ---------------------------------------------------------------------------

def _fake_resp(status: int, body: dict) -> object:
    """Build a minimal fake requests.Response-like object."""
    resp = types.SimpleNamespace()
    resp.status_code = status
    resp.raise_for_status = (
        (lambda: None)
        if status < 400
        else (lambda: (_ for _ in ()).throw(Exception(f"HTTP {status}")))
    )
    resp.json = lambda: body
    return resp


def _reddit_listing(posts: list[dict]) -> dict:
    """Wrap posts in Reddit JSON listing envelope."""
    return {
        "data": {
            "children": [{"data": p} for p in posts],
            "after": None,
        }
    }


def _make_post(n: int) -> dict:
    return {
        "id": f"post{n}",
        "title": f"Post {n}",
        "author": f"user{n}",
        "score": n * 10,
        "selftext": f"body {n}",
        "subreddit": "gadgets",
        "subreddit_name_prefixed": "r/gadgets",
        "num_comments": n,
        "upvote_ratio": 0.9,
        "link_flair_text": None,
        "permalink": f"/r/gadgets/comments/post{n}/post_{n}/",
        "created_utc": 1700000000 + n,
    }


# ---------------------------------------------------------------------------
# Shared setup: monkeypatch _requests.get and time.sleep inside reddit module
# ---------------------------------------------------------------------------

def _setup_monkeypatch(monkeypatch, responses: list):
    """
    Patch reddit module's requests Session so sess.get returns fake responses
    in order. Also patch time.sleep to skip _SLEEP delay.
    Returns a call_log list that records every (url, params) pair.
    """
    import app.crawlers.reddit as reddit_mod

    call_log: list[tuple[str, dict]] = []
    call_idx = [0]

    class _FakeSession:
        headers = {}
        proxies: dict = {}

        def update(self, h):  # headers.update
            pass

        def get(self, url: str, params=None, timeout=30):
            call_log.append((url, params or {}))
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(responses):
                return responses[idx]
            # Default: empty Reddit listing (pagination terminator)
            return _fake_resp(200, _reddit_listing([]))

    fake_sess = _FakeSession()
    fake_sess.headers = _FakeSession()  # sess.headers.update(...)

    # Patch _requests.Session() to return our fake session
    monkeypatch.setattr(
        reddit_mod._requests, "Session",
        lambda: fake_sess,
    )
    # Suppress _SLEEP delay
    monkeypatch.setattr(reddit_mod.time, "sleep", lambda _: None)

    return call_log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRedditFetcherCountApiCalls:
    """After migration, _get() increments counter.api_calls on each 200 response."""

    def test_single_subreddit_top_call_counts(self, monkeypatch):
        """subreddit_top_posts: one request → api_calls == 1."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        posts_body = _reddit_listing([_make_post(1), _make_post(2)])
        responses = [_fake_resp(200, posts_body)]
        _setup_monkeypatch(monkeypatch, responses)

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)
        posts = fetcher.subreddit_top_posts("gadgets", limit=10)

        assert counter.api_calls == 1, (
            f"Expected 1 api_call for one successful request, got {counter.api_calls}"
        )
        assert len(posts) == 2, f"Expected 2 posts, got {len(posts)}"

    def test_two_subreddit_calls_count_separately(self, monkeypatch):
        """Two separate subreddit requests → api_calls == 2."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        body1 = _reddit_listing([_make_post(1)])
        body2 = _reddit_listing([_make_post(2)])
        _setup_monkeypatch(monkeypatch, [_fake_resp(200, body1), _fake_resp(200, body2)])

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)
        posts_top = fetcher.subreddit_top_posts("gadgets", limit=10)
        posts_hot = fetcher.subreddit_hot_posts("gadgets", limit=10)

        assert counter.api_calls == 2, (
            f"Expected 2 api_calls for two successful requests, got {counter.api_calls}"
        )
        assert len(posts_top) == 1
        assert len(posts_hot) == 1

    def test_no_counter_no_error(self, monkeypatch):
        """Without counter= (default None), _get() still works normally."""
        import app.crawlers.reddit as reddit_mod

        posts_body = _reddit_listing([_make_post(1)])
        _setup_monkeypatch(monkeypatch, [_fake_resp(200, posts_body)])

        fetcher = reddit_mod.RedditFetcher()  # no counter
        posts = fetcher.subreddit_top_posts("gadgets", limit=10)
        assert len(posts) == 1

    def test_failed_request_not_counted(self, monkeypatch):
        """HTTP errors (raise_for_status raises) → api_calls stays 0."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        error_resp = _fake_resp(429, {})
        _setup_monkeypatch(monkeypatch, [error_resp])

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)

        with pytest.raises(Exception):
            fetcher.subreddit_top_posts("gadgets", limit=10)

        assert counter.api_calls == 0, (
            f"Failed request must not be counted, got {counter.api_calls}"
        )

    def test_arctic_shift_calls_counted(self, monkeypatch):
        """Arctic Shift fallback requests also increment api_calls."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        arctic_body = {"data": [_make_post(1), _make_post(2)]}
        _setup_monkeypatch(monkeypatch, [_fake_resp(200, arctic_body)])

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)
        items = fetcher.arctic_user_posts("someuser", limit=10)

        assert counter.api_calls == 1, (
            f"Arctic Shift successful request should count, got {counter.api_calls}"
        )
        assert len(items) == 2

    def test_arctic_shift_exception_not_counted(self, monkeypatch):
        """Arctic Shift exception (network error) → api_calls stays 0, returns []."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        import app.crawlers.reddit as reddit_mod2

        call_log: list = []

        class _BadSession:
            headers = type("H", (), {"update": lambda s, h: None})()
            proxies: dict = {}

            def get(self, url: str, params=None, timeout=30):
                call_log.append(url)
                raise ConnectionError("network down")

        monkeypatch.setattr(reddit_mod2._requests, "Session", lambda: _BadSession())
        monkeypatch.setattr(reddit_mod2.time, "sleep", lambda _: None)

        counter = CrawlCounter()
        fetcher = reddit_mod2.RedditFetcher(counter=counter)
        items = fetcher.arctic_user_posts("someuser", limit=10)

        # arctic_user_posts catches exceptions and returns []
        assert items == [], f"Expected [], got {items}"
        assert counter.api_calls == 0, (
            f"Exception must not be counted, got {counter.api_calls}"
        )

    def test_pagination_accumulates_counts(self, monkeypatch):
        """search_user_posts with pagination: each page request counts separately."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        # Page 1: has results + 'after' token
        page1 = {
            "data": {
                "children": [{"data": _make_post(1)}, {"data": _make_post(2)}],
                "after": "t3_page2token",
            }
        }
        # Page 2: has results + no further 'after'
        page2 = {
            "data": {
                "children": [{"data": _make_post(3)}],
                "after": None,
            }
        }
        _setup_monkeypatch(
            monkeypatch,
            [_fake_resp(200, page1), _fake_resp(200, page2)],
        )

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)
        results = fetcher.search_user_posts("testuser", limit=50)

        assert counter.api_calls == 2, (
            f"Expected 2 api_calls for 2-page pagination, got {counter.api_calls}"
        )
        assert len(results) == 3, f"Expected 3 posts from 2 pages, got {len(results)}"

    def test_sleep_not_broken(self, monkeypatch):
        """_SLEEP is still called (we just mock it to avoid delays)."""
        import app.crawlers.reddit as reddit_mod
        from app.fetching import CrawlCounter

        sleep_calls: list[float] = []

        import app.crawlers.reddit as reddit_mod2
        monkeypatch.setattr(
            reddit_mod2.time, "sleep",
            lambda s: sleep_calls.append(s),
        )
        posts_body = _reddit_listing([_make_post(1)])

        class _FakeSession:
            headers = type("H", (), {"update": lambda s, h: None})()
            proxies: dict = {}

            def get(self, url: str, params=None, timeout=30):
                return _fake_resp(200, posts_body)

        monkeypatch.setattr(reddit_mod._requests, "Session", lambda: _FakeSession())

        counter = CrawlCounter()
        fetcher = reddit_mod.RedditFetcher(counter=counter)
        fetcher.subreddit_top_posts("gadgets", limit=10)

        # _SLEEP (1.2) must have been called
        assert len(sleep_calls) >= 1, "time.sleep must still be called for rate limiting"
        assert all(s == reddit_mod._SLEEP for s in sleep_calls), (
            f"sleep value must equal _SLEEP={reddit_mod._SLEEP}, got {sleep_calls}"
        )
