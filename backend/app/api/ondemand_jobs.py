"""按需抓取历史(OnDemandJob)—— 记录写入 + 列表/详情/删除/批量/重试 的纯逻辑。

routes.py 只做薄路由声明,业务逻辑集中在此,避免 routes.py 膨胀。
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models import OnDemandJob, Product, Review
from ..ondemand.queue import PENDING, TERMINAL, enqueue
from ..ondemand.registry import classify_url, detect_platform

MAX_BATCH = 1000  # 单批 URL 上限


class PendingExistsError(Exception):
    """本 workspace 有未完成(queued/running)任务,禁止再次提交批量。"""


class NotRetryableError(Exception):
    """job 非终态(queued/running),不可重试。"""


def _status_of(listing_count: int, review_count: int, notes: list) -> str:
    if listing_count == 0 and review_count == 0:
        return "failed"
    if notes:
        return "partial"
    return "success"


def record_job(session: Session, *, ws_id: int | None, username: str | None,
               url: str, result, batch_id: str | None = None,
               max_items: int = 100, review_limit: int = 100) -> OnDemandJob:
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
        batch_id=batch_id or uuid.uuid4().hex,
        max_items=max_items,
        review_limit=review_limit,
        attempts=1,
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
        "batch_id": job.batch_id,
        "attempts": job.attempts or 0,
        "error": job.error,
        "max_items": job.max_items,
        "review_limit": job.review_limit,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def has_pending(session: Session, *, ws_id: int | None) -> bool:
    """本 workspace 是否存在未完成(queued/running)任务。"""
    return (session.query(OnDemandJob)
            .filter(OnDemandJob.workspace_id == ws_id,
                    OnDemandJob.status.in_(PENDING)).first() is not None)


def submit_batch(session: Session, *, ws_id: int | None, username: str | None,
                 urls: list[str], max_items: int, review_limit: int) -> dict:
    """提交一批 URL:建 queued job + 入队,立即返回。不阻塞抓取。

    去空行/去重 → 校验数量(≤MAX_BATCH)与并发闸(无未完成任务)→
    逐条识别平台(识别不了进 skipped)→ 同批共享 batch_id。
    """
    # 去空行 + 去重(保序)
    seen: set[str] = set()
    cleaned: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        cleaned.append(u)

    if len(cleaned) > MAX_BATCH:
        raise ValueError(f"单批最多 {MAX_BATCH} 条,当前 {len(cleaned)} 条")
    if cleaned and has_pending(session, ws_id=ws_id):
        raise PendingExistsError("有未完成任务,请等待完成后再提交")

    batch_id = uuid.uuid4().hex
    skipped: list[dict] = []
    new_ids: list[int] = []
    for u in cleaned:
        if detect_platform(u) is None:
            skipped.append({"url": u, "reason": "无法识别平台"})
            continue
        job = OnDemandJob(
            url=u, platform=detect_platform(u), kind=classify_url(u),
            status="queued", notes=[], item_skus=[], batch_id=batch_id,
            max_items=max_items, review_limit=review_limit, attempts=0,
            workspace_id=ws_id, created_by=username,
        )
        session.add(job)
        session.flush()
        new_ids.append(job.id)

    for jid in new_ids:
        enqueue(jid)
    return {"batch_id": batch_id, "queued": len(new_ids), "skipped": skipped}


def retry_job(session: Session, *, ws_id: int | None, job_id: int) -> dict | None:
    """重试单条:仅终态可重试,置 queued + 入队。

    返回 None 表示不存在或越权(端点据此返回 404/403);
    非终态抛 NotRetryableError(端点 409)。
    """
    job = session.get(OnDemandJob, job_id)
    if job is None or job.workspace_id != ws_id:
        return None
    if job.status not in TERMINAL:
        raise NotRetryableError(f"任务状态 {job.status},不可重试")
    job.status = "queued"
    job.error = None
    session.flush()
    enqueue(job.id)
    return {"id": job.id, "status": "queued"}


def retry_failed_batch(session: Session, *, ws_id: int | None,
                       batch_id: str) -> dict:
    """一键重试整批失败:该 batch 下所有 failed 的 job 置 queued + 入队。"""
    rows = (session.query(OnDemandJob)
            .filter(OnDemandJob.workspace_id == ws_id,
                    OnDemandJob.batch_id == batch_id,
                    OnDemandJob.status == "failed").all())
    ids = []
    for r in rows:
        r.status = "queued"
        r.error = None
        ids.append(r.id)
    session.flush()
    for jid in ids:
        enqueue(jid)
    return {"batch_id": batch_id, "requeued": len(ids)}


def list_jobs_logic(session: Session, *, ws_id: int | None,
                    platform: str | None, page: int, page_size: int,
                    batch_id: str | None = None,
                    status: str | None = None) -> dict:
    q = session.query(OnDemandJob).filter(OnDemandJob.workspace_id == ws_id)
    if platform:
        q = q.filter(OnDemandJob.platform == platform)
    if batch_id:
        q = q.filter(OnDemandJob.batch_id == batch_id)
    if status:
        q = q.filter(OnDemandJob.status == status)
    total = q.count()
    rows = (q.order_by(OnDemandJob.created_at.desc(), OnDemandJob.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "jobs": [_job_dict(r) for r in rows]}


def job_detail_logic(session: Session, *, ws_id: int | None,
                     job_id: int) -> dict | None:
    """返回 job + listings + reviews;job 不存在或不属于 ws_id 时返回 None。"""
    job = session.get(OnDemandJob, job_id)
    if job is None or job.workspace_id != ws_id:
        return None
    skus = list(job.item_skus or [])
    listings, reviews = [], []
    if skus:
        prods = (session.query(Product)
                 .filter(Product.site.like("ondemand_%"),
                         Product.sku.in_(skus)).all())
        listings = [{"sku": p.sku, "title": p.title, "sale_price": p.sale_price,
                     "original_price": p.original_price, "currency": p.currency,
                     "image_urls": p.image_urls or [], "product_url": p.product_url}
                    for p in prods]
        revs = (session.query(Review)
                .filter(Review.platform.like("ondemand_%"),
                        Review.sku.in_(skus)).all())
        reviews = [{"review_id": r.review_id, "rating": r.rating,
                    "content": r.content, "review_date":
                    r.review_date.isoformat() if r.review_date else None}
                   for r in revs]
    return {"job": _job_dict(job), "listings": listings, "reviews": reviews}


def delete_job_logic(session: Session, *, ws_id: int | None,
                     job_id: int) -> bool:
    """删单条;不存在或不属于 ws_id 返回 False。只删记录,不删 Product/Review。"""
    job = session.get(OnDemandJob, job_id)
    if job is None or job.workspace_id != ws_id:
        return False
    session.delete(job)
    return True


def clear_jobs_logic(session: Session, *, ws_id: int | None) -> int:
    """清空本 workspace 的记录,返回删除条数。只删记录,不删 Product/Review。"""
    rows = session.query(OnDemandJob).filter(
        OnDemandJob.workspace_id == ws_id).all()
    n = len(rows)
    for r in rows:
        session.delete(r)
    return n
