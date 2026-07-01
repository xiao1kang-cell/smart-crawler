import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from email.utils import formatdate
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import requests
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.anker_review_serializer import normalize_review_result
from app.crawlers.amazon_crawler.shuler.util.config import (
    CALLBACK_SECRET,
    CALLBACK_TIMEOUT_SECONDS,
    OSS_ACCESS_KEY_ID,
    OSS_ACCESS_KEY_SECRET,
    OSS_BUCKET,
    OSS_ENDPOINT,
    OSS_RESULT_PREFIX,
    OSS_SIGNED_URL_EXPIRES_SECONDS,
)

STATUS_DESC = {-1: "asin_not_found", 0: "pending", 1: "running", 2: "success", 3: "failed"}


def _env_map(name: str) -> Dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            return {}

    out: Dict[str, str] = {}
    for part in raw.replace(";", ",").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def resolve_job_callback_url(tenant_id: str | None, explicit_url: str | None = None) -> str:
    return str(explicit_url or "").strip()


def external_job_status(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    mapping = {
        "queued": "pending",
        "pending": "pending",
        "running": "running",
        "completed": "finished",
        "success": "finished",
        "partial": "finished",
        "failed": "failed",
        "error": "failed",
    }
    return mapping.get(raw, raw or "unknown")


def normalized_job_reason(job: Any) -> str | None:
    status = external_job_status(getattr(job, "status", ""))
    success = status == "finished"
    raw_reason = getattr(job, "error_msg", None) or getattr(job, "fail_reason", None) or ""
    normalized_reason = _extract_fail_reason(str(raw_reason), 2 if success else 3)
    return normalized_reason or (None if success else str(raw_reason or "error"))


def build_job_callback_payload(job: Any) -> Dict[str, Any]:
    job_type = str(getattr(job, "job_type", "") or "")
    status = external_job_status(getattr(job, "status", ""))
    success = status == "finished"
    reason = normalized_job_reason(job)
    result_url = getattr(job, "result_url", None) or ""
    snapshot_url = getattr(job, "snapshot_url", None) or ""
    result: Dict[str, Any] = {"data": result_url, "code": 200 if success else 500}
    if job_type == "AmazonListingJob" and snapshot_url:
        result["snapshot"] = snapshot_url

    return {
        "reason": reason,
        "req_ssn": getattr(job, "req_ssn", "") or getattr(job, "task_id", ""),
        "result": result,
        "rsp_code": "00000" if success else "E5000",
        "rsp_msg": "success" if success else "failed",
        "status": status,
        "type": job_type,
    }


def send_job_callback(job: Any, *, callback_url: str | None = None, timeout_seconds: int | None = None) -> Dict[str, Any]:
    target_url = resolve_job_callback_url(getattr(job, "tenant_id", ""), callback_url or getattr(job, "callback_url", ""))
    if not target_url:
        return {"callback_status": "none", "callback_error": "callback url is empty"}

    payload = build_job_callback_payload(job)
    send_result = _send_callback(target_url, payload, timeout_seconds=timeout_seconds)
    if send_result["callback_status"] == 1:
        return {"callback_status": "success", "callback_error": "", "callback_updated_at": datetime.utcnow()}
    return {
        "callback_status": "failed",
        "callback_error": send_result.get("callback_error", ""),
        "callback_updated_at": datetime.utcnow(),
    }


def _oss_enabled() -> bool:
    return bool(OSS_ENDPOINT and OSS_BUCKET and OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET)


def _endpoint_host() -> str:
    endpoint = str(OSS_ENDPOINT or "").strip().rstrip("/")
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    return parsed.netloc or parsed.path


def _bucket_base_url() -> str:
    return f"https://{OSS_BUCKET}.{_endpoint_host()}"


def _quote_object_key(object_key: str) -> str:
    return quote(object_key.strip("/"), safe="/")


def _sign_oss(method: str, object_key: str, content_type: str = "", date_or_expires: str = "") -> str:
    resource = f"/{OSS_BUCKET}/{object_key.strip('/')}"
    string_to_sign = f"{method}\n\n{content_type}\n{date_or_expires}\n{resource}"
    digest = hmac.new(
        OSS_ACCESS_KEY_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _safe_path_part(value: str, default: str = "unknown") -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    safe = re.sub(r"_+", "_", safe).strip("._-")
    return safe[:80] or default


def _tenant_path(tenant_id: str = "") -> str:
    if str(tenant_id or "").strip():
        return _safe_path_part(tenant_id, "default").lower()
    return "shulex/default"


def _build_object_key(
    task_id: str,
    asin: str,
    region: str,
    biz_source: str = "",
    tenant_id: str = "",
) -> str:
    prefix = str(OSS_RESULT_PREFIX or "crawler-data/reviews").strip().strip("/")
    day = datetime.now().strftime("%Y%m%d")
    safe_task_id = _safe_path_part(task_id, "task")
    return f"{prefix}/{_tenant_path(tenant_id)}/{day}/{safe_task_id}.json"


def _build_snapshot_object_key(
    task_id: str,
    asin: str,
    region: str,
    biz_source: str = "",
    tenant_id: str = "",
) -> str:
    prefix = str(OSS_RESULT_PREFIX or "crawler-data/reviews").strip().strip("/")
    day = datetime.now().strftime("%Y%m%d")
    safe_task_id = _safe_path_part(task_id, "task")
    return f"{prefix}/{_tenant_path(tenant_id)}/{day}/{safe_task_id}_snapshot.html"


def _signed_get_url(object_key: str) -> str:
    expires = int(time.time()) + max(int(OSS_SIGNED_URL_EXPIRES_SECONDS or 0), 60)
    signature = _sign_oss("GET", object_key, "", str(expires))
    return (
        f"{_bucket_base_url()}/{_quote_object_key(object_key)}"
        f"?OSSAccessKeyId={quote(OSS_ACCESS_KEY_ID)}"
        f"&Expires={expires}"
        f"&Signature={quote(signature)}"
    )


def signed_get_url_for_object_key(object_key: str) -> str:
    if not _oss_enabled():
        raise RuntimeError("OSS config is incomplete")
    if not str(object_key or "").strip():
        raise RuntimeError("OSS object key is empty")
    return _signed_get_url(object_key)


def upload_json_to_oss(object_key: str, payload: Any) -> str:
    if not _oss_enabled():
        raise RuntimeError("OSS config is incomplete")

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    content_type = "application/json; charset=utf-8"
    date_header = formatdate(usegmt=True)
    signature = _sign_oss("PUT", object_key, content_type, date_header)
    url = f"{_bucket_base_url()}/{_quote_object_key(object_key)}"
    headers = {
        "Date": date_header,
        "Content-Type": content_type,
        "Authorization": f"OSS {OSS_ACCESS_KEY_ID}:{signature}",
    }
    resp = requests.put(url, data=body, headers=headers, timeout=CALLBACK_TIMEOUT_SECONDS)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"OSS upload failed status={resp.status_code} body={resp.text[:200]}")
    return _signed_get_url(object_key)


def upload_text_to_oss(
        object_key: str,
        text: str,
        content_type: str = "text/html; charset=utf-8",
) -> str:
    if not _oss_enabled():
        raise RuntimeError("OSS config is incomplete")

    body = str(text or "").encode("utf-8")
    date_header = formatdate(usegmt=True)
    signature = _sign_oss("PUT", object_key, content_type, date_header)
    url = f"{_bucket_base_url()}/{_quote_object_key(object_key)}"
    headers = {
        "Date": date_header,
        "Content-Type": content_type,
        "Authorization": f"OSS {OSS_ACCESS_KEY_ID}:{signature}",
    }
    resp = requests.put(url, data=body, headers=headers, timeout=CALLBACK_TIMEOUT_SECONDS)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"OSS upload failed status={resp.status_code} body={resp.text[:200]}")
    return _signed_get_url(object_key)


