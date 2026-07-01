"""
Worker recovery registry.

Workers write the active task and the initially acquired account/fingerprint to
Redis while a task is running. The get_reviews_main parent process scans stale
records for the local node and cleans up after SIGKILL, Windows updates, or
other hard exits where Python finally/signal handlers cannot run.
"""
import os
import socket
import threading
import time
import traceback
from typing import Optional

from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PASSWORD, REDIS_PORT, REDIS_QUEUE_DB, REDIS_USERNAME,
)

ACTIVE_WORKER_PREFIX = "crawler:active_worker"
RECOVERY_LOCK_PREFIX = "crawler:active_worker_recovery"
ACTIVE_WORKER_KEY_TTL_SECONDS = int(os.getenv("ACTIVE_WORKER_KEY_TTL_SECONDS", "7200"))
ACTIVE_WORKER_HEARTBEAT_SECONDS = int(os.getenv("ACTIVE_WORKER_HEARTBEAT_SECONDS", "10"))
ACTIVE_WORKER_STALE_SECONDS = int(os.getenv("ACTIVE_WORKER_STALE_SECONDS", "120"))


def get_recovery_node_id() -> str:
    """Stable node id used to keep browser cleanup local to one Windows host."""
    configured = os.getenv("CRAWLER_NODE_ID", "").strip()
    if configured:
        return configured
    return socket.gethostname().split(".")[0]


def _make_redis():
    import redis as redis_lib
    return redis_lib.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        username=REDIS_USERNAME,
        password=REDIS_PASSWORD,
        db=REDIS_QUEUE_DB,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def _key(worker_name: str) -> str:
    return f"{ACTIVE_WORKER_PREFIX}:{worker_name}"


class WorkerRecoveryTracker:
    """Per-process active task heartbeat."""

    def __init__(self, worker_name: str, node_id: str = None):
        self.worker_name = worker_name
        self.node_id = node_id or get_recovery_node_id()
        self._redis = None
        self._active_key: Optional[str] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name=f"recovery-heartbeat-{worker_name}",
        )
        self._thread.start()

    def _client(self):
        if self._redis is None:
            self._redis = _make_redis()
        return self._redis

    def register(
        self,
        table: str,
        row_id,
        task_kind: str,
        asin: str = "",
        country: str = "",
        account=None,
    ) -> None:
        username = getattr(account, "username", "") if account else ""
        platform = getattr(account, "platform", "amazon") if account else "amazon"
        fingerprint_id = getattr(account, "fingerprint_id", "") if account else ""
        now = time.time()
        data = {
            "worker_name": self.worker_name,
            "node_id": self.node_id,
            "pid": str(os.getpid()),
            "table": table,
            "row_id": str(row_id),
            "task_kind": task_kind,
            "asin": asin or "",
            "country": country or "",
            "username": username or "",
            "platform": platform or "amazon",
            "fingerprint_id": fingerprint_id or "",
            "started_at": f"{now:.3f}",
            "updated_at": f"{now:.3f}",
        }
        try:
            r = self._client()
            key = _key(self.worker_name)
            r.hset(key, mapping=data)
            r.expire(key, ACTIVE_WORKER_KEY_TTL_SECONDS)
            self._active_key = key
        except Exception:
            logger.warning(f"[WorkerRecovery] 注册 active worker 失败: {traceback.format_exc()[:500]}")

    def register_session(self, task_kind: str, country: str = "", account=None) -> None:
        """Track an idle reusable account/browser session after the active task is done."""
        self.register(
            table="",
            row_id="",
            task_kind=task_kind,
            asin="",
            country=country,
            account=account,
        )

    def clear(self) -> None:
        key = self._active_key
        self._active_key = None
        if not key:
            return
        try:
            self._client().delete(key)
        except Exception:
            logger.debug(f"[WorkerRecovery] 清理 active worker key 失败 key={key}")

    def close(self) -> None:
        self.clear()
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(ACTIVE_WORKER_HEARTBEAT_SECONDS):
            key = self._active_key
            if not key:
                continue
            try:
                r = self._client()
                r.hset(key, "updated_at", f"{time.time():.3f}")
                r.expire(key, ACTIVE_WORKER_KEY_TTL_SECONDS)
            except Exception:
                self._redis = None


