from __future__ import annotations

import hashlib
import os
from collections import deque
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..db import session_scope
from ..models import AmazonJobIndex, AmazonListingJob, AmazonReviewJob
from ..crawlers.amazon_crawler.shuler.util.oss_callback import (
    external_job_status,
    normalized_job_reason,
    resolve_job_callback_url,
)

router = APIRouter(tags=["amazon-voc"])

JOB_REVIEW = "AmazonReviewJob"
JOB_LISTING = "AmazonListingJob"
_LOCAL_CALLBACKS = deque(maxlen=20)

JOB_SUBMIT_EXAMPLES = {
    "AmazonListingJob": {
        "summary": "商品详情采集",
        "description": "采集 Amazon 商品详情。只有显式传 callback/callback_url 时才回调。",
        "value": {
            "tenant_id": "anker_001",
            "app_id": "voc",
            "req_ssn": "TL1781759443539",
            "type": "AmazonListingJob",
            "priority": "100",
            "biz_source": "",
            "payload": {
                "market": "us",
                "asin": "B0F9L1PPPJ",
                "include_ratings_by_feature": False,
                "need_login": False,
            },
            "sla": 1,
            "callback": "http://127.0.0.1:8077/api/v1/test/delivery/receive",
        },
    },
    "AmazonReviewJob": {
        "summary": "商品评论采集",
        "description": "采集 Amazon 商品评论，limit 通常传 999。",
        "value": {
            "tenant_id": "anker_001",
            "app_id": "voc",
            "req_ssn": "TR1781759443539",
            "type": "AmazonReviewJob",
            "priority": "P0",
            "biz_source": "CD",
            "payload": {
                "market": "us",
                "asin": "B0D62GMQ3F",
                "last_time": "1990-02-10",
                "limit": 999,
            },
            "sla": 1,
            "callback": "http://127.0.0.1:8077/api/v1/test/delivery/receive",
        },
    },
}

JOB_RESULT_EXAMPLES = {
    "Query by req_ssn": {
        "summary": "按请求流水号和类型查询任务结果",
        "value": {
            "tenant_id": "anker_001",
            "app_id": "voc",
            "req_ssn": "TL1781759443539",
            "type": "AmazonListingJob",
        },
    },
}

CALLBACK_RECEIVE_EXAMPLES = {
    "AmazonListingJob callback": {
        "summary": "商品详情回调 body",
        "value": {
            "reason": None,
            "req_ssn": "TL1781759443539",
            "result": {
                "code": 200,
                "data": "http://voc-prod-collector-v2.shulex.com/parse/unpack/OSS_US/listing-result.json",
                "snapshot": "http://voc-prod-collector-v2.shulex.com/parse/unpack/OSS_US/listing-snapshot.gz",
            },
            "rsp_code": "00000",
            "rsp_msg": "success",
            "status": "finished",
            "type": "AmazonListingJob",
        },
    },
    "AmazonReviewJob callback": {
        "summary": "商品评论回调 body",
        "value": {
            "reason": None,
            "req_ssn": "TR1781759443539",
            "result": {
                "code": 200,
                "data": "http://voc-prod-collector-v2.shulex.com/parse/unpack/OSS_US/review-result.json",
            },
            "rsp_code": "00000",
            "rsp_msg": "success",
            "status": "finished",
            "type": "AmazonReviewJob",
        },
    },
}


class ReviewPayload(BaseModel):
    market: str = "US"
    asin: str
    last_time: str | None = None
    limit: int = 999
    max_pages: int = 10
    star_filter: list[int] | None = None
    query_conditions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("star_filter")
    @classmethod
    def _star_filter_list(cls, value):
        if value is not None and not isinstance(value, list):
            raise ValueError("star_filter must be a list")
        return value


class SubmitRequest(BaseModel):
    tenant_id: str = "default"
    app_id: str = "voc"
    req_ssn: str
    type: str = JOB_REVIEW
    priority: str | int = 100
    biz_source: str = ""
    payload: dict[str, Any]
    sla: int | None = None
    callback: str = ""
    callback_url: str = ""


