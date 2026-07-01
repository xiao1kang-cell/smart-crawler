"""Postgres adapter for the legacy Amazon crawler DB surface.

The migrated worker/account scheduler still imports ``MySQLTaskDB`` from the
old path.  This module keeps that surface, but maps the calls used by the
review single-worker path to smart-crawler's SQLAlchemy models.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import and_, func, or_

from app.db import IS_SQLITE, session_scope
from app.models import (
    AccountUsageLog,
    AmazonCrawlerAccount,
    AmazonListingJob,
    AmazonReviewJob,
    CrawlerQueueDepthSnapshot,
    CrawlerRuntimeStatus,
    ReviewsError,
)
from app.crawlers.amazon_crawler.shuler.util.oss_callback import send_job_callback


class _CompatConnection:
    in_transaction = False

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    def start_transaction(self) -> None:
        return None

    def close(self) -> None:
        return None


class _CompatCursor:
    def execute(self, *_args, **_kwargs) -> None:
        logger.warning("[mysql-adapter] raw cursor.execute is not supported in Postgres adapter")

    def executemany(self, *_args, **_kwargs) -> None:
        logger.warning("[mysql-adapter] raw cursor.executemany is not supported in Postgres adapter")

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self) -> None:
        return None


def _market(value: Any) -> str:
    market = str(value or "US").strip().upper()
    return {"GB": "UK"}.get(market, market)


def _json_obj(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return value


def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _datetime_value(value: Any, default: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            normalized = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is not None:
                    return parsed.astimezone().replace(tzinfo=None)
                return parsed
            except ValueError:
                try:
                    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
    return default or datetime.now()


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _account_row(row: AmazonCrawlerAccount) -> Dict[str, Any]:
    return {
        "id": int(row.id or 0),
        "platform": row.platform or "amazon",
        "username": row.username or "",
        "password": row.password or "",
        "country": _market(row.country),
        "cookies": _json_obj(row.cookies) or {},
        "proxy_": _json_obj(row.proxy_config) or {},
        "fingerprint_id": row.fingerprint_id or "",
        "static_ip": row.static_ip or "",
        "totp_secret": row.totp_secret or "",
        "state": int(row.state if row.state is not None else 1),
        "is_used": bool(row.is_used),
        "last_used_time": float(row.last_used_time or 0.0),
        "fail_count": int(row.fail_count or 0),
        "cooldown_until": float(row.cooldown_until or 0.0),
        "city": row.city or "",
        "user_agent": row.user_agent or "",
        "refresh_time": row.refresh_time or "",
        "create_time": row.create_time or "",
        "update_time": row.update_time or "",
        "quota_factor": float(row.quota_factor or 1.0),
        "label": row.label or "",
    }


def _assign_account_fields(row: AmazonCrawlerAccount, account_dict: Dict[str, Any], *, insert: bool = False) -> None:
    now = _now_str()
    if insert:
        row.platform = str(account_dict.get("platform") or "amazon")
        row.username = str(account_dict.get("username") or "").strip()
        row.create_time = str(account_dict.get("create_time") or now)
    if "password" in account_dict:
        row.password = account_dict.get("password") or row.password
    if "country" in account_dict:
        row.country = _market(account_dict.get("country"))
    if "cookies" in account_dict:
        row.cookies = _json_obj(account_dict.get("cookies")) or account_dict.get("cookies")
    if "proxy_" in account_dict:
        row.proxy_config = _json_obj(account_dict.get("proxy_")) or account_dict.get("proxy_")
    if "fingerprint_id" in account_dict:
        row.fingerprint_id = account_dict.get("fingerprint_id") or row.fingerprint_id
    if "static_ip" in account_dict:
        row.static_ip = account_dict.get("static_ip") or ""
    if "totp_secret" in account_dict:
        row.totp_secret = account_dict.get("totp_secret") or row.totp_secret
    if "state" in account_dict:
        row.state = int(account_dict.get("state") if account_dict.get("state") is not None else row.state or 1)
    if "is_used" in account_dict:
        row.is_used = _bool_value(account_dict.get("is_used"))
    if "last_used_time" in account_dict:
        row.last_used_time = float(account_dict.get("last_used_time") or 0.0)
    if "fail_count" in account_dict:
        row.fail_count = int(account_dict.get("fail_count") or 0)
    if "cooldown_until" in account_dict:
        row.cooldown_until = float(account_dict.get("cooldown_until") or 0.0)
    if "city" in account_dict:
        row.city = account_dict.get("city") or row.city
    if "user_agent" in account_dict:
        row.user_agent = account_dict.get("user_agent") or row.user_agent
    if "refresh_time" in account_dict:
        row.refresh_time = account_dict.get("refresh_time") or row.refresh_time
    if "quota_factor" in account_dict:
        row.quota_factor = float(account_dict.get("quota_factor") or 1.0)
    if "label" in account_dict:
        row.label = account_dict.get("label") or ""
    row.update_time = str(account_dict.get("update_time") or now)


def _apply_account_filters(q, criteria: Optional[Dict[str, Any]], *, active_only: bool = False):
    criteria = dict(criteria or {})
    has_platform = "platform" in criteria
    has_label = "label" in criteria
    if active_only:
        q = q.filter(AmazonCrawlerAccount.state == 1)
    for key, value in criteria.items():
        if value is None or value == "":
            continue
        if key in {"country", "market", "region"}:
            q = q.filter(func.upper(AmazonCrawlerAccount.country) == _market(value))
        elif key == "platform":
            q = q.filter(func.lower(AmazonCrawlerAccount.platform) == str(value).lower())
        elif key == "label":
            q = q.filter(AmazonCrawlerAccount.label == value)
        elif hasattr(AmazonCrawlerAccount, key):
            q = q.filter(getattr(AmazonCrawlerAccount, key) == value)
    if not has_platform:
        q = q.filter(or_(
            AmazonCrawlerAccount.platform.is_(None),
            func.lower(AmazonCrawlerAccount.platform) == "amazon",
        ))
    if not has_label:
        q = q.filter(or_(AmazonCrawlerAccount.label.is_(None), AmazonCrawlerAccount.label != "stress_test"))
    return q


def _job_row(job: AmazonReviewJob) -> Dict[str, Any]:
    payload = _json_obj(job.payload) or {}
    return {
        "id": int(job.id),
        "task_id": job.task_id,
        "req_ssn": job.req_ssn or job.task_id,
        "asin": job.asin,
        "region": _market(job.market),
        "country": _market(job.market),
        "params": payload,
        "priority": int(job.priority or 100),
        "need_crawler_time": job.next_attempt_at or job.created_at or datetime.utcnow(),
        "source": payload.get("source", ""),
        "callback": job.callback_url or payload.get("callback", ""),
    }


def _listing_row(job: AmazonListingJob) -> Dict[str, Any]:
    payload = _json_obj(job.payload) or {}
    return {
        "id": int(job.id),
        "task_id": job.task_id,
        "asin": job.asin,
        "region": _market(job.market),
        "country": _market(job.market),
        "params": payload,
        "priority": int(job.priority or 100),
        "need_crawler_time": job.next_attempt_at or job.created_at or datetime.utcnow(),
        "callback": job.callback_url or payload.get("callback", ""),
    }


def _callback_row(job: AmazonReviewJob | AmazonListingJob) -> Dict[str, Any]:
    payload = _json_obj(job.payload) or {}
    return {
        "id": int(job.id),
        "job_type": job.job_type or job.__class__.__name__,
        "task_id": job.task_id or "",
        "tenant_id": job.tenant_id or "",
        "app_id": job.app_id or "",
        "req_ssn": job.req_ssn or "",
        "asin": job.asin or "",
        "region": _market(job.market),
        "country": _market(job.market),
        "status": 2 if str(job.status or "").lower() == "completed" else 3,
        "callback_url": job.callback_url or "",
        "callback_status": job.callback_status or "none",
        "callback_attempts": int(job.callback_attempts or 0),
        "callback_last_error": job.callback_last_error or "",
        "callback_updated_at": job.callback_updated_at,
        "oss_object_key": job.oss_object_key or "",
        "oss_result_url": job.result_url or "",
        "result_url": job.result_url or "",
        "snapshot_url": job.snapshot_url or "",
        "snapshot_object_key": job.snapshot_object_key or "",
        "snapshot_html": job.snapshot_html or "",
        "result_count": int(job.result_count or 0),
        "result": job.result_data,
        "error_msg": job.error_msg or "",
        "fail_reason": job.fail_reason or "",
        "params": payload,
    }


_LEGACY_STATUS_TO_JOB_STATUS = {
    0: ("queued",),
    1: ("running",),
    2: ("completed",),
    3: ("failed",),
}


class MySQLTaskDB:
    """Compatibility class used by the unmodified legacy worker code."""

    supports_legacy_mysql_tables = False

    def __init__(self, *args, **kwargs) -> None:
        self.conn = _CompatConnection()
        self.cursor = _CompatCursor()

    def _check_connection(self) -> None:
        return None

    def _table_exists(self, _table_name: str) -> bool:
        return False

    def close(self) -> None:
        return None

    def ensure_monitoring_tables(self) -> None:
        return None

    def ensure_single_task_callback_columns(self) -> None:
        return None

    def record_queue_depth_snapshot(self, snapshot: Dict[str, int], queue_keys: Dict[str, str]) -> None:
        if not snapshot:
            return
        now = datetime.utcnow()
        rows = [
            CrawlerQueueDepthSnapshot(
                queue_name=str(name),
                redis_key=str((queue_keys or {}).get(name, "")),
                depth=int(depth),
                created_at=now,
            )
            for name, depth in snapshot.items()
        ]
        with session_scope() as s:
            s.add_all(rows)

    def cleanup_queue_depth_snapshots(self, retain_hours: int = 72) -> int:
        cutoff = datetime.utcnow() - timedelta(hours=int(retain_hours or 72))
        with session_scope() as s:
            return int(
                s.query(CrawlerQueueDepthSnapshot)
                .filter(CrawlerQueueDepthSnapshot.created_at < cutoff)
                .delete(synchronize_session=False)
                or 0
            )

    def update_runtime_status(self, component: str, status: str = "ok", message: str = "") -> None:
        now = datetime.utcnow()
        with session_scope() as s:
            row = s.get(CrawlerRuntimeStatus, str(component)[:128])
            if row is None:
                row = CrawlerRuntimeStatus(component=str(component)[:128])
                s.add(row)
            row.status = str(status or "ok")[:16]
            row.message = str(message or "")
            row.updated_at = now

    def get_runtime_statuses(self, components: List[str]) -> Dict[str, Dict[str, Any]]:
        names = [str(component)[:128] for component in components or [] if str(component or "").strip()]
        if not names:
            return {}
        with session_scope() as s:
            rows = s.query(CrawlerRuntimeStatus).filter(CrawlerRuntimeStatus.component.in_(names)).all()
            return {
                str(row.component): {
                    "component": row.component,
                    "status": row.status,
                    "message": row.message,
                    "updated_at": row.updated_at,
                }
                for row in rows
            }

    def reset_stuck_tasks_to_retry(self, table: str, time_field: str, stuck_minutes: int) -> int:
        model = self._legacy_task_model(table)
        if model is None:
            return 0
        field_name = "updated_at" if str(time_field or "") in {"updated_at", "update_time"} else str(time_field or "")
        field = getattr(model, field_name, None)
        if field is None:
            return 0
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=int(stuck_minutes or 0))
        with session_scope() as s:
            rows = s.query(model).filter(model.status == "running", field < cutoff).all()
            for job in rows:
                job.status = "queued"
                job.worker = None
                job.heartbeat_at = None
                job.next_attempt_at = now
                job.updated_at = now
            return len(rows)

    @staticmethod
    def _legacy_task_model(table: str):
        table = str(table or "").strip()
        if table == "crawl_single_tasks":
            return AmazonReviewJob
        if table in {"crawl_asin_detail_tasks", "crawl_asin_tasks"}:
            return AmazonListingJob
        return None

    def load_all_accounts(self, filter_conditions: Dict = None) -> List[Dict]:
        with session_scope() as s:
            q = s.query(AmazonCrawlerAccount)
            q = _apply_account_filters(q, filter_conditions, active_only=True)
            return [_account_row(row) for row in q.order_by(AmazonCrawlerAccount.last_used_time.asc()).all()]

    def release_timeout_accounts_by_filter(self, filter_conditions: Dict = None, timeout_threshold_ts: float = 0.0) -> int:
        now = _now_str()
        with session_scope() as s:
            q = s.query(AmazonCrawlerAccount).filter(
                AmazonCrawlerAccount.state == 1,
                AmazonCrawlerAccount.is_used.is_(True),
                AmazonCrawlerAccount.last_used_time > 0,
                AmazonCrawlerAccount.last_used_time < float(timeout_threshold_ts or 0.0),
            )
            q = _apply_account_filters(q, filter_conditions)
            rows = q.all()
            for row in rows:
                row.is_used = False
                row.cooldown_until = 0.0
                row.update_time = now
            return len(rows)

    def load_available_account_candidates(self, filter_conditions: Dict = None, now_ts: float = None, limit: int = 0) -> List[Dict]:
        now_ts = float(now_ts if now_ts is not None else time.time())
        with session_scope() as s:
            q = s.query(AmazonCrawlerAccount).filter(
                AmazonCrawlerAccount.state == 1,
                or_(AmazonCrawlerAccount.is_used.is_(None), AmazonCrawlerAccount.is_used.is_(False)),
                or_(AmazonCrawlerAccount.cooldown_until.is_(None), AmazonCrawlerAccount.cooldown_until <= now_ts),
            )
            q = _apply_account_filters(q, filter_conditions)
            q = q.order_by(AmazonCrawlerAccount.last_used_time.asc(), AmazonCrawlerAccount.username.asc())
            if int(limit or 0) > 0:
                q = q.limit(int(limit))
            return [_account_row(row) for row in q.all()]

    def count_accounts_by_filter(self, filter_conditions: Dict = None, active_only: bool = False) -> int:
        with session_scope() as s:
            q = _apply_account_filters(s.query(AmazonCrawlerAccount), filter_conditions, active_only=active_only)
            return int(q.count() or 0)

    def count_tasks_by_legacy_table(self, table_name: str, legacy_status: int) -> int:
        table = str(table_name or "").strip()
        statuses = _LEGACY_STATUS_TO_JOB_STATUS.get(int(legacy_status), ())
        if not statuses:
            return 0
        model = AmazonListingJob if table in {"crawl_asin_detail_tasks", "crawl_asin_tasks"} else AmazonReviewJob
        with session_scope() as s:
            return int(s.query(model).filter(model.status.in_(statuses)).count() or 0)

    def get_account_by_username(self, username: str, platform: str = "amazon") -> Optional[Dict]:
        with session_scope() as s:
            row = s.query(AmazonCrawlerAccount).filter(
                func.lower(AmazonCrawlerAccount.username) == str(username or "").lower(),
                or_(AmazonCrawlerAccount.platform.is_(None), func.lower(AmazonCrawlerAccount.platform) == platform.lower()),
            ).first()
            return _account_row(row) if row else None

    def update_account(self, account_dict: Dict) -> None:
        username = str(account_dict.get("username") or "").strip()
        if not username:
            return
        platform = str(account_dict.get("platform") or "amazon")
        with session_scope() as s:
            row = s.query(AmazonCrawlerAccount).filter(
                func.lower(AmazonCrawlerAccount.username) == username.lower(),
                or_(AmazonCrawlerAccount.platform.is_(None), func.lower(AmazonCrawlerAccount.platform) == platform.lower()),
            ).first()
            if row is None:
                return
            account_dict = dict(account_dict)
            account_dict.pop("id", None)
            account_dict.pop("username", None)
            account_dict.pop("platform", None)
            _assign_account_fields(row, account_dict)

    def insert_account(self, account_dict: Dict) -> None:
        username = str(account_dict.get("username") or "").strip()
        if not username:
            return
        platform = str(account_dict.get("platform") or "amazon")
        with session_scope() as s:
            row = s.query(AmazonCrawlerAccount).filter(
                func.lower(AmazonCrawlerAccount.username) == username.lower(),
                func.lower(AmazonCrawlerAccount.platform) == platform.lower(),
            ).first()
            if row is None:
                row = AmazonCrawlerAccount(username=username, platform=platform)
                s.add(row)
                insert = True
            else:
                insert = False
            _assign_account_fields(row, account_dict, insert=insert)

    def insert_accounts_batch(self, accounts: List[Dict]) -> None:
        for account in accounts or []:
            self.insert_account(account)

    def release_account_by_username(self, username: str, platform: str = "amazon", note: str = "") -> int:
        if not username:
            return 0
        now = _now_str()
        with session_scope() as s:
            row = s.query(AmazonCrawlerAccount).filter(
                func.lower(AmazonCrawlerAccount.username) == str(username).lower(),
                or_(AmazonCrawlerAccount.platform.is_(None), func.lower(AmazonCrawlerAccount.platform) == platform.lower()),
            ).first()
            if row is None:
                return 0
            row.is_used = False
            row.cooldown_until = 0.0
            row.update_time = now
            return 1

    def _claim_query(self, s, *, region: str = None, source: str = None):
        now = datetime.utcnow()
        q = s.query(AmazonReviewJob).filter(
            AmazonReviewJob.status == "queued",
            AmazonReviewJob.next_attempt_at <= now,
        )
        if region:
            q = q.filter(func.upper(AmazonReviewJob.market) == _market(region))
        if source and source not in {"normal", "None", "none", "default"}:
            q = q.filter(AmazonReviewJob.payload["source"].as_string() == source)
        else:
            q = q.filter(or_(
                AmazonReviewJob.payload.is_(None),
                AmazonReviewJob.payload["source"].as_string().is_(None),
                AmazonReviewJob.payload["source"].as_string() != "stress_test",
            ))
        return q

    def claim_single_task_by_id(self, row_id: int, region: str = None, source: str = None, worker_name: str = "") -> Optional[Dict]:
        now = datetime.utcnow()
        with session_scope() as s:
            q = self._claim_query(s, region=region, source=source).filter(AmazonReviewJob.id == int(row_id))
            if not IS_SQLITE:
                q = q.with_for_update(skip_locked=True)
            job = q.first()
            if job is None:
                return None
            job.status = "running"
            job.worker = worker_name or None
            job.started_at = now
            job.heartbeat_at = now
            job.updated_at = now
            return _job_row(job)

    def claim_single_task_by_task_id(self, task_id: str, region: str = None, source: str = None, worker_name: str = "") -> Optional[Dict]:
        now = datetime.utcnow()
        with session_scope() as s:
            q = self._claim_query(s, region=region, source=source).filter(AmazonReviewJob.task_id == str(task_id))
            if not IS_SQLITE:
                q = q.with_for_update(skip_locked=True)
            job = q.first()
            if job is None:
                return None
            job.status = "running"
            job.worker = worker_name or None
            job.started_at = now
            job.heartbeat_at = now
            job.updated_at = now
            return _job_row(job)

    def update_single_task_result(
        self,
        task_id: str,
        success: bool,
        result_count: int = 0,
        error_msg: str = "",
        result_data: Any = None,
        force_final: bool = False,
        expected_row_id: int = None,
        expected_worker_name: str = None,
        **_kwargs,
    ) -> Dict[str, Any]:
        now = datetime.utcnow()
        with session_scope() as s:
            q = s.query(AmazonReviewJob).filter(AmazonReviewJob.task_id == str(task_id))
            if expected_row_id is not None:
                q = q.filter(AmazonReviewJob.id == int(expected_row_id))
            job = q.first()
            if job is None:
                return {"skipped": True, "reason": "missing"}
            if expected_worker_name and job.worker and job.worker != expected_worker_name:
                return {"skipped": True, "reason": "worker_mismatch"}
            job.result_count = int(result_count or 0)
            job.result_data = result_data
            job.error_msg = str(error_msg or "")
            job.updated_at = now
            job.worker = None
            job.heartbeat_at = None
            if success:
                job.status = "completed"
                job.fail_reason = ""
                job.completed_at = now
            elif force_final:
                job.status = "failed"
                job.fail_reason = "error"
                job.completed_at = now
            else:
                job.status = "queued"
                job.fail_reason = "retry"
                job.next_attempt_at = now + timedelta(seconds=20)
            return {"skipped": False}

    def reset_single_tasks_by_ids(self, row_ids: List[int], error_msg: str = "") -> int:
        now = datetime.utcnow()
        ids = [int(x) for x in row_ids or []]
        if not ids:
            return 0
        with session_scope() as s:
            rows = s.query(AmazonReviewJob).filter(AmazonReviewJob.id.in_(ids)).all()
            for job in rows:
                job.status = "queued"
                job.worker = None
                job.heartbeat_at = None
                job.error_msg = error_msg or job.error_msg
                job.next_attempt_at = now + timedelta(seconds=20)
                job.updated_at = now
            return len(rows)

    def fail_or_retry_single_task_by_id(self, row_id: int, error_msg: str = "", worker_name: str = "", **_kwargs) -> Dict[str, Any]:
        now = datetime.utcnow()
        with session_scope() as s:
            job = s.get(AmazonReviewJob, int(row_id))
            if job is None:
                return {"skipped": True, "reason": "missing"}
            retries = int(job.retries or 0) + 1
            job.retries = retries
            job.worker = None
            job.heartbeat_at = None
            job.error_msg = error_msg or job.error_msg
            job.updated_at = now
            if retries < int(job.max_retries or 3):
                job.status = "queued"
                job.next_attempt_at = now + timedelta(seconds=30)
            else:
                job.status = "failed"
                job.completed_at = now
            return {"skipped": False, "retries": retries, "status": job.status}

    def update_single_task_callback_state(self, task_id: str, **kwargs) -> None:
        with session_scope() as s:
            job = s.query(AmazonReviewJob).filter(AmazonReviewJob.task_id == str(task_id)).first()
            if job is None:
                job = s.query(AmazonListingJob).filter(AmazonListingJob.task_id == str(task_id)).first()
            if job is None:
                return
            if "callback_status" in kwargs:
                value = kwargs.get("callback_status")
                if value in {1, "1", "success"}:
                    job.callback_status = "success"
                elif value in {3, "3", "none"}:
                    job.callback_status = "none"
                else:
                    job.callback_status = "failed"
            if "callback_last_error" in kwargs:
                job.callback_last_error = str(kwargs.get("callback_last_error") or "")
            if "callback_url" in kwargs:
                job.callback_url = str(kwargs.get("callback_url") or "")
            if "oss_object_key" in kwargs:
                job.oss_object_key = str(kwargs.get("oss_object_key") or "")[:512]
            if "oss_result_url" in kwargs:
                job.result_url = str(kwargs.get("oss_result_url") or "")
            if "snapshot_object_key" in kwargs:
                job.snapshot_object_key = str(kwargs.get("snapshot_object_key") or "")[:512]
            if "snapshot_url" in kwargs:
                job.snapshot_url = str(kwargs.get("snapshot_url") or "")
            if "snapshot_html" in kwargs:
                job.snapshot_html = str(kwargs.get("snapshot_html") or "")
            if kwargs.get("increment_attempts"):
                job.callback_attempts = int(job.callback_attempts or 0) + 1
            job.callback_updated_at = datetime.utcnow()

    def list_retryable_single_callbacks(
            self,
            limit: int = 50,
            max_attempts: int = 5,
            min_retry_interval_seconds: int = 300,
    ) -> List[Dict[str, Any]]:
        cutoff = datetime.utcnow() - timedelta(seconds=int(min_retry_interval_seconds or 0))
        rows: List[Dict[str, Any]] = []
        per_model_limit = max(int(limit or 50), 1)
        with session_scope() as s:
            for model in (AmazonReviewJob, AmazonListingJob):
                q = s.query(model).filter(
                    model.status.in_(("completed", "failed")),
                    model.callback_url.is_not(None),
                    model.callback_url != "",
                    model.callback_status.in_(("pending", "failed")),
                    model.callback_attempts < int(max_attempts or 5),
                    or_(model.callback_updated_at.is_(None), model.callback_updated_at <= cutoff),
                )
                q = q.order_by(model.callback_updated_at.asc().nullsfirst(), model.id.asc()).limit(per_model_limit)
                rows.extend(_callback_row(job) for job in q.all())
        rows.sort(key=lambda row: (row.get("callback_updated_at") or datetime.min, int(row.get("id") or 0)))
        return rows[:int(limit or 50)]

    def ensure_asin_detail_tasks_table(self) -> None:
        return None

    def ensure_static_ip_column(self) -> None:
        return None

    def _claim_asin_query(self, s, *, region: str = None):
        now = datetime.utcnow()
        q = s.query(AmazonListingJob).filter(
            AmazonListingJob.status == "queued",
            AmazonListingJob.next_attempt_at <= now,
        )
        if region:
            q = q.filter(func.upper(AmazonListingJob.market) == _market(region))
        return q

    def claim_asin_task_by_id(self, row_id: int, region: str = None) -> Optional[Dict]:
        now = datetime.utcnow()
        with session_scope() as s:
            q = self._claim_asin_query(s, region=region).filter(AmazonListingJob.id == int(row_id))
            if not IS_SQLITE:
                q = q.with_for_update(skip_locked=True)
            job = q.first()
            if job is None:
                return None
            job.status = "running"
            job.worker = "asin-worker"
            job.started_at = now
            job.heartbeat_at = now
            job.updated_at = now
            return _listing_row(job)

    def claim_asin_task_by_task_id(self, task_id: str, region: str = None) -> Optional[Dict]:
        now = datetime.utcnow()
        with session_scope() as s:
            q = self._claim_asin_query(s, region=region).filter(AmazonListingJob.task_id == str(task_id))
            if not IS_SQLITE:
                q = q.with_for_update(skip_locked=True)
            job = q.first()
            if job is None:
                return None
            job.status = "running"
            job.worker = "asin-worker"
            job.started_at = now
            job.heartbeat_at = now
            job.updated_at = now
            return _listing_row(job)

    def reset_asin_detail_tasks_by_ids(self, row_ids: List[int], error_msg: str = "") -> int:
        ids = [int(x) for x in row_ids or []]
        if not ids:
            return 0
        now = datetime.utcnow()
        with session_scope() as s:
            rows = s.query(AmazonListingJob).filter(AmazonListingJob.id.in_(ids)).all()
            for job in rows:
                job.status = "queued"
                job.worker = None
                job.heartbeat_at = None
                job.error_msg = error_msg or job.error_msg
                job.next_attempt_at = now + timedelta(seconds=20)
                job.updated_at = now
            return len(rows)

    def complete_asin_detail_task(
            self,
            task_id: int | str,
            result: Optional[Dict],
            success: bool,
            error_msg: str = "",
            snapshot_html: str = "",
    ) -> None:
        now = datetime.utcnow()
        callback_task_id = ""
        with session_scope() as s:
            job = None
            try:
                job = s.get(AmazonListingJob, int(task_id))
            except (TypeError, ValueError):
                job = None
            if job is None:
                job = s.query(AmazonListingJob).filter(AmazonListingJob.task_id == str(task_id)).first()
            if job is None:
                return
            job.status = "completed" if success else "failed"
            job.result_data = result or {}
            job.result_count = 1 if success and result else 0
            job.error_msg = "" if success else str(error_msg or "")
            job.fail_reason = "" if success else "error"
            job.worker = None
            job.heartbeat_at = None
            job.completed_at = now
            job.updated_at = now
            callback_task_id = job.task_id if job.callback_url else ""
        if callback_task_id:
            with session_scope() as s:
                job = s.query(AmazonListingJob).filter(AmazonListingJob.task_id == callback_task_id).first()
                if job is None:
                    return
                upload_error = ""
                snapshot_ready = True
                if success and result:
                    upload_error, snapshot_ready = self._upload_listing_artifacts_to_oss(job, snapshot_html=snapshot_html)
                if snapshot_ready:
                    callback_result = send_job_callback(job)
                else:
                    callback_result = {
                        "callback_status": "failed",
                        "callback_error": upload_error or "snapshot upload failed",
                    }
                value = callback_result.get("callback_status")
                if value in {1, "1", "success"}:
                    job.callback_status = "success"
                elif value in {3, "3", "none"}:
                    job.callback_status = "none"
                else:
                    job.callback_status = "failed"
                job.callback_last_error = str(callback_result.get("callback_error") or upload_error or "")
                job.callback_attempts = int(job.callback_attempts or 0) + 1
                job.callback_updated_at = datetime.utcnow()

    @staticmethod
    def _upload_listing_artifacts_to_oss(job: AmazonListingJob, snapshot_html: str = "") -> tuple[str, bool]:
        from app.crawlers.amazon_crawler.shuler.util.oss_callback import (
            _build_object_key,
            _build_snapshot_object_key,
            upload_json_to_oss,
            upload_text_to_oss,
        )

        error_parts: list[str] = []
        try:
            if not job.result_url:
                object_key = _build_object_key(
                    job.task_id or str(job.id),
                    job.asin or "",
                    _market(job.market),
                    biz_source=job.biz_source or "",
                    tenant_id=job.tenant_id or "",
                )
                payload = {
                    "task_id": job.task_id,
                    "asin": job.asin,
                    "region": _market(job.market),
                    "tenant_id": job.tenant_id or "",
                    "biz_source": job.biz_source or "",
                    "task_type": "listing",
                    "status": 2 if str(job.status or "").lower() == "completed" else 3,
                    "result_count": int(job.result_count or 0),
                    "result": job.result_data if job.result_data is not None else {},
                    "error_msg": job.error_msg or "",
                    "created_at": datetime.utcnow().isoformat(timespec="seconds"),
                }
                job.result_url = upload_json_to_oss(object_key, payload)
                job.oss_object_key = object_key
        except Exception as exc:
            error_parts.append(f"result upload failed: {str(exc)[:900]}")
            logger.warning(f"[listing-callback] 上传结果OSS失败 task_id={job.task_id}: {exc}")

        snapshot_text = str(snapshot_html or job.snapshot_html or "")
        if snapshot_text and not job.snapshot_url:
            try:
                snapshot_key = _build_snapshot_object_key(
                    job.task_id or str(job.id),
                    job.asin or "",
                    _market(job.market),
                    biz_source=job.biz_source or "",
                    tenant_id=job.tenant_id or "",
                )
                job.snapshot_url = upload_text_to_oss(snapshot_key, snapshot_text)
                job.snapshot_object_key = snapshot_key
                job.snapshot_html = ""
            except Exception as exc:
                job.snapshot_html = snapshot_text
                job.snapshot_url = ""
                job.snapshot_object_key = ""
                error_parts.append(f"snapshot upload failed: {str(exc)[:900]}")
                logger.warning(f"[listing-callback] 上传快照OSS失败，已暂存HTML task_id={job.task_id}: {exc}")

        error_msg = "; ".join(error_parts)[:1000]
        job.callback_last_error = error_msg
        return error_msg, bool(not snapshot_text or job.snapshot_url)

    def insert_reviews_error(self, asin: str = "", country: str = "", resp: str = "", review_data: Any = None, task_info: Any = None, error_msg: str = "", **_kwargs) -> None:
        row = ReviewsError(
            asin=str(asin or "")[:32],
            country=_market(country),
            resp=str(resp or ""),
            review_data=_json_obj(review_data),
            task_info=_json_obj(task_info),
            error_msg=str(error_msg or "")[:512],
            status=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        with session_scope() as s:
            s.add(row)

    def insert_usage_log(self, **kwargs) -> None:
        now = datetime.now()
        start_time = _datetime_value(kwargs.get("start_time"), now)
        end_time = _datetime_value(kwargs.get("end_time"), now)
        row = AccountUsageLog(
            task_id=str(kwargs.get("task_id") or ""),
            asin=str(kwargs.get("asin") or ""),
            country=_market(kwargs.get("country") or ""),
            username=str(kwargs.get("username") or ""),
            success=bool(kwargs.get("success")),
            review_count=int(kwargs.get("review_count") or 0),
            expected_count=int(kwargs.get("expected_count") or 0),
            start_time=start_time,
            end_time=end_time,
            duration_seconds=int(kwargs.get("duration_seconds") or 0),
            retry_count=int(kwargs.get("retry_count") or 0),
            error_msg=str(kwargs.get("error_msg") or "")[:2000],
            worker_id=str(kwargs.get("worker_id") or "")[:128],
            ip=str(kwargs.get("ip") or "")[:128],
            task_type=str(kwargs.get("task_type") or "review")[:32],
            created_at=now,
        )
        with session_scope() as s:
            s.add(row)

    def get_all_static_ips(self, country: str = None, platform: str = "amazon") -> List[Dict]:
        with session_scope() as s:
            q = s.query(AmazonCrawlerAccount).filter(
                AmazonCrawlerAccount.state == 1,
                or_(AmazonCrawlerAccount.is_used.is_(None), AmazonCrawlerAccount.is_used.is_(False)),
                AmazonCrawlerAccount.static_ip.is_not(None),
                AmazonCrawlerAccount.static_ip != "",
                or_(AmazonCrawlerAccount.platform.is_(None), func.lower(AmazonCrawlerAccount.platform) == platform.lower()),
            )
            if country:
                q = q.filter(func.upper(AmazonCrawlerAccount.country) == _market(country))
            return [{"username": row.username or "", "static_ip": row.static_ip or ""} for row in q.all()]