def _matches_node(data: dict, node_id: str = None) -> bool:
    if not node_id:
        return True
    record_node = (data.get("node_id") or "").strip()
    if record_node:
        return record_node == node_id

    # Backward-compatible fallback for records created before node_id existed.
    worker_name = data.get("worker_name") or ""
    return f"-{node_id}-" in worker_name or worker_name.endswith(f"-{node_id}")


def recover_stale_workers(
    stale_seconds: int = ACTIVE_WORKER_STALE_SECONDS,
    *,
    node_id: str = None,
    close_browser: bool = True,
) -> int:
    """Recover stale active-worker records for one node."""
    try:
        r = _make_redis()
        keys = list(r.scan_iter(f"{ACTIVE_WORKER_PREFIX}:*"))
    except Exception:
        logger.warning(f"[WorkerRecovery] Redis scan 失败: {traceback.format_exc()[:500]}")
        return 0

    recovered = 0
    now = time.time()
    for key in keys:
        try:
            data = r.hgetall(key) or {}
            if not data:
                continue
            if not _matches_node(data, node_id):
                continue
            updated_at = float(data.get("updated_at") or data.get("started_at") or 0)
            if now - updated_at < stale_seconds:
                continue

            worker_name = data.get("worker_name") or key.rsplit(":", 1)[-1]
            lock_key = f"{RECOVERY_LOCK_PREFIX}:{worker_name}"
            if not r.set(lock_key, "1", nx=True, ex=60):
                continue

            _recover_one(data, close_browser=close_browser)
            r.delete(key)
            recovered += 1
        except Exception:
            logger.error(f"[WorkerRecovery] 恢复 stale worker 失败 key={key}: {traceback.format_exc()[:800]}")
    return recovered


def _recover_one(data: dict, *, close_browser: bool = True) -> None:
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB

    table = data.get("table", "")
    row_id = data.get("row_id")
    username = data.get("username", "")
    platform = data.get("platform", "amazon") or "amazon"
    fingerprint_id = data.get("fingerprint_id", "")
    worker_name = data.get("worker_name", "")
    node_id = data.get("node_id", "")

    db = MySQLTaskDB()
    try:
        if row_id:
            if table == "crawl_single_tasks":
                update_result = db.fail_or_retry_single_task_by_id(
                    int(row_id),
                    "stale worker recovered",
                    only_running=True,
                )
                if update_result:
                    logger.warning(
                        f"[WorkerRecovery] single任务已计入重试 row_id={row_id} "
                        f"status={update_result.get('status')} "
                        f"retry={update_result.get('retry_count')}/{update_result.get('retry_times')}"
                    )
            elif table == "crawler_asin_tasks_temp":
                db.reset_temp_tasks_by_ids([int(row_id)], "stale worker recovered")
            elif table == "crawl_asin_detail_tasks":
                db.reset_asin_detail_tasks_by_ids([int(row_id)], "stale worker recovered")
            elif table == "asin_tasks":
                db.update_task_status(int(row_id), 0, "stale worker recovered", table_name="asin_tasks")

        if username:
            db.release_account_by_username(username, platform=platform, note="stale worker recovered")
    finally:
        db.close()

    if fingerprint_id and close_browser:
        try:
            from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import close_browser
            close_browser(fingerprint_id)
        except Exception as exc:
            logger.warning(f"[WorkerRecovery] 关闭指纹浏览器失败 fingerprint_id={fingerprint_id}: {exc}")

    logger.warning(
        f"[WorkerRecovery] 已恢复 stale worker={worker_name} node={node_id or '-'} table={table} "
        f"row_id={row_id} account={username} fingerprint={fingerprint_id}"
    )
