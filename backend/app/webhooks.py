"""Workspace webhook notifications.

Task code only enqueues delivery rows. Delivery dispatch is separate so slow or
failing customer endpoints never block crawler state transitions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from ipaddress import ip_address
from urllib.parse import urlparse

import requests
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import WebhookConfig, WebhookDelivery, WorkspaceSite

logger = logging.getLogger("smart-crawler.webhooks")

SIGNATURE_HEADER = "X-SmartCrawler-Signature"
EVENT_HEADER = "X-SmartCrawler-Event"
DELIVERY_HEADER = "X-SmartCrawler-Delivery"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def generate_secret() -> str:
    """Return a signing secret suitable for showing once in the settings UI."""
    return "whsec_" + secrets.token_urlsafe(32)


def validate_webhook_url(url: str) -> str:
    """Validate and normalize an outbound webhook URL.

    This blocks obvious SSRF footguns without doing DNS resolution in the
    request path. Hostname policies are intentionally conservative.
    """
    value = (url or "").strip()
    if not value:
        raise ValueError("webhook URL 不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webhook URL 只支持 http/https")
    if not parsed.hostname:
        raise ValueError("webhook URL 缺少 host")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise ValueError("webhook URL 不能指向本机地址")
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or
               ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise ValueError("webhook URL 不能指向内网或保留 IP")
    return value


def payload_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")


def _is_dingtalk_robot_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return host == "oapi.dingtalk.com" and "/robot/send" in parsed.path


def _dingtalk_text_payload(payload: dict) -> dict:
    job = payload.get("job") or {}
    result = job.get("result") or {}
    lines = [
        f"SmartCrawler {payload.get('event') or 'job.event'}",
        f"workspace: {payload.get('workspace_id')}",
        f"job: {job.get('kind') or '-'} #{job.get('id') or '-'} {job.get('status') or '-'}",
    ]
    site = result.get("site")
    if site:
        lines.append(f"site: {site}")
    trigger = result.get("trigger")
    if trigger:
        lines.append(f"trigger: {trigger}")
    for key, label in (
        ("products_count", "products"),
        ("products", "products"),
        ("new_count", "new"),
        ("new", "new"),
        ("promotion_count", "promotions"),
        ("promotions", "promotions"),
    ):
        value = result.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    error = job.get("error")
    if error:
        lines.append(f"error: {str(error)[:300]}")
    return {
        "msgtype": "text",
        "text": {"content": "\n".join(lines)},
    }


def _outbound_payload(url: str, payload: dict) -> dict:
    if _is_dingtalk_robot_url(url):
        return _dingtalk_text_payload(payload)
    return payload


def sign_payload(payload: dict, secret: str) -> str:
    digest = hmac.new((secret or "").encode("utf-8"), payload_bytes(payload),
                      hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _response_success(url: str, resp) -> bool:
    if not 200 <= resp.status_code < 300:
        return False
    if _is_dingtalk_robot_url(url):
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(resp.text or "{}")
            except Exception:
                data = {}
        if isinstance(data, dict) and data.get("errcode") not in (None, 0):
            return False
    return True


def build_payload(*, delivery_id: int, workspace_id: int,
                  job_kind: str, job_id: int, status: str,
                  event_type: str | None = None,
                  created_at: datetime | None = None,
                  finished_at: datetime | None = None,
                  error: str | None = None,
                  result: dict | None = None) -> dict:
    if event_type is None:
        event_type = "job.success" if status == "success" else "job.failed"
    event_time = finished_at if event_type == "job.completed" else datetime.utcnow()
    return {
        "event": event_type,
        "webhook_id": f"whd_{delivery_id}",
        "timestamp": _iso(event_time),
        "workspace_id": workspace_id,
        "job": {
            "id": job_id,
            "kind": job_kind,
            "status": status,
            "created_at": _iso(created_at),
            "finished_at": _iso(finished_at),
            "error": error,
            "result": result or {},
        },
    }


def _configs_for_event(db: Session, *, workspace_id: int | None,
                       site: str | None = None) -> list[WebhookConfig]:
    q = db.query(WebhookConfig).filter(WebhookConfig.active.is_(True))
    if workspace_id:
        return q.filter(WebhookConfig.workspace_id == workspace_id).all()
    if site:
        return (
            q.join(WorkspaceSite, WorkspaceSite.workspace_id == WebhookConfig.workspace_id)
            .filter(WorkspaceSite.site == site,
                    WorkspaceSite.enabled.is_(True),
                    or_(WorkspaceSite.hidden.is_(False),
                        WorkspaceSite.hidden.is_(None)))
            .all()
        )
    return []


def enqueue_delivery(db: Session, *, workspace_id: int | None,
                     event_type: str, job_kind: str, job_id: int,
                     status: str, site: str | None = None,
                     created_at: datetime | None = None,
                     finished_at: datetime | None = None,
                     error: str | None = None,
                     result: dict | None = None) -> int:
    """Insert pending deliveries for matching active configs.

    Returns the number of rows added. All errors are swallowed and logged because
    notification bookkeeping must not change crawler outcomes.
    """
    try:
        configs = _configs_for_event(db, workspace_id=workspace_id, site=site)
        count = 0
        now = datetime.utcnow()
        for cfg in configs:
            delivery = WebhookDelivery(
                workspace_id=cfg.workspace_id,
                config_id=cfg.id,
                event_type=event_type,
                job_kind=job_kind,
                job_id=job_id,
                status="pending",
                retries=0,
                max_retries=5,
                next_retry_at=now,
                created_at=now,
            )
            db.add(delivery)
            db.flush()
            delivery.payload = build_payload(
                delivery_id=delivery.id,
                workspace_id=cfg.workspace_id,
                event_type=event_type,
                job_kind=job_kind,
                job_id=job_id,
                status=status,
                created_at=created_at,
                finished_at=finished_at,
                error=error,
                result=result,
            )
            count += 1
        return count
    except Exception as exc:
        logger.warning("enqueue webhook delivery failed: %s", exc)
        return 0


def _backoff(retries: int) -> timedelta:
    table = {1: 30, 2: 120, 3: 600, 4: 1800}
    return timedelta(seconds=table.get(retries, 3600))


def dispatch_pending(db: Session, *, limit: int = 20,
                     timeout: float = 6.0) -> int:
    """Send due pending deliveries and update their statuses."""
    now = datetime.utcnow()
    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.status == "pending",
                or_(WebhookDelivery.next_retry_at.is_(None),
                    WebhookDelivery.next_retry_at <= now))
        .order_by(WebhookDelivery.id)
        .limit(max(1, min(limit, 100)))
        .all()
    )
    sent = 0
    for delivery in rows:
        cfg = db.get(WebhookConfig, delivery.config_id)
        if not cfg or not cfg.active:
            delivery.status = "failed"
            delivery.response_snippet = "webhook config inactive or missing"
            delivery.finished_at = datetime.utcnow()
            sent += 1
            continue
        payload = delivery.payload or {}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "smart-crawler-webhook/1.0",
            EVENT_HEADER: delivery.event_type or payload.get("event") or "",
            DELIVERY_HEADER: f"whd_{delivery.id}",
            SIGNATURE_HEADER: sign_payload(payload, cfg.secret or ""),
        }
        try:
            outbound = _outbound_payload(cfg.url, payload)
            resp = requests.post(
                cfg.url,
                data=payload_bytes(outbound),
                headers=headers,
                timeout=timeout,
            )
            delivery.http_status = resp.status_code
            delivery.response_snippet = (resp.text or "")[:500]
            if _response_success(cfg.url, resp):
                delivery.status = "success"
                delivery.finished_at = datetime.utcnow()
            else:
                _mark_retry(delivery, f"http {resp.status_code}")
            sent += 1
        except Exception as exc:
            delivery.response_snippet = str(exc)[:500]
            _mark_retry(delivery, str(exc))
            sent += 1
    return sent


def _mark_retry(delivery: WebhookDelivery, reason: str) -> None:
    retries = (delivery.retries or 0) + 1
    delivery.retries = retries
    if retries >= (delivery.max_retries or 5):
        delivery.status = "failed"
        delivery.finished_at = datetime.utcnow()
        if not delivery.response_snippet:
            delivery.response_snippet = reason[:500]
        return
    delivery.status = "pending"
    delivery.next_retry_at = datetime.utcnow() + _backoff(retries)
