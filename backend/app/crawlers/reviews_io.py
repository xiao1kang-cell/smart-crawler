"""Reviews.io 评论采集器 —— 模块二（规格 F2-004）。

Reviews.io 提供公开商家评论 API，直连可用、无需代理：
  GET https://api.reviews.io/merchant/reviews?store={store}&per_page=N&page=N
返回 {stats, reviews:[...], total_pages}。
"""
from __future__ import annotations

from curl_cffi import requests as creq

API = "https://api.reviews.io/merchant/reviews"


class ReviewsIoCrawler:
    platform = "reviews_io"

    def __init__(self, channel: dict, max_pages: int = 20):
        self.channel = channel
        self.store = channel["store"]              # 如 aosom-uk
        self.site = channel["site"]
        self.max_pages = channel.get("max_pages", max_pages)
        self.notes: list[str] = []

    def crawl(self) -> list[dict]:
        sess = creq.Session(impersonate="chrome")
        out: list[dict] = []
        for page in range(1, self.max_pages + 1):
            try:
                resp = sess.get(API, params={"store": self.store,
                                "per_page": 100, "page": page}, timeout=30)
                if resp.status_code != 200:
                    self.notes.append(f"page{page} HTTP {resp.status_code}")
                    break
                data = resp.json()
            except Exception as exc:
                self.notes.append(f"page{page} 异常: {exc}")
                break
            reviews = data.get("reviews") or []
            if not reviews:
                break
            for r in reviews:
                row = self._map(r)
                if row:
                    out.append(row)
            if page >= (data.get("total_pages") or 1):
                break
        stats = (data.get("stats") or {}) if "data" in dir() else {}
        self.notes.append(f"采集 {len(out)} 条评论"
                          + (f"（平台共 {stats.get('total_reviews')} 条）"
                             if stats.get("total_reviews") else ""))
        return out

    def _map(self, r: dict) -> dict | None:
        rid = r.get("store_review_id") or r.get("id")
        if not rid:
            return None
        reviewer = r.get("reviewer")
        if isinstance(reviewer, dict):
            name = (f"{reviewer.get('first_name','')} "
                    f"{reviewer.get('last_name','')}").strip() \
                or reviewer.get("name")
        else:
            name = reviewer
        replies = r.get("replies") or []
        reply = replies[0] if replies else {}
        return {
            "review_id": str(rid), "platform": "reviews_io",
            "site": self.site,
            "reviewer_name": name,
            "rating": r.get("rating"),
            "title": r.get("title"),
            "content": r.get("comments") or r.get("review"),
            "review_date": r.get("date_created"),
            "order_id": r.get("order_number"),
            "reply_content": reply.get("comments") if isinstance(reply, dict)
            else None,
            "review_topics": r.get("tags"),
        }
