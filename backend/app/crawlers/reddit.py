"""Reddit 数据采集器 —— 两条可靠路径：

路径 A  Reddit 公开 JSON（subreddit top + 全站搜索 author: filter）
路径 B  Arctic Shift API（https://arctic-shift.photon-reddit.com）——
        历史存档，已删帖也有，Reddit 限流时的兜底。

不需要 OAuth/API Key。限流：1.2 req/s（Reddit），无限流（Arctic Shift）。
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests as _requests

if TYPE_CHECKING:
    from ..fetching import CrawlCounter

_REDDIT_BASE = "https://www.reddit.com"
_ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; smart-crawler/1.0; "
        "+https://smartcrawler.io; contact:mcp@smartcrawler.io)"
    ),
    "Accept": "application/json",
}
_SLEEP = 1.2


class RedditFetcher:
    def __init__(self, proxy: str | None = None, *,
                 counter: "CrawlCounter | None" = None):
        self.sess = _requests.Session()
        self.sess.headers.update(_HEADERS)
        if proxy:
            self.sess.proxies = {"http": proxy, "https": proxy}
        self._counter = counter

    def _get(self, url: str, params: dict | None = None,
             base: str = _REDDIT_BASE) -> dict | list:
        extra = {"raw_json": "1"} if base == _REDDIT_BASE else {}
        r = self.sess.get(
            (base + url) if not url.startswith("http") else url,
            params={**(params or {}), **extra},
            timeout=30,
        )
        r.raise_for_status()
        # count_api_fetch equivalent: each successful (status 200) call → api_calls += 1
        if self._counter is not None and getattr(r, "status_code", None) == 200:
            self._counter.api_calls += 1
        time.sleep(_SLEEP)
        return r.json()

    # ── subreddit ──────────────────────────────────────────────────────────

    def subreddit_top_posts(self, subreddit: str, limit: int = 100,
                             t: str = "all") -> list[dict]:
        """取 subreddit top 帖子（最多 100 条/次，t: hour/day/week/month/year/all）。"""
        data = self._get(f"/r/{subreddit}/top.json", {"limit": limit, "t": t})
        return [c["data"] for c in data.get("data", {}).get("children", [])]

    def subreddit_hot_posts(self, subreddit: str, limit: int = 100) -> list[dict]:
        data = self._get(f"/r/{subreddit}/hot.json", {"limit": limit})
        return [c["data"] for c in data.get("data", {}).get("children", [])]

    # ── user (路径 A: Reddit 全站搜索) ─────────────────────────────────────

    def search_user_posts(self, username: str, subreddit: str | None = None,
                           limit: int = 100, sort: str = "top") -> list[dict]:
        """用 Reddit 全站搜索找某用户的帖子。
        /user/{username}/submitted.json 已被 Reddit 限制，搜索路径更可靠。"""
        results, after = [], None
        while len(results) < limit:
            params: dict = {
                "q": f"author:{username}",
                "sort": sort, "t": "all", "limit": 100,
            }
            if after:
                params["after"] = after
            data = self._get("/search.json", params)
            children = data.get("data", {}).get("children", [])
            if not children:
                break
            for c in children:
                d = c.get("data", {})
                if subreddit and d.get("subreddit", "").lower() != subreddit.lower():
                    continue
                results.append(d)
                if len(results) >= limit:
                    break
            after = data.get("data", {}).get("after")
            if not after:
                break
        return results

    def user_about(self, username: str) -> dict:
        """取用户基本信息；账号已删除时返回空 dict。"""
        try:
            data = self._get(f"/user/{username}/about.json")
            return data.get("data", {})
        except Exception:
            return {}

    # ── user (路径 B: Arctic Shift 存档) ───────────────────────────────────

    def arctic_user_posts(self, username: str, subreddit: str | None = None,
                           limit: int = 100) -> list[dict]:
        """从 Arctic Shift 历史存档取用户帖子（包含已删帖，不受 Reddit 限流）。"""
        params: dict = {"author": username, "limit": limit}
        if subreddit:
            params["subreddit"] = subreddit
        try:
            data = self._get("/posts/search", params, base=_ARCTIC_BASE)
        except Exception:
            return []
        items = data.get("data", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    def arctic_user_comments(self, username: str, subreddit: str | None = None,
                              limit: int = 100) -> list[dict]:
        params: dict = {"author": username, "limit": limit}
        if subreddit:
            params["subreddit"] = subreddit
        try:
            data = self._get("/comments/search", params, base=_ARCTIC_BASE)
        except Exception:
            return []
        items = data.get("data", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []


# ── high-level helpers ─────────────────────────────────────────────────────

def get_top_contributors(subreddit: str, top_n: int = 3,
                          lookback_posts: int = 200,
                          proxy: str | None = None) -> list[dict]:
    """从 subreddit top posts（含 all-time + year）找 top N 贡献者。

    排名公式：posts_count×10 + total_score×0.01
    过滤系统账号（AutoModerator/[deleted]）。
    """
    fetcher = RedditFetcher(proxy=proxy)
    scores: dict[str, dict] = defaultdict(
        lambda: {"posts": 0, "score": 0})
    _SKIP = {"[deleted]", "[removed]", "automoderator"}

    for t_filter in ("all", "year"):
        for p in fetcher.subreddit_top_posts(subreddit, limit=100, t=t_filter):
            author = p.get("author", "")
            if not author or author.lower() in _SKIP:
                continue
            scores[author]["posts"] += 1
            scores[author]["score"] += p.get("score", 0)

    ranked = sorted(
        scores.items(),
        key=lambda kv: kv[1]["posts"] * 10 + kv[1]["score"] * 0.01,
        reverse=True,
    )

    result = []
    for username, stats in ranked[:top_n]:
        about = fetcher.user_about(username)
        created = about.get("created_utc", time.time())
        age_days = max(0, int((time.time() - created) / 86400))
        result.append({
            "username": username,
            "subreddit": subreddit,
            "posts_in_sub": stats["posts"],
            "post_total_score": stats["score"],
            "reddit_age_days": age_days,
            "link_karma": about.get("link_karma", 0),
            "comment_karma": about.get("comment_karma", 0),
            "total_karma": about.get("total_karma", 0),
            "profile_url": f"https://www.reddit.com/u/{username}",
        })
    return result


def get_user_activity(username: str, subreddit: str | None = None,
                       post_limit: int = 100, comment_limit: int = 100,
                       proxy: str | None = None) -> dict:
    """取用户完整活动。

    帖子优先 Arctic Shift（有历史），回退 Reddit 全站搜索。
    评论用 Arctic Shift（Reddit 评论端点已全部限制）。
    """
    fetcher = RedditFetcher(proxy=proxy)
    about = fetcher.user_about(username)

    # Posts: Arctic Shift first
    posts_raw = fetcher.arctic_user_posts(username, subreddit=subreddit,
                                          limit=post_limit)
    if not posts_raw:
        posts_raw = fetcher.search_user_posts(username, subreddit=subreddit,
                                              limit=post_limit)

    # Comments: Arctic Shift
    comments_raw = fetcher.arctic_user_comments(username, subreddit=subreddit,
                                                limit=comment_limit)

    def fmt_post(p: dict) -> dict:
        # Arctic Shift and Reddit JSON use same field names
        ts = p.get("created_utc", 0)
        return {
            "title": p.get("title", ""),
            "body": (p.get("selftext") or "")[:300],
            "score": p.get("score", 0),
            "upvote_ratio": p.get("upvote_ratio", 0),
            "num_comments": p.get("num_comments", 0),
            "subreddit": p.get("subreddit", ""),
            "flair": p.get("link_flair_text") or "",
            "created_utc": ts,
            "url": ("https://www.reddit.com" + p["permalink"]
                    if p.get("permalink") else ""),
        }

    def fmt_comment(c: dict) -> dict:
        return {
            "body": (c.get("body") or "")[:300],
            "score": c.get("score", 0),
            "subreddit": c.get("subreddit", ""),
            "parent_post_title": c.get("link_title") or "",
            "created_utc": c.get("created_utc", 0),
            "url": ("https://www.reddit.com" + c["permalink"]
                    if c.get("permalink") else ""),
        }

    posts = [fmt_post(p) for p in posts_raw]
    comments = [fmt_comment(c) for c in comments_raw]
    top_posts = sorted(posts, key=lambda x: x["score"], reverse=True)[:5]

    timeline: dict[str, int] = defaultdict(int)
    for p in posts:
        ts = p.get("created_utc", 0)
        if ts:
            ym = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
            timeline[ym] += 1

    created = about.get("created_utc", time.time())
    age_days = max(0, int((time.time() - created) / 86400))

    return {
        "username": username,
        "subreddit_filter": subreddit,
        "profile": {
            "reddit_age_days": age_days,
            "link_karma": about.get("link_karma", 0),
            "comment_karma": about.get("comment_karma", 0),
            "total_karma": about.get("total_karma", 0),
            "is_verified": about.get("verified", False),
        },
        "posts": posts,
        "comments": comments,
        "top_posts": top_posts,
        "monthly_post_count": dict(sorted(timeline.items())),
        "stats": {
            "total_posts": len(posts),
            "total_comments": len(comments),
            "avg_post_score": round(
                sum(p["score"] for p in posts) / max(1, len(posts)), 1),
            "avg_comment_score": round(
                sum(c["score"] for c in comments) / max(1, len(comments)), 1),
            "top_post_score": max((p["score"] for p in posts), default=0),
        },
    }
