"""口碑评论采集编排 —— 模块二。

读 review_channels.yaml，按 platform 选采集器，采集后 upsert 到 reviews 表。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from .db import session_scope
from .models import Review
from .pipeline import clean_text, parse_dt

_CHANNELS_FILE = Path(__file__).resolve().parent.parent / "review_channels.yaml"


def load_channels() -> tuple[list[dict], dict]:
    if not _CHANNELS_FILE.exists():
        return [], {}
    cfg = yaml.safe_load(_CHANNELS_FILE.read_text(encoding="utf-8")) or {}
    return cfg.get("channels", []), cfg.get("settings", {})


def _get_crawler(channel: dict, max_pages: int):
    platform = channel.get("platform")
    if platform == "trustpilot":
        from .crawlers.trustpilot import TrustpilotCrawler
        return TrustpilotCrawler(channel, max_pages=max_pages)
    # 其余平台采集器待补（google_map / trustedshop / reviews_io ...）
    raise ValueError(f"评论平台采集器未实现: {platform}")


def run_review_channel(site_name: str) -> dict:
    """采集单个评论渠道。"""
    channels, settings = load_channels()
    channel = next((c for c in channels if c["site"] == site_name), None)
    if channel is None:
        raise ValueError(f"评论渠道不存在: {site_name}")
    crawler = _get_crawler(channel, settings.get("max_pages", 10))
    reviews = crawler.crawl()
    stats = _upsert_reviews(reviews)
    return {"site": site_name, "platform": channel["platform"],
            "fetched": len(reviews), **stats, "notes": crawler.notes}


def run_review_platform(platform: str) -> list[dict]:
    """采集某平台全部渠道。"""
    channels, _ = load_channels()
    names = [c["site"] for c in channels if c.get("platform") == platform]
    out = []
    for n in names:
        try:
            out.append(run_review_channel(n))
        except Exception as exc:
            out.append({"site": n, "status": "failed", "error": str(exc)})
    return out


def _upsert_reviews(reviews: list[dict]) -> dict:
    """评论入库去重（platform + review_id 唯一）。"""
    stats = {"inserted": 0, "updated": 0}
    if not reviews:
        return stats
    with session_scope() as s:
        for r in reviews:
            rid, plat = r.get("review_id"), r.get("platform")
            if not rid:
                continue
            row = (s.query(Review)
                   .filter(Review.platform == plat, Review.review_id == rid)
                   .first())
            payload = dict(
                review_id=rid, platform=plat, site=r.get("site"),
                reviewer_name=clean_text(r.get("reviewer_name")),
                reviewer_country=r.get("reviewer_country"),
                rating=_int(r.get("rating")),
                title=clean_text(r.get("title")),
                content=clean_text(r.get("content")),
                language=r.get("language"),
                review_date=parse_dt(r.get("review_date")),
                purchase_date=parse_dt(r.get("purchase_date")),
                reply_content=clean_text(r.get("reply_content")),
                reply_date=parse_dt(r.get("reply_date")),
                sku=r.get("sku"), product_url=r.get("product_url"),
                order_id=r.get("order_id"), is_verified=r.get("is_verified"),
                review_topics=r.get("review_topics"),
            )
            if row is None:
                s.add(Review(collected_time=datetime.utcnow(), **payload))
                stats["inserted"] += 1
            else:
                for k, v in payload.items():
                    setattr(row, k, v)
                stats["updated"] += 1
    return stats


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