class JobResultRequest(BaseModel):
    tenant_id: str = "default"
    app_id: str = "voc"
    req_ssn: str
    type: str | None = None


class ListingSubmitRequest(BaseModel):
    market: str = "US"
    asin: str
    callback: str = ""


@router.post("/api/v1/test/delivery/receive")
async def receive_test_delivery_callback(
    request: Request,
    payload: Annotated[Any, Body(openapi_examples=CALLBACK_RECEIVE_EXAMPLES)] = None,
):
    item = {
        "received_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "headers": {str(k).lower(): str(v) for k, v in request.headers.items()},
        "payload": payload,
    }
    _LOCAL_CALLBACKS.append(item)
    body = payload if isinstance(payload, dict) else {}
    return {
        "success": True,
        "received_at": item["received_at"],
        "req_ssn": body.get("req_ssn"),
        "type": body.get("type"),
        "count": len(_LOCAL_CALLBACKS),
    }


@router.get("/api/v1/test/delivery/receive/latest")
def get_latest_test_delivery_callback():
    if not _LOCAL_CALLBACKS:
        return {"success": False, "payload": None, "headers": {}, "received_at": None}
    latest = _LOCAL_CALLBACKS[-1]
    return {"success": True, **latest}


def _priority(value: str | int | None) -> int:
    if value is None:
        return 100
    raw = str(value).strip().lower()
    labels = {"p0": 0, "p1": 100, "p2": 200, "explore": 900}
    if raw in labels:
        return labels[raw]
    try:
        return int(float(raw))
    except Exception:
        return 100


def _market(value: str | None) -> str:
    market = str(value or "US").strip().upper()
    return {"GB": "UK"}.get(market, market)


def _task_id(prefix: str, market: str, asin: str, req_ssn: str) -> str:
    raw = f"{prefix}:{market}:{asin}:{req_ssn}:{datetime.utcnow().isoformat()}"
    return f"{prefix}_{datetime.utcnow():%Y%m%d%H%M%S}_{market}_{asin}_{hashlib.md5(raw.encode()).hexdigest()[:10]}"


