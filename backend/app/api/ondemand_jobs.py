"""按需抓取历史(OnDemandJob)—— 记录写入 + 列表/详情/删除 的纯逻辑。

routes.py 只做薄路由声明,业务逻辑集中在此,避免 routes.py 膨胀。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import OnDemandJob, Product, Review
from ..ondemand.registry import classify_url, detect_platform


def _status_of(listing_count: int, review_count: int, notes: list) -> str:
    if listing_count == 0 and review_count == 0:
        return "failed"
    if notes:
        return "partial"
    return "success"


def record_job(session: Session, *, ws_id: int | None, username: str | None,
               url: str, result) -> OnDemandJob:
    """把一次 fetch 的 OnDemandResult 落成一条 OnDemandJob。"""
    skus = [l.get("sku") for l in result.listings if l.get("sku")]
    listing_count = len(result.listings)
    review_count = len(result.reviews)
    notes = list(result.notes or [])
    job = OnDemandJob(
        url=url,
        platform=detect_platform(url),
        kind=classify_url(url),
        listing_count=listing_count,
        review_count=review_count,
        status=_status_of(listing_count, review_count, notes),
        notes=notes,
        item_skus=skus,
        workspace_id=ws_id,
        created_by=username,
    )
    session.add(job)
    session.flush()
    return job


def _job_dict(job: OnDemandJob) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "platform": job.platform,
        "kind": job.kind,
        "listing_count": job.listing_count,
        "review_count": job.review_count,
        "status": job.status,
        "notes": job.notes or [],
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