def _send_callback(callback_url: str, payload: Dict[str, Any], timeout_seconds: int | None = None) -> Dict[str, Any]:
    headers = {}
    if CALLBACK_SECRET:
        headers["X-Callback-Secret"] = CALLBACK_SECRET
    try:
        resp = requests.post(
            callback_url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds or CALLBACK_TIMEOUT_SECONDS,
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            return {
                "callback_status": 2,
                "callback_error": f"callback failed status={resp.status_code} body={resp.text[:200]}",
            }
        return {"callback_status": 1, "callback_error": ""}
    except Exception as exc:
        return {"callback_status": 2, "callback_error": str(exc)[:1000]}


def _callback_payload(
    *,
    task_id: str,
    asin: str,
    region: str,
    status: int,
    result_count: int,
    result_url: str,
    error_msg: str = "",
) -> Dict[str, Any]:
    fail_reason = _extract_fail_reason(error_msg, status)
    api_status = -1 if fail_reason == "asin_not_found" else int(status)
    return {
        "task_id": task_id,
        "asin": asin,
        "region": region,
        "status": api_status,
        "status_desc": STATUS_DESC.get(api_status, "unknown"),
        "fail_reason": fail_reason,
        "task_type": "review",
        "result_count": int(result_count or 0),
        "result_url": result_url,
        "error_msg": error_msg or "",
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }


def _extract_fail_reason(error_msg: str, status: int) -> str:
    error_msg = str(error_msg or "")
    if error_msg.startswith("[ASIN_NOT_FOUND]"):
        return "asin_not_found"
    if error_msg.startswith("[NO_REVIEWS]"):
        return "no_reviews"
    if int(status or 0) == 3:
        return "error"
    return ""


def _has_result_data(result_data: Any, result_count: int) -> bool:
    try:
        if int(result_count or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass

    if result_data is None:
        return False
    if isinstance(result_data, str):
        value = result_data.strip()
        if not value or value.lower() == "null" or value in {"[]", "{}"}:
            return False
        try:
            return _has_result_data(json.loads(value), 0)
        except Exception:
            return True
    if isinstance(result_data, (list, tuple, set, dict)):
        return bool(result_data)
    return True


def dispatch_single_task_callback(
    *,
    callback_url: str,
    task_id: str,
    asin: str,
    region: str,
    result_data: Any,
    result_count: int,
    success: bool,
    error_msg: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upload single review result JSON to OSS and POST a completion callback.

    This is best-effort by design: callback failures must not change crawler task
    status after the data has already been written to MySQL.
    """
    callback_url = str(callback_url or "").strip()
    if not callback_url:
        return {"callback_status": 3, "callback_error": "callback url is empty"}

    try:
        extra = extra or {}
        biz_source = str(extra.get("biz_source") or "").strip()
        tenant_id = str(extra.get("tenant_id") or "").strip()
        object_key = ""
        result_url = ""
        callback_error = ""
        if _has_result_data(result_data, result_count):
            object_key = _build_object_key(
                task_id,
                asin,
                region,
                biz_source=biz_source,
                tenant_id=tenant_id,
            )
            task_status = 2 if success else 3
            fail_reason = _extract_fail_reason(error_msg, task_status)
            api_status = -1 if fail_reason == "asin_not_found" else task_status
            normalized_result = normalize_review_result(result_data)
            oss_payload = {
                "task_id": task_id,
                "asin": asin,
                "region": region,
                "tenant_id": tenant_id,
                "biz_source": biz_source,
                "task_type": "review",
                "status": api_status,
                "status_desc": STATUS_DESC.get(api_status, "unknown"),
                "fail_reason": fail_reason,
                "result_count": int(result_count or 0),
                "result": normalized_result,
                "error_msg": error_msg or "",
                "extra": extra,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            try:
                result_url = upload_json_to_oss(object_key, oss_payload)
            except Exception as exc:
                callback_error = str(exc)[:1000]
                logger.warning(f"[callback] 上传OSS失败但继续回调 task_id={task_id}: {callback_error}")
        payload = build_job_callback_payload(
            SimpleNamespace(
                task_id=task_id,
                req_ssn=extra.get("req_ssn") or task_id,
                job_type=extra.get("job_type") or "AmazonReviewJob",
                status="completed" if success else "failed",
                result_url=result_url,
                snapshot_url=extra.get("snapshot_url") or "",
                error_msg=error_msg,
                fail_reason=_extract_fail_reason(error_msg, 2 if success else 3),
            )
        )

        send_result = _send_callback(callback_url, payload)
        if callback_error and not send_result.get("callback_error"):
            send_result["callback_error"] = callback_error
        if send_result["callback_status"] != 1:
            logger.warning(
                f"[callback] 回调失败 task_id={task_id} error={send_result['callback_error']}"
            )
        elif object_key:
            logger.info(f"[callback] 回调成功 task_id={task_id} oss_key={object_key}")
        else:
            logger.info(f"[callback] 回调成功 task_id={task_id} empty_result_no_oss=1")
        return {
            "callback_status": send_result["callback_status"],
            "callback_error": send_result["callback_error"],
            "oss_object_key": object_key,
            "oss_result_url": result_url,
        }
    except Exception as exc:
        logger.warning(f"[callback] 上传OSS或回调异常 task_id={task_id}: {exc}")
        return {
            "callback_status": 2,
            "callback_error": str(exc)[:1000],
            "oss_object_key": "",
            "oss_result_url": "",
        }


def dispatch_existing_single_task_callback(row: Dict[str, Any]) -> Dict[str, Any]:
    """Retry callback for an already completed single task row from MySQL."""
    row = row or {}
    callback_url = str(row.get("callback_url") or "").strip()
    if not callback_url:
        return {"callback_status": 3, "callback_error": "callback url is empty"}

    params = row.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    extra = {
        "tenant_id": row.get("tenant_id", params.get("tenant_id", "")),
        "biz_source": params.get("biz_source", ""),
        "priority": row.get("priority", params.get("priority", "")),
        "need_crawler_time": str(row.get("need_crawler_time", "")),
        "source": params.get("source", ""),
    }
    result_count = int(row.get("result_count") or 0)
    result_data = row.get("result") if row.get("result") is not None else []
    row_status = int(row.get("status") or 2)
    success = row_status != 3
    has_result_data = _has_result_data(result_data, result_count)
    object_key = str(row.get("oss_object_key") or "").strip()
    result_url = str(row.get("oss_result_url") or row.get("result_url") or "").strip()
    snapshot_object_key = str(row.get("snapshot_object_key") or "").strip()
    snapshot_url = str(row.get("snapshot_url") or "").strip()
    snapshot_html = str(row.get("snapshot_html") or "")
    if object_key:
        try:
            result_url = signed_get_url_for_object_key(object_key)
        except Exception as exc:
            return {
                "callback_status": 2,
                "callback_error": str(exc)[:1000],
                "oss_object_key": object_key,
                "oss_result_url": result_url,
            }
    if str(row.get("job_type") or "") == "AmazonListingJob":
        if snapshot_object_key:
            try:
                snapshot_url = signed_get_url_for_object_key(snapshot_object_key)
            except Exception as exc:
                return {
                    "callback_status": 2,
                    "callback_error": str(exc)[:1000],
                    "oss_object_key": object_key,
                    "oss_result_url": result_url,
                    "snapshot_object_key": snapshot_object_key,
                    "snapshot_url": snapshot_url,
                    "snapshot_html": snapshot_html,
                }
        elif snapshot_html:
            snapshot_object_key = _build_snapshot_object_key(
                str(row.get("task_id") or ""),
                str(row.get("asin") or ""),
                str(row.get("region") or ""),
                biz_source=str(params.get("biz_source") or ""),
                tenant_id=str(row.get("tenant_id") or params.get("tenant_id") or ""),
            )
            try:
                snapshot_url = upload_text_to_oss(snapshot_object_key, snapshot_html)
                snapshot_html = ""
            except Exception as exc:
                return {
                    "callback_status": 2,
                    "callback_error": f"snapshot upload failed: {str(exc)[:900]}",
                    "oss_object_key": object_key,
                    "oss_result_url": result_url,
                    "snapshot_object_key": "",
                    "snapshot_url": "",
                    "snapshot_html": snapshot_html,
                }
    if result_url:
        payload = build_job_callback_payload(
            SimpleNamespace(
                task_id=str(row.get("task_id") or ""),
                req_ssn=str(row.get("req_ssn") or row.get("task_id") or ""),
                job_type=str(row.get("job_type") or "AmazonReviewJob"),
                status="completed" if success else "failed",
                result_url=result_url,
                snapshot_url=snapshot_url,
                error_msg=str(row.get("error_msg") or ""),
                fail_reason=_extract_fail_reason(str(row.get("error_msg") or ""), row_status),
            )
        )
        send_result = _send_callback(callback_url, payload)
        task_id = str(row.get("task_id") or "")
        if send_result["callback_status"] != 1:
            logger.warning(
                f"[callback] 回调失败 task_id={task_id} error={send_result['callback_error']}"
            )
        else:
            logger.info(f"[callback] 回调成功 task_id={task_id} oss_key={object_key}")
        return {
            "callback_status": send_result["callback_status"],
            "callback_error": send_result["callback_error"],
            "oss_object_key": object_key,
            "oss_result_url": result_url,
            "snapshot_object_key": snapshot_object_key,
            "snapshot_url": snapshot_url,
            "snapshot_html": snapshot_html,
        }

    return dispatch_single_task_callback(
        callback_url=callback_url,
        task_id=str(row.get("task_id") or ""),
        asin=str(row.get("asin") or ""),
        region=str(row.get("region") or ""),
        result_data=result_data,
        result_count=result_count,
        success=success,
        error_msg=str(row.get("error_msg") or ""),
        extra={
            **extra,
            "req_ssn": str(row.get("req_ssn") or row.get("task_id") or ""),
            "job_type": str(row.get("job_type") or "AmazonReviewJob"),
            "snapshot_url": str(row.get("snapshot_url") or ""),
        },
    )