def _env_map(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
    for part in raw.replace(";", ",").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def _auth_enabled() -> bool:
    return bool(os.environ.get("AMAZON_VOC_TOKEN", "").strip() or _env_map("AMAZON_VOC_TENANT_TOKENS"))


def _expected_token(tenant_id: str | None = None) -> str:
    tenant_tokens = _env_map("AMAZON_VOC_TENANT_TOKENS")
    tenant = str(tenant_id or "").strip()
    if tenant:
        return (
            tenant_tokens.get(tenant)
            or tenant_tokens.get(tenant.lower())
            or tenant_tokens.get(tenant.upper())
            or os.environ.get("AMAZON_VOC_TOKEN", "").strip()
        )
    return os.environ.get("AMAZON_VOC_TOKEN", "").strip()


def _require_token(tenant_id: str, x_token: str | None, authorization: str | None) -> None:
    if not _auth_enabled():
        return
    token = x_token or ""
    if not token and authorization:
        prefix = "bearer "
        if authorization.lower().startswith(prefix):
            token = authorization[len(prefix):].strip()
    expected = _expected_token(tenant_id)
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _enqueue_review_job(job) -> bool:
    try:
        from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import push_single_task

        return push_single_task(
            job.id,
            str(job.asin or "").upper(),
            str(job.market or "").upper(),
            need_crawler_time=job.next_attempt_at or datetime.utcnow(),
            priority=job.priority,
        )
    except Exception as exc:
        log.warning("amazon-voc review enqueue failed job_id=%s asin=%s error=%s", getattr(job, "id", None), getattr(job, "asin", ""), exc)
        return False


def _enqueue_listing_job(job) -> bool:
    try:
        from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import push_asin_task

        return push_asin_task(job.id, str(job.asin or "").upper())
    except Exception as exc:
        log.warning("amazon-voc listing enqueue failed job_id=%s asin=%s error=%s", getattr(job, "id", None), getattr(job, "asin", ""), exc)
        return False


def _enqueue_job(job) -> bool:
    if job.job_type == JOB_REVIEW:
        return _enqueue_review_job(job)
    if job.job_type == JOB_LISTING:
        return _enqueue_listing_job(job)
    return False


def _job_model(job_type: str):
    if job_type == JOB_REVIEW:
        return AmazonReviewJob
    if job_type == JOB_LISTING:
        return AmazonListingJob
    raise HTTPException(status_code=400, detail=f"unsupported type: {job_type}")


def _job_model_for_lookup(job_type: str):
    if job_type == JOB_LISTING:
        return AmazonListingJob
    if job_type == JOB_REVIEW:
        return AmazonReviewJob
    return None


def _normalize_payload(job_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    out = dict(payload or {})
    market = _market(out.get("market") or out.get("country"))
    out["market"] = market
    out["country"] = market
    if job_type == JOB_REVIEW:
        if "star_filter" in out and out["star_filter"] is not None and not isinstance(out["star_filter"], list):
            raise HTTPException(status_code=422, detail="star_filter must be a list")
        out.setdefault("limit", 999)
        out.setdefault("max_pages", 10)
        try:
            limit = int(out.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        try:
            page_cap = max(1, int(out.get("max_pages") or 10))
        except (TypeError, ValueError):
            page_cap = 10
        if limit > 0:
            out["max_pages"] = min(max(1, (limit + 9) // 10), page_cap)
    asin = str(out.get("asin") or "").strip().upper()
    if not asin:
        raise HTTPException(status_code=422, detail="asin required")
    out["asin"] = asin
    return out, market, asin


def _find_job_by_req(s, tenant_id: str, app_id: str, req_ssn: str, job_type: str | None = None):
    if job_type:
        model = _job_model(job_type)
        idx = s.query(AmazonJobIndex).filter_by(
            tenant_id=tenant_id,
            app_id=app_id,
            req_ssn=req_ssn,
            job_type=job_type,
        ).first()
        if idx is not None:
            job = s.get(model, idx.job_pk)
            if job is not None:
                return job
        return s.query(model).filter_by(tenant_id=tenant_id, app_id=app_id, req_ssn=req_ssn).first()

    idx = s.query(AmazonJobIndex).filter_by(
        tenant_id=tenant_id,
        app_id=app_id,
        req_ssn=req_ssn,
    ).first()
    if idx is not None:
        model = _job_model_for_lookup(idx.job_type) or AmazonReviewJob
        job = s.get(model, idx.job_pk)
        if job is not None:
            return job

    job = s.query(AmazonReviewJob).filter_by(tenant_id=tenant_id, app_id=app_id, req_ssn=req_ssn).first()
    if job is not None:
        return job
    return s.query(AmazonListingJob).filter_by(tenant_id=tenant_id, app_id=app_id, req_ssn=req_ssn).first()


def _job_result_response(job) -> dict[str, Any]:
    result_url = job.result_url or ""
    snapshot_url = job.snapshot_url or ""
    status = external_job_status(job.status)
    reason = normalized_job_reason(job)
    result = {
        "data": result_url,
        "code": 200 if status == "finished" else 500 if status == "failed" else 102,
    }
    if snapshot_url:
        result["snapshot"] = snapshot_url
    return {
        "rsp_code": "00000" if status != "failed" else "E5000",
        "rsp_msg": "success" if status != "failed" else "failed",
        "req_ssn": job.req_ssn,
        "status": status,
        "type": job.job_type,
        "task_id": job.task_id,
        "result": result,
        "result_count": job.result_count or 0,
        "result_data": job.result_data,
        "result_url": result_url,
        "snapshot_url": snapshot_url,
        "reason": reason,
        "error_msg": job.error_msg,
    }


def _upsert_index(s, job) -> None:
    row = s.query(AmazonJobIndex).filter_by(
        tenant_id=job.tenant_id,
        app_id=job.app_id,
        req_ssn=job.req_ssn,
        job_type=job.job_type,
    ).first()
    if row is None:
        row = s.query(AmazonJobIndex).filter_by(task_id=job.task_id).first()
    if row is None:
        row = AmazonJobIndex(task_id=job.task_id)
        s.add(row)
    row.tenant_id = job.tenant_id
    row.app_id = job.app_id
    row.req_ssn = job.req_ssn
    row.job_type = job.job_type
    row.job_pk = job.id
    row.table_name = job.__tablename__
    row.updated_at = datetime.utcnow()


@router.post("/job/submit")
def submit_job(
    data: Annotated[SubmitRequest, Body(openapi_examples=JOB_SUBMIT_EXAMPLES)],
    x_token: str | None = Header(default=None, alias="X-Token"),
    authorization: str | None = Header(default=None),
):
    _require_token(data.tenant_id, x_token, authorization)
    model = _job_model(data.type)
    payload, market, asin = _normalize_payload(data.type, data.payload)
    now = datetime.utcnow()
    callback_url = resolve_job_callback_url(data.tenant_id, data.callback or data.callback_url)
    prefix = "TR" if data.type == JOB_REVIEW else "TL"

    with session_scope() as s:
        job = s.query(model).filter_by(
            tenant_id=data.tenant_id,
            app_id=data.app_id,
            req_ssn=data.req_ssn,
            job_type=data.type,
        ).first()
        if job is None:
            job = model(
                task_id=_task_id(prefix, market, asin, data.req_ssn),
                tenant_id=data.tenant_id,
                app_id=data.app_id,
                req_ssn=data.req_ssn,
                job_type=data.type,
                market=market,
                asin=asin,
                priority=_priority(data.priority),
                biz_source=data.biz_source,
                sla=data.sla,
                payload=payload,
                raw_request=data.model_dump(),
                callback_url=callback_url,
                callback_status="pending" if callback_url else "none",
                status="queued",
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            s.add(job)
            s.flush()
            _upsert_index(s, job)
        queued = _enqueue_job(job)
        return {
            "rsp_code": "00000",
            "rsp_msg": "提交成功",
            "req_ssn": data.req_ssn,
            "data": {"task_id": job.task_id, "id": job.id, "queued": queued},
        }


@router.post("/job/result")
def get_job_result(
    data: Annotated[JobResultRequest, Body(openapi_examples=JOB_RESULT_EXAMPLES)],
    x_token: str | None = Header(default=None, alias="X-Token"),
    authorization: str | None = Header(default=None),
):
    _require_token(data.tenant_id, x_token, authorization)
    with session_scope() as s:
        job = _find_job_by_req(s, data.tenant_id, data.app_id, data.req_ssn, data.type)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_result_response(job)


@router.post("/job/amazon/monitor/submit")
def submit_listing(data: ListingSubmitRequest):
    market = _market(data.market)
    asin = str(data.asin or "").strip().upper()
    if not asin:
        raise HTTPException(status_code=422, detail="asin required")
    req_ssn = _task_id("TL", market, asin, "")
    now = datetime.utcnow()
    with session_scope() as s:
        job = AmazonListingJob(
            task_id=req_ssn,
            tenant_id="default",
            app_id="voc",
            req_ssn=req_ssn,
            job_type=JOB_LISTING,
            market=market,
            asin=asin,
            priority=0,
            payload={"market": market, "asin": asin},
            callback_url=data.callback,
            status="queued",
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        s.add(job)
        s.flush()
        _upsert_index(s, job)
        queued = _enqueue_job(job)
        return {
            "message": "提交成功",
            "req_ssn": req_ssn,
            "data": {"task_id": job.task_id, "id": job.id, "queued": queued},
        }


@router.get("/job/{task_id}")
def get_job(task_id: str):
    with session_scope() as s:
        job = s.query(AmazonReviewJob).filter_by(task_id=task_id).first()
        if job is None:
            job = s.query(AmazonListingJob).filter_by(task_id=task_id).first()
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "task_id": job.task_id,
            "req_ssn": job.req_ssn,
            "type": job.job_type,
            "status": job.status,
            "result_count": job.result_count,
            "result_data": job.result_data,
            "error_msg": job.error_msg,
        }
