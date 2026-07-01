# ------------------------------
# 6. 进程任务：单个进程的执行逻辑
# ------------------------------
import faulthandler
import hashlib
import json
import multiprocessing
import random
import sys
import threading
import time
import traceback,os
import re
from datetime import datetime, timedelta
from multiprocessing import Process
from typing import Any, Dict, List

from loguru import logger
from mysql.connector import OperationalError

from app.crawlers.amazon_worker import _bootstrap_runtime

_bootstrap_runtime()

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import *
from app.crawlers.amazon_crawler.shuler.services.amazon.reviews import Reviews
from app.crawlers.amazon_crawler.shuler.services.amazon.task_monitor import TaskMonitor
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import (
    HumanLikeAccountManager as AccountManager,
    SchedulerLockTimeout,
)
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import (
    STRESS_TEST_LABEL, )
from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import init_metrics
from app.crawlers.amazon_crawler.shuler.util.config import APP_ENV, MYSQL_HOST_SHULEX, MYSQL_PORT_SHULEX, MYSQL_USER_SHULEX, MYSQL_PASSWORD_SHULEX, MYSQL_DB_SHULEX
from app.crawlers.amazon_crawler.shuler.util.oss_callback import dispatch_single_task_callback
from app.crawlers.amazon_crawler.shuler.util.stress_account_manager import StressTestAccountManager

# MongoAccountDB 仅 legacy/queue/temp 模式用于存评论，single 模式不需要
try:
    from app.crawlers.amazon_crawler.shuler.util.mongo_ import MongoAccountDB
except ImportError:
    MongoAccountDB = None
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message

os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"  # 双重保障 # 禁用 macOS objc fork 检查（解决崩溃核心）

import socket as _socket
_HOSTNAME = _socket.gethostname().split('.')[0]  # 取第一段，避免 FQDN 过长

EMPTY_SLEEP_SECONDS = 10
try:
    REVIEW_IDLE_SESSION_CLOSE_SECONDS = max(
        60,
        int(os.getenv("REVIEW_IDLE_SESSION_CLOSE_SECONDS", "900")),
    )
except Exception:
    REVIEW_IDLE_SESSION_CLOSE_SECONDS = 900


def _dispatch_single_task_callback_if_needed(
        *,
        mysql_db: MySQLTaskDB,
        task: Dict[str, Any],
        params: Dict[str, Any],
        task_id: str,
        asin: str,
        region: str,
        result_data: Any,
        result_count: int,
        success: bool,
        error_msg: str = "",
) -> None:
    callback_url = str((params or {}).get("callback") or task.get("callback") or "").strip()
    if not callback_url:
        return

    try:
        callback_result = dispatch_single_task_callback(
            callback_url=callback_url,
            task_id=task_id,
            asin=asin,
            region=region,
            result_data=result_data,
            result_count=result_count,
            success=success,
            error_msg=error_msg,
            extra={
                "tenant_id": task.get("tenant_id") or params.get("tenant_id", ""),
                "biz_source": params.get("biz_source", ""),
                "req_ssn": task.get("req_ssn") or task_id,
                "priority": task.get("priority"),
                "need_crawler_time": str(task.get("need_crawler_time", "")),
                "source": params.get("source", ""),
            },
        )
        mysql_db.update_single_task_callback_state(
            task_id=task_id,
            callback_url=callback_url,
            oss_object_key=callback_result.get("oss_object_key", ""),
            oss_result_url=callback_result.get("oss_result_url", ""),
            callback_status=callback_result.get("callback_status", 2),
            callback_last_error=callback_result.get("callback_error", ""),
            increment_attempts=True,
        )
    except Exception:
        logger.warning(f"[callback] 任务{task_id} 回调状态写回异常: {traceback.format_exc()[:500]}")


# 优雅停止：复用 shuler/util/stop_signal.py 的共享实现
# 别名导入，保持原文件内调用点不变
from app.crawlers.amazon_crawler.shuler.util.stop_signal import (
    check_stop_signal as _check_stop_signal,
    install_signal_handlers as _install_stop_signal_handlers,
    configure_stop_signal_scope as _configure_stop_signal_scope,
    get_stop_signal_key as _get_stop_signal_key,
)

# Redis 队列：single 用 ZSET + priority hash，先按 priority，再按 need_crawler_time；temp 仍用 BLPOP。
from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import (
    KEY_REVIEW_SINGLE_OTHER, KEY_REVIEW_SINGLE_US, KEY_REVIEW_TEMP,
    pop_task as _redis_pop_task,
    push_single_task as _redis_push_single_task,
    pop_single_task as _redis_pop_single_task,
    pop_single_task_from_keys as _redis_pop_single_task_from_keys,
    parse_queue_payload,
)
from app.crawlers.amazon_crawler.shuler.util.worker_recovery import (
    ACTIVE_WORKER_STALE_SECONDS,
    WorkerRecoveryTracker,
    get_recovery_node_id,
    recover_stale_workers,
)
from app.crawlers.amazon_crawler.shuler.util.review_worker_cleanup import (
    cleanup_review_worker_on_signal,
    close_review_browser_target,
)
from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import get_global_rate_factor

# "other" 是启动 review-single-other 时传入的特殊 country，表示"所有非 US"。
# worker 内部把它转成 None（不限制 country），由 task 自带的 region 决定账号选择。
_COUNTRY_OTHER_ALIAS = "other"
_START_STAGGER_ENV = "WORKER_START_STAGGER_SECONDS"
_START_STAGGER_JITTER_ENV = "WORKER_START_STAGGER_JITTER_SECONDS"
_WORKER_HEARTBEAT_ENV = "REVIEW_WORKER_HEARTBEAT_SECONDS"
_WORKER_RECOVERY_INTERVAL_ENV = "WORKER_RECOVERY_INTERVAL_SECONDS"
_SCHEDULER_BUSY_REQUEUE_MIN_ENV = "SCHEDULER_BUSY_REQUEUE_DELAY_MIN_SECONDS"
_SCHEDULER_BUSY_REQUEUE_MAX_ENV = "SCHEDULER_BUSY_REQUEUE_DELAY_MAX_SECONDS"
try:
    _MISSING_ACCOUNT_ALERT_TTL_SECONDS = max(60, int(os.getenv("MISSING_ACCOUNT_ALERT_TTL_SECONDS", "3600")))
except Exception:
    _MISSING_ACCOUNT_ALERT_TTL_SECONDS = 3600


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(f"[启动配置] {name}={raw!r} 非法，使用默认 {default}")
        return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        logger.warning(f"[启动配置] {name}={raw!r} 非法，使用默认 {default}")
        return default


def _component_part(value, default: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        text = default
    chars = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-", "."):
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars).strip("_") or default


def _review_worker_component(worker_mode: str, country: str = None, source: str = None) -> str:
    mode = _component_part(worker_mode, "single")
    if mode == "single":
        country_part = _component_part(country, "all")
        component = f"review_single_{country_part}"
    else:
        component = f"review_{mode}"

    source_part = _component_part(source, "")
    if source_part and source_part != "normal":
        component = f"{component}_{source_part}"
    return component[:64]


def _scheduler_busy_requeue_delay() -> int:
    min_delay = _env_int(_SCHEDULER_BUSY_REQUEUE_MIN_ENV, 30, minimum=1)
    max_delay = _env_int(_SCHEDULER_BUSY_REQUEUE_MAX_ENV, 90, minimum=1)
    if max_delay < min_delay:
        max_delay = min_delay
    return random.randint(min_delay, max_delay)


def _account_type_desc(account_manager, country: str) -> str:
    platform = getattr(account_manager, "platform", "amazon") or "amazon"
    label = getattr(account_manager, "account_label", "") or "normal"
    return f"platform={platform}, country={country or '-'}, label={label}"


def _is_account_type_missing(account_manager, country: str) -> bool:
    checker = getattr(account_manager, "has_configured_account", None)
    if not callable(checker):
        return False
    try:
        return not checker({"country": country})
    except Exception:
        logger.warning(f"[账号检查] 检查账号库是否存在账号失败 country={country}: {traceback.format_exc()}")
        return False


def _alert_missing_account_type(account_manager, *, country: str, asin: str,
                                task_id, worker_name: str, table_name: str) -> None:

    account_type = _account_type_desc(account_manager, country)
    dedupe_key = (
        "alert:missing_account_type:"
        + account_type.replace(" ", "").replace(",", ":").replace("=", "-").lower()
    )
    should_send = True
    try:
        #   告警做了 1 小时同类型去重，避免同一个国家没账号时刷屏。可用环境变量调：
        #   set MISSING_ACCOUNT_ALERT_TTL_SECONDS=3600
        from app.crawlers.amazon_crawler.shuler.util.redis_ import RedisDistLock
        should_send = bool(RedisDistLock().acquire(
            dedupe_key,
            timeout=max(60, _MISSING_ACCOUNT_ALERT_TTL_SECONDS),
        ))
    except Exception as exc:
        logger.warning(f"[账号缺失] 告警去重失败，将直接发送: {exc}")

    if not should_send:
        return

    try:
        send_custom_robot_group_message(
            (
                f"[账号缺失] 账号库没有该类型账号，任务已置为失败(status=3)\n"
                f"类型: {account_type}\n"
                f"表: {table_name}\n"
                f"task_id: {task_id}\n"
                f"ASIN: {asin}\n"
                f"worker: {worker_name}"
            ),
            at_mobiles=["17398238551"],
        )
    except Exception:
        logger.error(f"[账号缺失] 发送告警失败: {traceback.format_exc()}")


def _sleep_between_tasks(country: str = "", worker_name: str = "") -> None:
    """Apply BanAnalyzer throttling only between tasks, not inside page pagination."""
    if _check_stop_signal():
        return

    try:
        rate = get_global_rate_factor(site=country or "")
    except Exception:
        rate = 1.0

    if rate < 0.999:
        base_delay = random.uniform(5.0, 15.0)
        delay = min(60.0, base_delay * (1.0 / max(0.5, rate)))
        logger.info(
            f"[调速] worker={worker_name} country={country or '-'} "
            f"rate_factor={rate:.2f}，任务间隔等待 {delay:.1f}s"
        )
    else:
        delay = random.uniform(0.2, 1.0)

    time.sleep(delay)


def _local_worker_recovery_loop(stop_event: threading.Event, *, node_id: str, interval_seconds: int) -> None:
    """Recover stale workers for the local Windows browser node only."""
    logger.info(
        f"[WorkerRecovery] 本机恢复线程启动 node_id={node_id}, "
        f"stale_seconds={ACTIVE_WORKER_STALE_SECONDS}, interval={interval_seconds}s"
    )
    while not stop_event.is_set():
        try:
            recovered = recover_stale_workers(node_id=node_id, close_browser=True)
            if recovered:
                logger.warning(
                    f"[WorkerRecovery] 本机恢复 stale worker 数={recovered}, node_id={node_id}"
                )
        except Exception:
            logger.warning(f"[WorkerRecovery] 本机扫描异常: {traceback.format_exc()[:800]}")
        stop_event.wait(interval_seconds)


def _close_idle_review_session_if_due(
    *,
    idle_started_at,
    has_session,
    close_session_callback,
    release_session_callback,
    logger_prefix: str,
):
    """Close a review browser/account session after the worker has been idle long enough."""
    if not has_session():
        return None, False
    if idle_started_at is None:
        return time.time(), False

    idle_seconds = time.time() - idle_started_at
    if idle_seconds < REVIEW_IDLE_SESSION_CLOSE_SECONDS:
        return idle_started_at, False
    print(f"{logger_prefix} 连续无任务 {int(idle_seconds)}s，"
        f"关闭当前浏览器会话并释放账号 session")
    logger.info(
        f"{logger_prefix} 连续无任务 {int(idle_seconds)}s，"
        f"关闭当前浏览器会话并释放账号 session"
    )
    close_session_callback()
    try:
        release_session_callback()
    except Exception:
        logger.warning(f"{logger_prefix} 空闲释放账号 session 异常: {traceback.format_exc()[:500]}")
    return None, True


class SingleTaskDeadline:
    """Per-worker hard wall-clock timeout for the active single task."""

    def __init__(self, *, worker_name: str, timeout_seconds: int):
        self.worker_name = worker_name
        self.timeout_seconds = int(timeout_seconds or 0)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._deadline_at = 0.0
        self._task = {}
        self._account = None
        self._thread = None
        self._generation = 0

    def start(self):
        if self.timeout_seconds <= 0:
            return self
        self._thread = threading.Thread(
            target=self._watch,
            daemon=True,
            name=f"single-task-deadline-{self.worker_name}",
        )
        self._thread.start()
        return self

    def arm(self, *, task: Dict, account) -> None:
        if self.timeout_seconds <= 0:
            return
        with self._lock:
            self._generation += 1
            self._task = dict(task or {})
            self._account = account
            self._deadline_at = time.monotonic() + self.timeout_seconds

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._deadline_at = 0.0
            self._task = {}
            self._account = None

    def close(self) -> None:
        self.cancel()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _watch(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                deadline_at = self._deadline_at
                task = dict(self._task)
                account = self._account
                generation = self._generation

            if not deadline_at:
                self._stop.wait(1.0)
                continue

            remaining = deadline_at - time.monotonic()
            if remaining > 0:
                self._stop.wait(min(remaining, 1.0))
                continue

            with self._lock:
                if self._deadline_at != deadline_at or self._generation != generation:
                    continue
                self._deadline_at = 0.0

            if self._handle_timeout(task, account, generation=generation):
                return

    def _is_generation_current(self, generation: int) -> bool:
        with self._lock:
            return self._generation == generation

    def _active_task_state(self, db, *, row_id, task_id: str):
        if not task_id:
            return True, "task_id is empty"
        db.cursor.execute(
            """
            SELECT id, status, worker_name, updated_at
            FROM crawl_single_tasks
            WHERE task_id=%s
            """,
            (task_id,),
        )
        row = db.cursor.fetchone()
        if not row:
            return False, f"task not found task_id={task_id}"

        db_row_id = row.get("id")
        try:
            same_row = int(db_row_id) == int(row_id)
        except (TypeError, ValueError):
            same_row = str(db_row_id or "") == str(row_id or "")
        status = int(row.get("status") or 0)
        db_worker = str(row.get("worker_name") or "")
        if not same_row or status != 1 or db_worker != self.worker_name:
            return False, (
                f"task no longer belongs to current worker: task_id={task_id} row_id={row_id} "
                f"db_row_id={db_row_id} db_status={status} db_worker={db_worker!r} "
                f"db_updated_at={row.get('updated_at')}"
            )
        return True, (
            f"task still active: task_id={task_id} row_id={row_id} "
            f"db_status={status} db_worker={db_worker!r} db_updated_at={row.get('updated_at')}"
        )

    def _handle_timeout(self, task: Dict, account, *, generation: int) -> bool:
        row_id = task.get("id")
        task_id = task.get("task_id", "")
        asin = task.get("asin", "")
        country = task.get("country") or task.get("region", "")
        username = getattr(account, "username", "")
        platform = getattr(account, "platform", "amazon") or "amazon"
        fingerprint_id = getattr(account, "fingerprint_id", "")
        reason = (
            f"single task hard timeout {self.timeout_seconds}s "
            f"worker={self.worker_name} row_id={row_id} task_id={task_id} asin={asin}"
        )

        db = None
        alert_title = "任务硬超时"
        should_update_task = False
        db_state = ""
        try:
            db = MySQLTaskDB()
            still_active, db_state = self._active_task_state(db, row_id=row_id, task_id=task_id)
            try:
                if db.conn and db.conn.is_connected() and db.conn.in_transaction:
                    db.conn.rollback()
            except Exception:
                pass
            if not still_active:
                if not self._is_generation_current(generation):
                    logger.warning(
                        f"[任务硬超时] watchdog 已过期但 worker 已进入新状态，跳过 worker 退出 "
                        f"worker={self.worker_name} task_id={task_id} generation={generation} {db_state}"
                    )
                    return False
                alert_title = "Worker硬超时(stale task)"
                logger.error(
                    f"[{alert_title}] {reason}；DB 显示旧任务已不属于当前 worker，但 worker deadline generation "
                    f"仍未变化，判定当前 worker 卡在旧执行链路，即将退出。{db_state}"
                )
            else:
                should_update_task = True
                logger.error(f"[任务硬超时] {reason}，即将退出 worker 交由父进程重启")
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except Exception:
                pass
            if should_update_task and row_id:
                timeout_update = db.fail_or_retry_single_task_by_id(
                    row_id=int(row_id),
                    error_msg=f"任务执行超过硬超时 {self.timeout_seconds}s，worker 已重启",
                    only_running=True,
                )
                if timeout_update:
                    logger.warning(
                        f"[任务硬超时] 已更新重试计数 row_id={row_id} task_id={task_id} "
                        f"status={timeout_update.get('status')} "
                        f"retry={timeout_update.get('retry_count')}/{timeout_update.get('retry_times')}"
                    )
                elif should_update_task and task_id:
                    db.update_single_task_result(
                        task_id=task_id,
                        success=False,
                        result_count=0,
                        error_msg=f"任务执行超过硬超时 {self.timeout_seconds}s，worker 已重启",
                        result_data=None,
                        expected_row_id=row_id,
                        expected_worker_name=self.worker_name,
                    )
            if username:
                db.release_account_by_username(
                    username,
                    platform=platform,
                    note="single task hard timeout",
                )
        except Exception:
            logger.error(f"[任务硬超时] 回写任务/释放账号失败: {traceback.format_exc()}")
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass

        if fingerprint_id:
            try:
                from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import close_browser
                close_browser(fingerprint_id)
            except Exception as exc:
                logger.warning(f"[任务硬超时] 关闭指纹浏览器失败 fingerprint_id={fingerprint_id}: {exc}")

        try:
            send_custom_robot_group_message(
                f"[{alert_title}] worker={self.worker_name}, account={username}, country={country}, "
                f"asin={asin}, task_id={task_id}, timeout={self.timeout_seconds}s，worker 已重启",
                at_mobiles=["17398238551"],
            )
        except Exception:
            pass

        os._exit(124)
        return True


def _detach_debugger():
    """
    PyCharm Debug 模式下 pydev_monkey.py 会把 C 帧评估器重新注入 spawn 出的子进程，
    导致 BeautifulSoup 解析时 SIGSEGV。调用 stoptrace() 移除 C 帧评估器。
    副作用：子进程内断点失效，主进程不受影响。
    windows没有这个问题
    """
    try:
        import pydevd
        pydevd.stoptrace()
    except Exception:
        pass

def get_temp_review_table_name():
    """获取临时任务评论表名（按日期生成，如 crawler_reviews_20260409）"""
    from datetime import datetime
    date_str = datetime.now().strftime("%Y%m%d")
    return f"crawler_reviews_{date_str}"


def get_batch_review_table_name(batch_no: str):
    """按批次号生成评论表名，中文批次名用 hash 后缀避免安全化后撞表。"""
    batch = (batch_no or "").strip().lower()
    if not batch:
        return get_temp_review_table_name()
    safe = re.sub(r"[^a-z0-9_]+", "_", batch)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        safe = "batch"
    digest = hashlib.sha1(batch.encode("utf-8")).hexdigest()[:12]
    prefix = "crawler_reviews_"
    max_safe_len = 64 - len(prefix) - 1 - len(digest)
    safe = safe[:max_safe_len].strip("_") or "batch"
    return f"{prefix}{safe}_{digest}"


def _quote_table_name(table_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", table_name or ""):
        raise ValueError(f"非法表名: {table_name}")
    return f"`{table_name}`"


def _ensure_review_table_columns(mysql_db, table_name: str):
    """Add newly parsed review columns to existing review tables."""
    mysql_db.cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table_name,)
    )
    existing = {row["COLUMN_NAME"] for row in (mysql_db.cursor.fetchall() or [])}
    if not existing:
        return

    quoted_table = _quote_table_name(table_name)
    if "isReviewLocal" not in existing:
        mysql_db.cursor.execute(
            f"ALTER TABLE {quoted_table} ADD COLUMN `isReviewLocal` TINYINT NULL DEFAULT NULL AFTER `country`"
        )
        mysql_db.conn.commit()


def _normalize_optional_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return 1
        if normalized in {"0", "false", "no"}:
            return 0
        return None
    return 1 if value else 0


def create_temp_table(mysql_db, table_name: str = None):
    """创建评论表（默认当天表，也支持按批次指定表名）"""
    if not table_name:
        table_name = get_temp_review_table_name()
    create_sql = f"""
           CREATE TABLE IF NOT EXISTS {_quote_table_name(table_name)} (
               `id` BIGINT NOT NULL AUTO_INCREMENT,
               `task_id` BIGINT NULL,
               `review_url` VARCHAR(1024) DEFAULT '',
               `countryCode` VARCHAR(128) DEFAULT '',
               `country` VARCHAR(16) DEFAULT '',
               `isReviewLocal` TINYINT NULL DEFAULT NULL,
               `reviewDate` VARCHAR(64) DEFAULT '',
               `hasVideo` TINYINT DEFAULT 0,
               `reviewId` VARCHAR(128) DEFAULT '',
               `videos` JSON NULL,
               `reviewTitle` VARCHAR(1024) DEFAULT '',
               `asin` VARCHAR(32) DEFAULT '',
               `helpfulNum` VARCHAR(32) DEFAULT '0',
               `reviewerName` VARCHAR(256) DEFAULT '',
               `isVP` TINYINT DEFAULT 0,
               `isVineVoice` TINYINT DEFAULT 0,
               `images` JSON NULL,
               `rating` DECIMAL(4,2) NULL,
               `comment` LONGTEXT,
               `earlyReviewer` TINYINT DEFAULT 0,
               `reviewerId` VARCHAR(128) DEFAULT '',
               `dimension` JSON NULL,
               `create_time` DATETIME NULL,
               `flag` VARCHAR(32) DEFAULT '',
               PRIMARY KEY (`id`),
               UNIQUE KEY `uniq_review` (`reviewId`, `asin`, `country`),
               KEY `idx_asin_country` (`asin`, `country`),
               KEY `idx_task_id` (`task_id`)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
       """
    mysql_db.cursor.execute(create_sql)
    mysql_db.conn.commit()
    _ensure_review_table_columns(mysql_db, table_name)
    logger.info(f"临时评论表已创建/确认: {table_name}")
    return table_name


def _set_batch_running(mysql_db, batch_no: str, review_table_name: str):
    if not batch_no:
        return
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    mysql_db.cursor.execute(
        """
        UPDATE crawler_task_batches
        SET status=1,
            review_table_name=CASE WHEN review_table_name='' THEN %s ELSE review_table_name END,
            updated_at=%s
        WHERE batch_no=%s
        """,
        (review_table_name, now, batch_no)
    )
    mysql_db.conn.commit()


def _refresh_batch_status(mysql_db, batch_no: str):
    if not batch_no:
        return
    mysql_db.cursor.execute(
        """
        SELECT
            SUM(CASE WHEN status=0 THEN 1 ELSE 0 END) AS pending_cnt,
            SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) AS running_cnt,
            SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS success_cnt,
            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS failed_cnt
        FROM crawler_asin_tasks_temp
        WHERE batch_no=%s
        """,
        (batch_no,)
    )
    row = mysql_db.cursor.fetchone() or {}
    pending_cnt = int(row.get("pending_cnt") or 0)
    running_cnt = int(row.get("running_cnt") or 0)
    failed_cnt = int(row.get("failed_cnt") or 0)
    if pending_cnt == 0 and running_cnt == 0:
        status = 3 if failed_cnt > 0 else 2
    else:
        status = 1
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    mysql_db.cursor.execute(
        "UPDATE crawler_task_batches SET status=%s, updated_at=%s WHERE batch_no=%s",
        (status, now, batch_no)
    )
    mysql_db.conn.commit()
    return status

def _migrate_to_crawler_reviews(mysql_db, src_table: str, batch_no: str, mysql_task_db=None):
    """
    批次全部完成后将临时表数据迁移至 Shulex 库的正式表 crawler_reviews：
    1. 检查 crawler_reviews 所有行 flag=1，否则跳过
    2. 备份为 crawler_reviews_back_<YYYYMMDDHHmm>
    3. TRUNCATE crawler_reviews
    4. 按列名（排除 id）INSERT SELECT 从临时表导入
    """
    from datetime import datetime
    _ensure_review_table_columns(mysql_db, src_table)
    _ensure_review_table_columns(mysql_db, "crawler_reviews")

    # 1. 检查 flag
    mysql_db.cursor.execute(
        "SELECT COUNT(*) AS cnt FROM crawler_reviews WHERE flag != '1' OR flag IS NULL"
    )
    row = mysql_db.cursor.fetchone() or {}
    not_flagged = int(row.get("cnt") or 0)
    if not_flagged > 0:
        logger.warning(f"批次{batch_no}: crawler_reviews 存在 {not_flagged} 条 flag 非 1 的记录，跳过迁移")
        return

    # 2. 备份
    backup_name = "crawler_reviews_back_" + datetime.now().strftime("%Y%m%d%H%M")
    mysql_db.cursor.execute(f"CREATE TABLE `{backup_name}` LIKE crawler_reviews")
    mysql_db.cursor.execute(f"INSERT INTO `{backup_name}` SELECT * FROM crawler_reviews")
    mysql_db.conn.commit()
    logger.info(f"批次{batch_no}: crawler_reviews 已备份为 {backup_name}")

    # 3. 清空
    mysql_db.cursor.execute("TRUNCATE TABLE crawler_reviews")
    mysql_db.conn.commit()
    logger.info(f"批次{batch_no}: crawler_reviews 已清空")

    # 4. 动态获取临时表列名（排除 id），让正式表 id 自增
    mysql_db.cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME != 'id' "
        "ORDER BY ORDINAL_POSITION",
        (src_table,)
    )
    cols = [r["COLUMN_NAME"] for r in mysql_db.cursor.fetchall()]
    col_list = ", ".join(f"`{c}`" for c in cols)
    mysql_db.cursor.execute(
        f"INSERT INTO crawler_reviews ({col_list}) SELECT {col_list} FROM `{src_table}`"
    )
    mysql_db.conn.commit()
    logger.info(f"批次{batch_no}: 已将 {src_table} 数据导入 crawler_reviews")

    # 写回迁移状态
    if mysql_task_db is not None:
        try:
            mysql_task_db.cursor.execute(
                "UPDATE crawler_task_batches SET migrated=1, migrated_at=NOW() WHERE batch_no=%s",
                (batch_no,)
            )
            mysql_task_db.conn.commit()
            logger.info(f"批次{batch_no}: crawler_task_batches.migrated 已标记为 1")
        except Exception:
            logger.warning(f"批次{batch_no}: 更新 migrated 状态失败: {traceback.format_exc()}")

def _insert_reviews_to_mysql(mysql_db, reviews: list, asin: str, country: str, table_name: str = None):
    """批量插入评论数据到 MySQL 的指定评论表（默认使用当天日期的临时表）"""
    if not reviews:
        return
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 如果没有指定表名，使用当天日期的临时表
    if table_name is None:
        table_name = get_temp_review_table_name()
    _ensure_review_table_columns(mysql_db, table_name)

    # 准备批量插入的数据
    rows = []
    for r in reviews:
        review_asin = (r.get("asin") or asin or "").strip().upper()
        rows.append((
            r.get("task_id"),
            r.get("review_url", ""),
            r.get("countryCode", ""),
            country.upper(),
            _normalize_optional_bool(r.get("isReviewLocal")),
            r.get("reviewDate", r.get("date", "")),
            1 if r.get("hasVideo") else 0,
            r.get("reviewId", r.get("review_id", "")),
            json.dumps(r.get("videos", []), ensure_ascii=False),
            r.get("reviewTitle", r.get("title", "")),
            review_asin,
            r.get("helpfulNum", ""),
            r.get("reviewerName", r.get("author", "")),
            1 if r.get("isVP") or r.get("verified_purchase") else 0,
            1 if r.get("isVineVoice") else 0,
            json.dumps(r.get("images", []), ensure_ascii=False),
            r.get("rating", 0),
            r.get("comment", r.get("content", "")),
            1 if r.get("earlyReviewer") else 0,
            r.get("reviewerId", r.get("reviewerId", "")),
            json.dumps(r.get("dimension", {}), ensure_ascii=False),
            now,
            "",
        ))
    sql = f"""
        INSERT INTO {_quote_table_name(table_name)}
        (task_id, review_url, countryCode, country, isReviewLocal, reviewDate, hasVideo, reviewId, videos,
         reviewTitle, asin, helpfulNum, reviewerName, isVP, isVineVoice, images, rating,
         comment, earlyReviewer, reviewerId, dimension, create_time, flag)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, %s, %s, %s, %s,
                CAST(%s AS JSON), %s, %s, %s, %s, CAST(%s AS JSON), %s, %s)
        ON DUPLICATE KEY UPDATE
        review_url=VALUES(review_url), countryCode=VALUES(countryCode), country=VALUES(country),
        isReviewLocal=VALUES(isReviewLocal),
        reviewDate=VALUES(reviewDate), hasVideo=VALUES(hasVideo), videos=VALUES(videos),
        reviewTitle=VALUES(reviewTitle), helpfulNum=VALUES(helpfulNum), reviewerName=VALUES(reviewerName),
        isVP=VALUES(isVP), isVineVoice=VALUES(isVineVoice), images=VALUES(images),
        rating=VALUES(rating), comment=VALUES(comment), earlyReviewer=VALUES(earlyReviewer),
        dimension=VALUES(dimension), create_time=VALUES(create_time), flag=VALUES(flag)
    """
    mysql_db._check_connection()
    try:
        mysql_db.conn.start_transaction()
        mysql_db.cursor.executemany(sql, rows)
        mysql_db.conn.commit()
    except Exception as e:
        mysql_db.conn.rollback()
        logger.error(f"批量插入评论失败: {e}")
        raise


def execute_subtask(worker_id: int, subtask: Dict, mysql_db: MySQLTaskDB, account_manager: AccountManager):
    """执行单个子任务并回写数据库状态"""
    subtask_id = subtask["id"]
    asin = subtask["asin"]
    country = subtask["country"]
    task_unique_id = f"subtask_{subtask_id}_w{worker_id}_{int(time.time())}"
    account = None
    review = None
    success = False
    result_count = 0
    error_msg = ""

    try:
        account = account_manager.get_account({"country": country})
        if not account:
            raise Exception(f"无可用账号，country={country}")

        review = Reviews(account)
        # 解析前关闭 pymongo 连接，停止后台监控线程（防止 C 扩展线程导致 SIGSEGV）
        # account_manager.mongo_db.client.close()
        payload = {
            "task": {"id": subtask_id, "task_id": subtask["task_id"]},
            "country": country,
            "asin": asin,
            "max_pages": subtask.get("max_pages", 3),
            "query_conditions": subtask.get("query_conditions") or {},
            "task_type": subtask.get("task_type", "review"),
        }
        logger.info(f"进程{worker_id}-子任务{subtask_id} 开始执行: {asin}-{country}")
        product_reviews = review.get_reviews_main(payload)

        if product_reviews:
            db = MongoAccountDB()
            db.db["reviews"].insert_many(product_reviews)
            db.client.close()  # 用完即关，防止后台线程积累
            result_count = len(product_reviews)
        success = True
    except Exception as exc:
        error_msg = str(exc)
        traceback_text = traceback.format_exc()
        logger.error(f"[W{worker_id:02d}] 子任务{subtask_id} 执行失败: {traceback_text}")
        if '无账号可用' in error_msg:
            time.sleep(60 * 2)
            send_custom_robot_group_message(f'{error_msg}-{subtask}')


        if account and ("登录失败" in traceback_text or "可能被封" in traceback_text):
            account.state = -1
            account_manager._save_account(account)
            send_custom_robot_group_message(
                f"账号疑似异常: {account.username}, subtask={subtask_id}",
                at_mobiles=["17398238551"]
            )
    finally:
        try:
            mysql_db.update_subtask_result(
                subtask_id=subtask_id,
                success=success,
                result_count=result_count,
                error_msg=error_msg,
            )
        except Exception:
            logger.error(f"[W{worker_id:02d}] 子任务{subtask_id} 状态回写失败: {traceback.format_exc()}")

        try:
            if account:
                account_manager.release_account(account, asin, success, task_unique_id)
        except Exception:
            logger.error(f"[W{worker_id:02d}] 子任务{subtask_id} 释放账号失败")

        try:
            if review and getattr(review, 'use_local_browser', True):
                review.quit_local_dp()
            else:
                if review and review.page:
                    all_tabs = review.page.browser.get_tabs()
                    for tab in all_tabs:
                        tab.close()
                if review and review.chrome and review.account_info and review.account_info.fingerprint_id:
                    review.chrome.quit_fingerprint(review.account_info.fingerprint_id)
        except Exception:
            ...




def process_worker_single(worker_id: int, country: str = None, task_type: str = None,
                          source: str = None):
    """
    单任务消费者（Playwright + AccountScheduler 版）：从 crawl_single_tasks 拉取任务
    按 priority、need_crawler_time 排序，支持浏览器复用和账号切换

    :param source: 过滤 crawl_single_tasks.source。
                   传 'stress_test' 时自动启用压测模式（StressTestAccountManager，无调度限制，每日任务量自动递增）；
                   None 或其他值使用常规 HumanLikeAccountManager。
    """
    from app.crawlers.amazon_crawler.shuler.services.amazon.reviews_playwright import PlaywrightReviewScraper

    # _detach_debugger()
    faulthandler.enable(file=sys.stderr, all_threads=True)
    init_metrics(env=APP_ENV)   # spawn 子进程需要独立初始化
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger as _sl
    _sl("stress_worker" if (source or "").strip().lower() == "stress_test" else "single_worker", worker_id=worker_id)
    worker_name = f"single-worker-{_HOSTNAME}-{worker_id}-{os.getpid()}"
    recovery_tracker = WorkerRecoveryTracker(worker_name)

    is_stress = (source == STRESS_TEST_LABEL)
    if is_stress:
        logger.info(f"🚀 {worker_name} 启动（压测模式），region={country}")
        account_manager = StressTestAccountManager(worker_id=worker_name)
    else:
        logger.info(f"🚀 {worker_name} 启动（Playwright + AccountScheduler），region={country}, task_type={task_type}, source={source}")
        account_manager = AccountManager(worker_id=worker_name)

    mysql_db = MySQLTaskDB()
    worker_component = _review_worker_component("single", country, source)
    progress_component = f"{worker_component}_progress"[:64]

    scraper = None
    current_crawler = None
    current_username = None
    current_task_info = {}
    active_account = None
    task_deadline = SingleTaskDeadline(
        worker_name=worker_name,
        timeout_seconds=SINGLE_TASK_HARD_TIMEOUT_SECONDS,
    ).start()
    idle_started_at = None

    def _close_current_scraper():
        """安全关闭当前评论浏览器会话（PlaywrightReviewScraper 或 Reviews/AmazonBase）。"""
        nonlocal scraper, current_crawler, current_username
        target = current_crawler or scraper
        close_review_browser_target(target, worker_label=f"W{worker_id:02d}")
        if target is scraper:
            scraper = None
        current_crawler = None
        if scraper is None:
            current_username = None

    def _close_idle_session_if_due():
        """连续无任务时关闭当前浏览器会话，避免无头浏览器长期驻留。"""
        nonlocal idle_started_at, active_account, current_task_info
        idle_started_at, closed = _close_idle_review_session_if_due(
            idle_started_at=idle_started_at,
            has_session=lambda: bool(scraper or current_crawler or current_username),
            close_session_callback=_close_current_scraper,
            release_session_callback=account_manager.force_release,
            logger_prefix=f"[W{worker_id:02d}]",
        )
        if closed:
            active_account = None
            current_task_info = {}
            recovery_tracker.clear()

    def _record_idle_account_session(default_country: str = "") -> None:
        """Keep a recovery record while an account session is retained for reuse."""
        session_account = None
        try:
            if hasattr(account_manager, 'scheduler'):
                session_account = account_manager.scheduler.get_current_account(worker_name)
            else:
                session_account = getattr(account_manager, "_current_account", None)
        except Exception:
            session_account = None

        if session_account:
            recovery_tracker.register_session(
                task_kind="review_single_session",
                country=getattr(session_account, "country", "") or default_country,
                account=session_account,
            )
        else:
            recovery_tracker.clear()

    def _record_claim_progress(status: str, event: str, payload: str = "", row: Dict = None) -> None:
        row = row or {}
        message = (
            f"host={_HOSTNAME}, worker={worker_name}, event={event}, "
            f"payload={str(payload or '')[:80]}, row_id={row.get('id') or ''}, "
            f"task_id={row.get('task_id') or ''}, asin={row.get('asin') or ''}, "
            f"region={row.get('region') or country or ''}"
        )
        try:
            mysql_db.update_runtime_status(progress_component, status, message)
        except Exception:
            logger.debug(f"[WorkerProgress] 更新失败 component={progress_component}: {traceback.format_exc()[:500]}")

    def _on_worker_stop_signal(signum: int) -> None:
        target = current_crawler or scraper
        cleanup_review_worker_on_signal(
            worker_label=f"W{worker_id:02d}",
            signum=signum,
            mysql_db=mysql_db,
            account_manager=account_manager,
            task_info=current_task_info,
            browser_target=target,
            active_account=active_account,
            close_browser_callback=_close_current_scraper,
        )

    _install_stop_signal_handlers(
        logger_prefix=f"ReviewSingle-{worker_id}",
        on_signal=_on_worker_stop_signal,
    )

    try:
        while True:
            # 任务边界检查停止信号：当前批次任务结束后立即退出
            if _check_stop_signal():
                logger.info(f"[W{worker_id:02d}] 收到停止信号，退出主循环")
                break

            # ── 取任务：只从 Redis single ZSET；backfill 负责把 MySQL pending 补回 Redis ──
            # country == "other" → single_other 队列；country == "US" → US 队列；
            # country is None → 同时监听 US + other，避免无国家参数时空转。
            is_other = (str(country or "").strip().lower() == _COUNTRY_OTHER_ALIAS)
            redis_key = KEY_REVIEW_SINGLE_OTHER if is_other else (
                KEY_REVIEW_SINGLE_US if str(country).upper() == "US" else None
            )

            tasks = []
            claim_missed = False
            if country is None:
                popped = _redis_pop_single_task_from_keys(
                    [KEY_REVIEW_SINGLE_US, KEY_REVIEW_SINGLE_OTHER],
                    timeout_seconds=10,
                )
                queue_payload = popped[1] if popped else None
            elif redis_key:
                queue_payload = _redis_pop_single_task(redis_key, timeout_seconds=10)
            else:
                logger.error(f"[W{worker_id:02d}] 不支持的 single country={country!r}，请使用 US/other/None")
                time.sleep(EMPTY_SLEEP_SECONDS)
                continue

            if queue_payload:
                try:
                    row_identifier, asin_hint = parse_queue_payload(queue_payload)
                    # other/None 不限定 region；US 队列限定 region=US
                    claim_region = None if (is_other or country is None) else country
                    if row_identifier.isdigit():
                        row = mysql_db.claim_single_task_by_id(
                            row_id=int(row_identifier),
                            region=claim_region,
                            source=source,
                            worker_name=worker_name,
                        )
                    else:
                        # 兼容 Redis 中尚未消费的旧 task_id 队列值。
                        row = mysql_db.claim_single_task_by_task_id(
                            task_id=row_identifier,
                            region=claim_region,
                            source=source,
                            worker_name=worker_name,
                        )
                    if row:
                        _record_claim_progress("ok", "claim", payload=queue_payload, row=row)
                        tasks = [row]
                    else:
                        _record_claim_progress("degraded", "claim_miss", payload=queue_payload)
                        claim_missed = True
                        # 任务已被其他 worker 拿走 / 已超时 / 已完成（属正常竞争）
                        logger.debug(
                            f"[W{worker_id:02d}] Redis payload={queue_payload} asin={asin_hint} 但 MySQL 状态不符，跳过"
                        )
                except Exception:
                    _record_claim_progress("degraded", "claim_error", payload=queue_payload)
                    logger.error(f"[W{worker_id:02d}] claim_single_task 异常 payload={queue_payload}: {traceback.format_exc()}")
                    # 任务已 pop 出 Redis，但 MySQL 拉取失败 → backfill 兜底会重新入队
                    time.sleep(EMPTY_SLEEP_SECONDS)
                    continue

            if not tasks:
                _close_idle_session_if_due()
                if claim_missed:
                    continue
                time.sleep(EMPTY_SLEEP_SECONDS)
                continue
            idle_started_at = None

            for task in tasks:
                task_id = task["task_id"]
                row_id = task["id"]
                asin = task["asin"]
                region = task["region"]
                current_task_info = {
                    "table": "crawl_single_tasks",
                    "id": int(row_id),
                    "asin": asin,
                    "country": region,
                }
                success = False
                error_msg = ""
                asin_not_found = False
                account = None
                product_reviews = None
                use_curl = False
                skip_status_update = False  # 无账号时已手动退回队列，跳过 finally 里的 release_account

                try:
                    recovery_tracker.register(
                        table="crawl_single_tasks",
                        row_id=row_id,
                        task_kind="review_single",
                        asin=asin,
                        country=region,
                    )
                    task_deadline.arm(task=task, account=None)
                    try:
                        account = account_manager.get_account({"country": region})
                    except SchedulerLockTimeout as exc:
                        delay_seconds = _scheduler_busy_requeue_delay()
                        logger.warning(
                            f"[W{worker_id:02d}] 账号调度锁忙，任务退回等待重试 "
                            f"id={row_id} task_id={task_id} asin={asin} delay={delay_seconds}s: {exc}"
                        )
                        try:
                            update_result = mysql_db.update_single_task_result(
                                task_id=task_id,
                                success=False,
                                result_count=0,
                                error_msg=f"账号调度锁忙，退回队列 {delay_seconds}s 后重试",
                                result_data=None,
                                expected_row_id=row_id,
                                expected_worker_name=worker_name,
                            )
                            if (update_result or {}).get("skipped"):
                                logger.warning(
                                    f"[W{worker_id:02d}] 调度锁忙退回被跳过，不重复入队 task_id={task_id}: {update_result}"
                                )
                            else:
                                _redis_push_single_task(
                                    row_id=row_id,
                                    asin=asin,
                                    region=region,
                                    need_crawler_time=datetime.now() + timedelta(seconds=delay_seconds),
                                    priority=task.get("priority"),
                                )
                        except Exception:
                            logger.error(f"[W{worker_id:02d}] 调度锁忙任务退回失败: {traceback.format_exc()}")
                        skip_status_update = True
                        continue
                    if not account:
                        if _is_account_type_missing(account_manager, region):
                            error_msg = f"账号库无该类型账号: {_account_type_desc(account_manager, region)}"
                            logger.error(f"[W{worker_id:02d}] 任务{task_id} {error_msg}，直接失败")
                            mysql_db.update_single_task_result(
                                task_id=task_id,
                                success=False,
                                result_count=0,
                                error_msg=error_msg,
                                result_data=None,
                                force_final=True,
                                expected_row_id=row_id,
                                expected_worker_name=worker_name,
                            )
                            _alert_missing_account_type(
                                account_manager,
                                country=region,
                                asin=asin,
                                task_id=task_id,
                                worker_name=worker_name,
                                table_name="crawl_single_tasks",
                            )
                            skip_status_update = True
                            continue

                        logger.warning(f"[W{worker_id:02d}] 任务{task_id} 无可用账号，退回队列，等待20秒")
                        mysql_db.update_single_task_result(
                            task_id=task_id,
                            success=False,
                            result_count=0,
                            error_msg="无可用账号，退回队列",
                            result_data=None,
                            expected_row_id=row_id,
                            expected_worker_name=worker_name,
                        )
                        skip_status_update = True
                        time.sleep(20)
                        break
                    active_account = account
                    recovery_tracker.register(
                        table="crawl_single_tasks",
                        row_id=row_id,
                        task_kind="review_single",
                        asin=asin,
                        country=region,
                        account=account,
                    )
                    task_deadline.arm(task=task, account=account)

                    # 先判断当前账号走哪条路径，再决定是否需要 playwright scraper
                    use_curl = 'zone-custom-region-' in account.proxy_['http']

                    # 账号切换时：关旧浏览器，只在需要 playwright 路径时才开新浏览器
                    if account.username != current_username:
                        _close_current_scraper()
                        current_username = account.username
                        if not use_curl:
                            dummy_task = {"asin": "INIT", "country": region, "id": str(task_id)}
                            scraper = PlaywrightReviewScraper(account_info=account, task=dummy_task)
                            logger.info(f"进程{worker_name}: 新建浏览器会话 (账号={current_username})")
                        else:
                            scraper = None
                            logger.info(f"进程{worker_name}: 账号切换，curl_cffi 路径 (账号={current_username})")

                    # 构造任务 payload
                    params = task.get("params") or {}
                    if isinstance(params, str):
                        params = json.loads(params)

                    payload = {
                        "id": task["id"],
                        "task_id": task_id,
                        "country": region,
                        "asin": asin,
                        "max_pages": int(params.get("max_pages", CRAWLER_CONFIG["max_pages"])),
                        "query_conditions": params.get("query_conditions") or {},
                        "task_type": params.get("task_type", "review"),
                    }

                    logger.info(f"进程{worker_name}-任务{task_id}: 开始执行 ASIN={asin}, 账号={current_username},国家={region}")
                    print(f"{datetime.now()}-进程{worker_name}-任务{task_id}: 开始执行 ASIN={asin}, 账号={current_username},国家={region}")

                    if use_curl:
                        review = Reviews(account)
                        current_crawler = review
                        product_reviews = review.get_reviews_main(payload, worker_name, account_manager)
                        asin_not_found = getattr(review, '_asin_not_found', False)
                    else:
                        # scraper 此时一定非 None（账号未切换时沿用旧 scraper，切换时上面已新建）
                        current_crawler = scraper
                        product_reviews = scraper.run(
                            payload, worker_id=worker_name, account_manager=account_manager)
                        asin_not_found = getattr(scraper, '_asin_not_found', False)
                    result_count = 0
                    if product_reviews:
                        result_count = len(product_reviews)
                        success = True
                        # 回写结果到 MySQL
                        update_result = mysql_db.update_single_task_result(
                            task_id=task_id,
                            success=True,
                            result_count=result_count,
                            error_msg="",
                            result_data=product_reviews,
                            expected_row_id=row_id,
                            expected_worker_name=worker_name,
                        )
                        if (update_result or {}).get("skipped"):
                            logger.warning(
                                f"[W{worker_id:02d}] 任务{task_id} 成功结果回写被跳过: {update_result}"
                            )
                            skip_status_update = True
                            continue
                        _dispatch_single_task_callback_if_needed(
                            mysql_db=mysql_db,
                            task=task,
                            params=params,
                            task_id=task_id,
                            asin=asin,
                            region=region,
                            result_data=product_reviews,
                            result_count=result_count,
                            success=True,
                            error_msg="",
                        )
                        logger.info(f"进程{worker_name} | 已保存ASIN {asin} 的 {result_count} 条评论")
                    else:
                        success = not asin_not_found  # 无评论算成功，ASIN 不存在算失败终态
                        no_review_error = (
                            f"[ASIN_NOT_FOUND] ASIN无效: {asin}"
                            if asin_not_found
                            else f"[NO_REVIEWS] ASIN无评论: {asin}"
                        )
                        update_result = mysql_db.update_single_task_result(
                            task_id=task_id,
                            success=success,
                            result_count=0,
                            error_msg=no_review_error,
                            result_data=[],
                            force_final=asin_not_found,
                            expected_row_id=row_id,
                            expected_worker_name=worker_name,
                        )
                        if (update_result or {}).get("skipped"):
                            logger.warning(
                                f"[W{worker_id:02d}] 任务{task_id} 空结果回写被跳过: {update_result}"
                            )
                            skip_status_update = True
                            continue
                        _dispatch_single_task_callback_if_needed(
                            mysql_db=mysql_db,
                            task=task,
                            params=params,
                            task_id=task_id,
                            asin=asin,
                            region=region,
                            result_data=[],
                            result_count=0,
                            success=True,
                            error_msg=no_review_error,
                        )
                    if task_deadline:
                        task_deadline.cancel()
                    print(f"进程{worker_name} | 已保存ASIN {asin} 的 {result_count} 条评论")
                    # run() 内部可能已换号，同步 current_username（curl 路径无 scraper，跳过）
                    if scraper:
                        current_username = getattr(scraper.account_info, 'username', current_username)
                        # 若 run() 内部换号，同步 StressTestAccountManager 的 _current_account
                        if not hasattr(account_manager, 'scheduler'):
                            new_acc = scraper.account_info
                            if new_acc and getattr(new_acc, 'username', None) != getattr(account, 'username', None):
                                account_manager._current_account = new_acc

                except Exception as exc:
                    error_msg = str(exc)
                    traceback_text = traceback.format_exc()
                    logger.error(f"[W{worker_id:02d}] 任务{task_id} ASIN={asin} 执行失败: {traceback_text}")

                    # playwright 路径：run() 内部换号后仍失败 → 同步 scraper 状态
                    if scraper and not use_curl:
                        current_username = getattr(scraper.account_info, 'username', current_username)

                    if _check_stop_signal():
                        try:
                            mysql_db.reset_single_tasks_by_ids(
                                [row_id],
                                error_msg="review worker interrupted before single writeback",
                            )
                            logger.warning(f"[W{worker_id:02d}] 停止信号期间 single 任务未成功，已退回 id={row_id} task_id={task_id}")
                        except Exception:
                            logger.error(f"[W{worker_id:02d}] single 任务退回失败: {traceback.format_exc()}")
                        skip_status_update = True
                    elif not skip_status_update:
                        # 回写失败状态
                        try:
                            update_result = mysql_db.update_single_task_result(
                                task_id=task_id,
                                success=False,
                                result_count=0,
                                error_msg=error_msg,
                                result_data=None,
                                expected_row_id=row_id,
                                expected_worker_name=worker_name,
                            )
                            if int((update_result or {}).get("status") or 0) == 3:
                                callback_params = task.get("params") or {}
                                if isinstance(callback_params, str):
                                    try:
                                        callback_params = json.loads(callback_params)
                                    except Exception:
                                        callback_params = {}
                                _dispatch_single_task_callback_if_needed(
                                    mysql_db=mysql_db,
                                    task=task,
                                    params=callback_params,
                                    task_id=task_id,
                                    asin=asin,
                                    region=region,
                                    result_data=[],
                                    result_count=0,
                                    success=False,
                                    error_msg=error_msg,
                                )
                        except Exception:
                            logger.error(f"[W{worker_id:02d}] 任务{task_id} 状态回写失败: {traceback.format_exc()}")

                finally:
                    if task_deadline:
                        task_deadline.cancel()
                    # 释放账号
                    try:
                        if account:
                            # 计算页面数：平均每页 10 条评论，向上取整
                            review_count = len(product_reviews) if product_reviews else 0
                            pages_fetched = max(1, (review_count + 9) // 10)
                            # StressTestAccountManager 没有 scheduler 属性，兼容两种 manager
                            effective_account = (
                                account_manager.scheduler.get_current_account(worker_name) or account
                                if hasattr(account_manager, 'scheduler') else account
                            )
                            account_manager.release_account(
                                effective_account,
                                asin, success, f"task_{task_id}_{worker_id}_{int(time.time())}",
                                pages_fetched=pages_fetched)
                    except Exception:
                        ...
                    if use_curl:
                        current_crawler = None
                    active_account = None
                    current_task_info = {}
                    _record_idle_account_session(region)

                _sleep_between_tasks(country=region, worker_name=worker_name)

    except KeyboardInterrupt:
        logger.info(f"[W{worker_id:02d}] 手动中断")
    except Exception as exc:
        logger.error(f"[W{worker_id:02d}] 💥 异常退出: {traceback.format_exc()}")
        sys.exit(1)
    finally:
        _close_current_scraper()
        task_deadline.close()
        account_manager.force_release()
        recovery_tracker.close()
        try:
            mysql_db.close()
        except Exception:
            ...
        logger.info(f"[W{worker_id:02d}] 🔚 退出")


# ------------------------------------
# 队列模式（crawl_subtasks）
# ------------------------------------

def start_workers(
        country: str = None,
        task_type: str = None,
        worker_mode: str = "single",
        max_restart_per_worker: int = 2,
        workers: int = None,
        source: str = None,
        start_stagger_seconds: float = None,
        start_stagger_jitter_seconds: float = None,
):
    table_map = {
        "legacy": "asin_tasks",
        "queue": "crawl_subtasks",
        "temp": "crawler_asin_tasks_temp",
        "single": "crawl_single_tasks",
    }

    """启动多进程任务消费者（支持不同任务来源 + 崩溃自动拉起）"""
    TaskMonitor.show_status(table_map[worker_mode])
    multiprocessing.set_start_method('spawn', force=True)

    worker_targets = {
        # "legacy": process_worker_,       # asin_tasks 历史任务表
        "temp": process_worker_temp,     # crawler_asin_tasks_temp 临时表
        "single": process_worker_single, # crawl_single_tasks 单任务表
    }
    target = worker_targets.get(worker_mode)
    if not target:
        raise ValueError(f"unsupported worker_mode={worker_mode}, available={list(worker_targets.keys())}")

    n_workers = workers if workers is not None else MAX_PROCESSES
    restart_counter = {i: 0 for i in range(n_workers)}
    if start_stagger_seconds is None:
        start_stagger_seconds = _env_float(_START_STAGGER_ENV, 0.0)
    if start_stagger_jitter_seconds is None:
        start_stagger_jitter_seconds = _env_float(_START_STAGGER_JITTER_ENV, 0.0)
    start_stagger_seconds = max(0.0, float(start_stagger_seconds or 0.0))
    start_stagger_jitter_seconds = max(0.0, float(start_stagger_jitter_seconds or 0.0))
    worker_component = _review_worker_component(worker_mode, country, source)
    _configure_stop_signal_scope(worker_component)
    heartbeat_interval = _env_int(_WORKER_HEARTBEAT_ENV, 15, minimum=5)
    heartbeat_db = None
    last_heartbeat_at = 0.0

    def _close_heartbeat_db() -> None:
        nonlocal heartbeat_db
        if heartbeat_db is None:
            return
        try:
            heartbeat_db.close()
        except Exception:
            pass
        heartbeat_db = None

    def _heartbeat_status(status_override: str = None) -> None:
        nonlocal heartbeat_db, last_heartbeat_at
        alive = sum(1 for p in processes if p is not None and p.is_alive())
        status = status_override or ("ok" if alive > 0 else "critical")
        message = (
            f"host={_HOSTNAME}, pid={os.getpid()}, alive={alive}/{n_workers}, "
            f"mode={worker_mode}, country={country or 'all'}, source={source or 'normal'}, "
            f"stop_key={_get_stop_signal_key()}"
        )
        try:
            if heartbeat_db is None:
                heartbeat_db = MySQLTaskDB()
                heartbeat_db.ensure_monitoring_tables()
            heartbeat_db.update_runtime_status(worker_component, status, message)
            last_heartbeat_at = time.monotonic()
        except Exception:
            logger.warning(f"[WorkerHeartbeat] 更新失败 component={worker_component}: {traceback.format_exc()[:800]}")
            _close_heartbeat_db()

    def _maybe_heartbeat(force: bool = False, status_override: str = None) -> None:
        if force or time.monotonic() - last_heartbeat_at >= heartbeat_interval:
            _heartbeat_status(status_override=status_override)

    recovery_node_id = get_recovery_node_id()
    recovery_interval = _env_int(_WORKER_RECOVERY_INTERVAL_ENV, 15, minimum=5)
    recovery_stop = threading.Event()
    try:
        recovered = recover_stale_workers(node_id=recovery_node_id, close_browser=True)
        if recovered:
            logger.warning(
                f"[WorkerRecovery] 启动前已恢复 stale worker 数={recovered}, node_id={recovery_node_id}"
            )
    except Exception:
        logger.warning(f"[WorkerRecovery] 启动前扫描异常: {traceback.format_exc()[:800]}")

    def _sleep_after_worker_start(worker_id: int) -> None:
        delay = start_stagger_seconds
        if start_stagger_jitter_seconds > 0:
            delay += random.uniform(0.0, start_stagger_jitter_seconds)
        if delay <= 0:
            return
        logger.info(f"[启动错峰] worker_id={worker_id} 已启动，等待 {delay:.1f}s 后继续")
        time.sleep(delay)

    def _start_one(worker_id: int, *, apply_stagger: bool = True) -> Process:
        if worker_mode == "single":
            p = Process(
                target=target,
                args=(worker_id, country, task_type, source),
                name=f"{worker_mode}-worker-{worker_id}",
            )
        else:
            p = Process(
                target=target,
                args=(worker_id, country, task_type),
                name=f"{worker_mode}-worker-{worker_id}",
            )

        p.start()
        logger.info(f"启动进程 worker_id={worker_id} pid={p.pid} mode={worker_mode}")
        if apply_stagger:
            _sleep_after_worker_start(worker_id)
        return p

    processes = []
    for i in range(n_workers):
        processes.append(_start_one(i, apply_stagger=(i < n_workers - 1)))

    _all_dead_alerted = False

    def _monitor_all_dead():
        nonlocal _all_dead_alerted
        while True:
            time.sleep(60)
            alive = sum(1 for p in processes if p is not None and p.is_alive())
            if alive == 0 and not _all_dead_alerted:
                _all_dead_alerted = True
                try:
                    from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
                    send_custom_robot_group_message(
                        f"[Worker 全停告警] 所有 {n_workers} 个 worker 进程已停止 mode={worker_mode}，"
                        f"爬虫主进程即将退出，请及时检查并重启",
                        at_mobiles=["17398238551"],
                    )
                except Exception:
                    pass
                break

    import threading as _threading
    _threading.Thread(target=_monitor_all_dead, daemon=True, name="WorkerMonitor").start()
    recovery_thread = _threading.Thread(
        target=_local_worker_recovery_loop,
        args=(recovery_stop,),
        kwargs={"node_id": recovery_node_id, "interval_seconds": recovery_interval},
        daemon=True,
        name=f"LocalWorkerRecovery-{recovery_node_id}",
    )
    recovery_thread.start()

    logger.info(
        f"启动 {n_workers} 个 worker，mode={worker_mode}, source={source}, "
        f"component={worker_component}, node_id={recovery_node_id}, stop_key={_get_stop_signal_key()}, "
        f"start_stagger={start_stagger_seconds:.1f}s, start_jitter={start_stagger_jitter_seconds:.1f}s"
    )
    _maybe_heartbeat(force=True)
    _stop_signal_seen = False
    while any(p is not None for p in processes):
        # 检测到停止信号 → 不再重启子进程，等所有子进程在任务边界自然退出
        if not _stop_signal_seen and _check_stop_signal():
            _stop_signal_seen = True
            logger.warning("[Main] 父进程检测到停止信号，停止重启子进程，等待子进程自然退出")
            _maybe_heartbeat(force=True, status_override="stopping")

        _maybe_heartbeat(status_override="stopping" if _stop_signal_seen else None)

        for i, p in enumerate(processes):
            if p is None or p.is_alive():
                continue

            exitcode = p.exitcode
            # 收到停止信号后：子进程正常退出（exitcode=0）就标记为完成，不再重启
            if _stop_signal_seen:
                logger.info(f"worker_id={i} pid={p.pid} 退出（停止信号生效中），不重启 exitcode={exitcode}")
                processes[i] = None
                continue

            # worker 是无限循环，任何退出（包括 exitcode=0）都视为异常，需要重启
            if exitcode == 0:
                logger.warning(f"进程意外正常退出 worker_id={i} pid={p.pid}，将重启")
            elif exitcode is None:
                logger.warning(f"进程状态未知 worker_id={i} pid={p.pid} exitcode={exitcode}")
            elif exitcode < 0:
                logger.error(f"进程被信号终止 worker_id={i} pid={p.pid} signal={-exitcode} exitcode={exitcode}")
            else:
                logger.error(f"进程异常退出 worker_id={i} pid={p.pid} exitcode={exitcode}")

            if restart_counter[i] >= max_restart_per_worker:
                logger.error(
                    f"worker_id={i} 达到最大重启次数，停止拉起 mode={worker_mode} restart_count={restart_counter[i]}"
                )
                try:
                    from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
                    send_custom_robot_group_message(
                        f"[Worker 停止告警] worker_id={i} mode={worker_mode} "
                        f"连续崩溃重启 {restart_counter[i]} 次后已停止，请检查日志",
                        at_mobiles=["17398238551"],
                    )
                except Exception:
                    pass
                processes[i] = None
                continue

            restart_counter[i] += 1
            logger.warning(
                f"准备重启 worker_id={i} mode={worker_mode} restart_count={restart_counter[i]}/{max_restart_per_worker}"
            )
            time.sleep(2)
            processes[i] = _start_one(i)

        time.sleep(1)

    _maybe_heartbeat(force=True, status_override="stopped")
    recovery_stop.set()
    if recovery_thread.is_alive():
        recovery_thread.join(timeout=2)
    _close_heartbeat_db()
    TaskMonitor.show_status(table_map[worker_mode])
    logger.success("🎉 所有进程退出")

def pull_tasks(mysql_task_db, country=None):
    """
    拉取任务策略：
    1. 如果指定了 country，直接取该国家的任务（最多 limit 个）
    2. 如果没指定 country，先查哪个国家待处理任务最多，然后只取该国家的任务（最多 limit 个）
    """
    table_name = "asin_tasks"
    limit = TASK_BATCH_SIZE

    # 情况1：指定了国家，直接查该国家
    if country:
        target_country = country
    else:
        # 情况2：没指定国家，先找任务数最多的国家
        find_country_sql = """
            SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1) AS country,
                   COUNT(*) as cnt
            FROM asin_tasks
            WHERE status = '0'
              AND shulex_commodity_id LIKE 'AMAZON-%-%'
            GROUP BY country
            ORDER BY cnt DESC
            LIMIT 1
        """
        try:
            mysql_task_db.cursor.execute(find_country_sql)
        except OperationalError as e:
            # 2013: Lost connection to MySQL server during query
            if e.errno == 2013:
                logger.warning(f"[MySQL] 连接断开，正在重连...")
                mysql_task_db._check_connection()
                mysql_task_db.cursor.execute(find_country_sql)
            else:
                raise
        row = mysql_task_db.cursor.fetchone()
        if not row:
            return []
        target_country = row["country"]
        # 如果最多的国家数量都不够 limit，那就取实际数量（后面 SQL 的 LIMIT 处理）

    # 只查目标国家的任务，最多 limit 个，加行锁
    fields = [
        "SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1) AS country",
        "id",
        "shulex_commodity_id"
    ]
    cond_dict = {
        "status": "0",
        "SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1)": target_country
    }
    like_cond = {"shulex_commodity_id": "AMAZON-%-%"}

    try:
        tasks = mysql_task_db.pull_pending_tasks(
            table_name=table_name,
            fields=fields,
            cond_dict=cond_dict,
            like_cond=like_cond,
            group_by=None,  # 不需要分组，直接取单国家数据
            order="id ASC",  # 简单排序即可
            limit=limit,
            for_update=True
        )
    except OperationalError as e:
        # 2013: Lost connection to MySQL server during query
        if e.errno == 2013:
            logger.warning(f"[MySQL] 连接断开，正在重连...")
            mysql_task_db._check_connection()
            tasks = mysql_task_db.pull_pending_tasks(
                table_name=table_name,
                fields=fields,
                cond_dict=cond_dict,
                like_cond=like_cond,
                group_by=None,
                order="id ASC",
                limit=limit,
                for_update=True
            )
        else:
            raise
    return tasks

def process_worker_temp(worker_id: int, country: str = None, task_type: str = None):
    """
    临时任务工作进程（Playwright + AccountScheduler 版）：
    1. 循环拉取 crawler_asin_tasks_temp 任务
    2. 每次任务前调用 scheduler.acquire() 获取账号（会话粘性）
       - 同一 session 内返回同一个账号 → 复用浏览器
       - session_budget 用完 → 返回新账号 → close_session + 新建 scraper
    3. PlaywrightReviewScraper.run() 执行抓取
    4. scheduler.complete_task() 更新会话计数
    5. 结果存 MySQL + 更新 crawler_asin_tasks_temp 状态
    """
    from app.crawlers.amazon_crawler.shuler.services.amazon.reviews_playwright import PlaywrightReviewScraper

    # _detach_debugger()
    faulthandler.enable(file=sys.stderr, all_threads=True)
    init_metrics(env=APP_ENV)   # spawn 子进程需要独立初始化
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger as _sl
    _sl("temp_worker", worker_id=worker_id)
    worker_name = f"temp-worker-{_HOSTNAME}-{worker_id}-{os.getpid()}"
    recovery_tracker = WorkerRecoveryTracker(worker_name)
    logger.info(f"🚀 工作进程{worker_id}启动（Playwright + AccountScheduler），开始拉取任务")
    print(f"[W{worker_id:02d}] 🚀 启动 pid={os.getpid()}", flush=True)

    account_manager = AccountManager(worker_id=worker_name)
    mysql_task_db = MySQLTaskDB()  # 任务库：拉取任务、更新状态
    mysql_review_db = MySQLTaskDB(  # Shulex 库：仅用于写入评论数据
        host=MYSQL_HOST_SHULEX,
        port=MYSQL_PORT_SHULEX,
        user=MYSQL_USER_SHULEX,
        password=MYSQL_PASSWORD_SHULEX,
        database=MYSQL_DB_SHULEX,
    )

    scraper = None
    current_crawler = None
    current_username = None
    current_task_info = {}
    active_account = None
    idle_started_at = None

    def _close_current_scraper():
        """安全关闭当前评论浏览器会话（PlaywrightReviewScraper 或 Reviews/AmazonBase）。"""
        nonlocal scraper, current_crawler, current_username
        target = current_crawler or scraper
        close_review_browser_target(target, worker_label=f"W{worker_id:02d}")
        if target is scraper:
            scraper = None
        current_crawler = None
        if scraper is None:
            current_username = None

    def _close_idle_session_if_due():
        """连续无任务时关闭当前浏览器会话，避免无头浏览器长期驻留。"""
        nonlocal idle_started_at, active_account, current_task_info
        idle_started_at, closed = _close_idle_review_session_if_due(
            idle_started_at=idle_started_at,
            has_session=lambda: bool(scraper or current_crawler or current_username),
            close_session_callback=_close_current_scraper,
            release_session_callback=account_manager.force_release,
            logger_prefix=f"[W{worker_id:02d}] (temp)",
        )
        if closed:
            active_account = None
            current_task_info = {}

    def _on_worker_stop_signal(signum: int) -> None:
        target = current_crawler or scraper
        cleanup_review_worker_on_signal(
            worker_label=f"W{worker_id:02d}",
            signum=signum,
            mysql_db=mysql_task_db,
            account_manager=account_manager,
            task_info=current_task_info,
            browser_target=target,
            active_account=active_account,
            close_browser_callback=_close_current_scraper,
        )

    _install_stop_signal_handlers(
        logger_prefix=f"ReviewTemp-{worker_id}",
        on_signal=_on_worker_stop_signal,
    )

    try:
        batch_table_cache = {}

        while True:
            # 任务边界检查停止信号：当前批次任务结束后立即退出
            if _check_stop_signal():
                logger.warning(f"[W{worker_id:02d}] (temp) 收到停止信号，退出主循环")
                break

            # ── 取任务：只从 Redis BLPOP；backfill 负责把 MySQL pending 补回 Redis ──
            tasks = []
            queue_payload = _redis_pop_task(KEY_REVIEW_TEMP, timeout_seconds=10)
            if queue_payload:
                try:
                    row_identifier, asin_hint = parse_queue_payload(queue_payload)
                    if not row_identifier.isdigit():
                        logger.warning(
                            f"[W{worker_id:02d}] temp BLPOP payload={queue_payload} asin={asin_hint} 无法解析为 id，跳过"
                        )
                        row = None
                    else:
                        row = mysql_task_db.claim_temp_task_by_id(int(row_identifier))
                    if row:
                        tasks = [row]
                except Exception:
                    logger.error(f"[W{worker_id:02d}] claim_temp_task_by_id 异常 payload={queue_payload}: {traceback.format_exc()}")
                    time.sleep(EMPTY_SLEEP_SECONDS)
                    continue

            if not tasks:
                # logger.info(f"🛑 工作进程{worker_id}：无待执行任务")
                _close_idle_session_if_due()
                time.sleep(30)
                continue
            idle_started_at = None

            for task in tasks:
                task_id = task["id"]
                task_country = task['country']
                asin = task['asin']
                current_task_info = {
                    "table": "crawler_asin_tasks_temp",
                    "id": int(task_id),
                    "asin": asin,
                    "country": task_country,
                }
                batch_no = str(task.get('batch_no', '') or '').strip()

                if batch_no not in batch_table_cache:
                    review_table = create_temp_table(mysql_review_db, get_batch_review_table_name(batch_no))
                    batch_table_cache[batch_no] = review_table
                    _set_batch_running(mysql_task_db, batch_no, review_table)
                current_review_table = batch_table_cache.get(batch_no)

                if 'query_conditions' in task.keys() and task['query_conditions']:
                    task['query_conditions'] = json.loads(task['query_conditions'])

                success = False
                error_msg = ""
                asin_not_found = False
                account = None
                use_curl = False
                skip_status_update = False
                try:
                    recovery_tracker.register(
                        table="crawler_asin_tasks_temp",
                        row_id=task_id,
                        task_kind="review_temp",
                        asin=asin,
                        country=task_country,
                    )
                    account = account_manager.get_account({"country": task_country})
                    if not account:
                        if _is_account_type_missing(account_manager, task_country):
                            error_msg = f"账号库无该类型账号: {_account_type_desc(account_manager, task_country)}"
                            logger.error(f"[W{worker_id:02d}] 任务{task_id} {error_msg}，直接失败")
                            mysql_task_db.update_task_status(
                                task_id,
                                3,
                                error_msg,
                                table_name='crawler_asin_tasks_temp'
                            )
                            _refresh_batch_status(mysql_task_db, batch_no)
                            _alert_missing_account_type(
                                account_manager,
                                country=task_country,
                                asin=asin,
                                task_id=task_id,
                                worker_name=worker_name,
                                table_name="crawler_asin_tasks_temp",
                            )
                            skip_status_update = True
                            continue

                        logger.warning(f"[W{worker_id:02d}] 任务{task_id} 无可用账号，退回队列，等待120秒")
                        mysql_task_db.update_task_status(
                            task_id, 0,
                            f"无可用账号，退回队列",
                            table_name='crawler_asin_tasks_temp'
                        )
                        _refresh_batch_status(mysql_task_db, batch_no)
                        skip_status_update = True
                        time.sleep(60 * 2)
                        break
                    active_account = account
                    recovery_tracker.register(
                        table="crawler_asin_tasks_temp",
                        row_id=task_id,
                        task_kind="review_temp",
                        asin=asin,
                        country=task_country,
                        account=account,
                    )

                    # 先判断当前账号走哪条路径，再决定是否需要 playwright scraper
                    use_curl = 'zone-custom-region-' in account.proxy_['http']

                    # 账号切换时：关旧浏览器，只在需要 playwright 路径时才开新浏览器
                    if account.username != current_username:
                        _close_current_scraper()
                        current_username = account.username
                        if not use_curl:
                            dummy_task = {"asin": "INIT", "country": task_country, "id": str(task_id)}
                            scraper = PlaywrightReviewScraper(account_info=account, task=dummy_task)
                            logger.info(f"进程{worker_id}: 新建浏览器会话 (账号={current_username})")
                        else:
                            scraper = None
                            logger.info(f"进程{worker_id}: 账号切换，curl_cffi 路径 (账号={current_username})")

                    # 构造任务 payload
                    payload = {
                        "id": task_id,
                        "country": task_country,
                        "asin": asin,
                        "max_pages": task.get("max_pages", 10),
                        "query_conditions": task.get("query_conditions", {}),
                        "task_type": "review",
                    }
                    print(f"{datetime.now()}-进程{worker_name}-任务{task_id}: 开始执行 ASIN={asin}, 国家={task_country} ,账号={current_username}")
                    logger.info(f"进程{worker_name}-任务{task_id}: 开始执行 ASIN={asin}, 国家={task_country} ,账号={current_username}")

                    if use_curl:
                        review = Reviews(account)
                        current_crawler = review
                        product_reviews = review.get_reviews_main(payload, worker_name, account_manager)
                        asin_not_found = getattr(review, '_asin_not_found', False)
                    else:
                        # scraper 此时一定非 None（账号未切换时沿用旧 scraper，切换时上面已新建）
                        current_crawler = scraper
                        product_reviews = scraper.run(
                            payload, worker_id=worker_name, account_manager=account_manager)
                        asin_not_found = getattr(scraper, '_asin_not_found', False)

                    if product_reviews:
                        # 批量插入 MySQL 临时评论表（如 crawler_reviews_20260409）
                        _insert_reviews_to_mysql(mysql_review_db, product_reviews, asin, task_country, current_review_table)
                        logger.info(f"进程{worker_id} | 已保存ASIN {asin} 的 {len(product_reviews)} 条评论到 {current_review_table}")
                    success = True

                    # run() 内部可能已换号，同步 current_username（curl 路径无 scraper，跳过）
                    if scraper:
                        current_username = getattr(scraper.account_info, 'username', current_username)

                except Exception as exc:
                    error_msg = str(exc)
                    trace_text = traceback.format_exc()
                    logger.error(f"[W{worker_id:02d}] 任务{task_id} ASIN={asin} 执行失败: {trace_text}")

                    # playwright 路径：run() 内部换号后仍失败 → 同步 scraper 状态
                    if scraper and not use_curl:
                        current_username = getattr(scraper.account_info, 'username', current_username)

                finally:
                    if not skip_status_update:
                        # 释放账号
                        try:
                            if account:
                                # 计算页面数：平均每页 10 条评论，向上取整
                                review_count = len(product_reviews) if product_reviews else 0
                                pages_fetched = max(1, (review_count + 9) // 10)
                                account_manager.release_account(
                                    account_manager.scheduler.get_current_account(worker_name) or account,
                                    asin, success, f"task_{task_id}_{worker_id}_{int(time.time())}",
                                    pages_fetched=pages_fetched)
                        except Exception:
                            ...

                        # 更新任务状态
                        if _check_stop_signal() and not success:
                            mysql_task_db.reset_temp_tasks_by_ids(
                                [task_id],
                                note="review worker interrupted before temp writeback",
                            )
                            logger.warning(f"[W{worker_id:02d}] 停止信号期间 temp 任务未成功，已退回 id={task_id} asin={asin}")
                        elif success:
                            _success_msg = (f"[ASIN_NOT_FOUND] ASIN无效: {asin}"
                                            if asin_not_found
                                            else f"进程{worker_id}执行成功，ASIN:{asin}")
                            mysql_task_db.update_task_status(
                                task_id, 2,
                                _success_msg,
                                table_name='crawler_asin_tasks_temp'
                            )
                        else:
                            mysql_task_db.update_task_status(
                                task_id, 3,
                                f"进程{worker_id}执行失败，ASIN:{asin}，错误：{error_msg[:100]}",
                                table_name='crawler_asin_tasks_temp'
                            )
                        batch_status = _refresh_batch_status(mysql_task_db, batch_no)
                        if batch_status == 2 and current_review_table:
                            try:
                                _migrate_to_crawler_reviews(mysql_review_db, current_review_table, batch_no, mysql_task_db=mysql_task_db)
                            except Exception:
                                logger.error(f"[W{worker_id:02d}] 批次{batch_no} 迁移至 crawler_reviews 失败: {traceback.format_exc()}")
                    if use_curl:
                        current_crawler = None
                    active_account = None
                    current_task_info = {}
                    recovery_tracker.clear()
                    if account:
                        _sleep_between_tasks(country=task_country, worker_name=worker_name)

    except KeyboardInterrupt:
        logger.info(f"进程{worker_id}: 手动中断")
    except Exception as e:
        logger.error(f"[W{worker_id:02d}] 💥 异常退出: {traceback.format_exc()}")
        sys.exit(1)
    finally:
        _close_current_scraper()
        account_manager.force_release()
        recovery_tracker.close()
        try:
            mysql_task_db.close()
        except Exception:
            ...
        try:
            mysql_review_db.close()
        except Exception:
            ...
        logger.info(f"[W{worker_id:02d}] 🔚 退出")

# ------------------------------
# 主函数：程序入口
# ------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Amazon Crawler Worker")
    parser.add_argument("--mode", default="single",
                        choices=[ "temp", "single",],
                        help="Worker模式: legacy/temp/single (默认: single)")
    parser.add_argument("--country", default=None, help="筛选国家，如 US/JP/DE")
    parser.add_argument("--task_type", default=None, help="筛选任务类型")
    parser.add_argument("--workers", type=int, default=2,
                        help="启动的 worker 进程数（覆盖 amazon_config.MAX_PROCESSES）")
    parser.add_argument("--source", default=None,
                        help="single 任务来源：normal/空=普通生产任务（排除 stress_test）；stress_test=只消费压测任务；其他值=精确匹配 source")
    parser.add_argument("--start-stagger", type=float, default=0.5,
                        help="父进程每启动一个 worker 后等待的固定秒数，也可用 WORKER_START_STAGGER_SECONDS")
    parser.add_argument("--start-jitter", type=float, default=1.5,
                        help="在 --start-stagger 基础上追加 0~N 秒随机抖动，也可用 WORKER_START_STAGGER_JITTER_SECONDS")

    args = parser.parse_args()
    _stop_scope = _review_worker_component(args.mode, args.country, args.source)
    _configure_stop_signal_scope(_stop_scope)

    # 注册优雅停止信号处理器。收到 SIGTERM / Ctrl+C / Windows Ctrl+Break 后，
    # 只写当前 worker 组的 Redis stop_signal，避免 US/other/temp 互相停止。
    _install_stop_signal_handlers(logger_prefix=f"ReviewMain-{_stop_scope}")

    # 0. 按 source/mode 选择日志文件名，方便区分不同启动场景
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger as _setup_logger
    _source = (args.source or "").strip().lower()
    if _source == "stress_test":
        _log_name = "stress_worker"
    else:
        _log_name = f"{args.mode}_worker"
    _log_path = _setup_logger(_log_name)
    print(f"{'='*60}")
    print(f"  Amazon Crawler Worker")
    print(f"  模式: {args.mode}  |  进程数: {args.workers}  |  国家: {args.country or 'ALL'}")
    print(f"  停止信号: {_get_stop_signal_key()}")
    print(f"  日志文件: {_log_path}")
    print(f"  查看日志: tail -f \"{_log_path}\"")
    print(f"{'='*60}")

    #
    #   # 压测 worker
    #   python -m amazon_crawler.shuler.services.amazon.get_reviews_main \
    #       --mode single --country US --workers 10 --source stress_test

    # 1. 可选指标采集：默认关闭，避免启动 InfluxDB/ConsoleSink 后台线程。
    if init_metrics(env=APP_ENV):
        logger.info("[Main] MetricsCollector 已初始化")

    # 2. 检测 daemon_main 心跳，未检测到则发钉钉告警
    _daemon_ok = False
    try:
        import redis as _redis_lib
        from app.crawlers.amazon_crawler.shuler.util.config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB
        from app.crawlers.amazon_crawler.shuler.util.daemon_main import DAEMON_HEARTBEAT_KEY
        _rc = _redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        _daemon_ok = bool(_rc.exists(DAEMON_HEARTBEAT_KEY))
    except Exception:
        pass

    if not _daemon_ok:
        logger.warning(
            "[Main] ⚠️  未检测到 daemon_main 心跳！"
            "EventLogConsumer / BanAnalyzerDaemon 等守护进程可能未运行，"
            "请先执行: python -m amazon_crawler.shuler.util.daemon_main"
        )
        try:
            send_custom_robot_group_message(
                "[Worker 启动告警] 未检测到 daemon_main 心跳，"
                "EventLogConsumer / BanAnalyzerDaemon 等守护进程可能未运行，"
                "请先启动: python -m amazon_crawler.shuler.util.daemon_main",
                at_mobiles=["17398238551"],
            )
        except Exception:
            pass

    kwargs = {"worker_mode": args.mode}
    if args.country:
        kwargs["country"] = args.country
    if args.task_type:
        kwargs["task_type"] = args.task_type
    if args.workers is not None:
        kwargs["workers"] = args.workers
    if args.source:
        kwargs["source"] = args.source
    if args.start_stagger is not None:
        kwargs["start_stagger_seconds"] = args.start_stagger
    if args.start_jitter is not None:
        kwargs["start_stagger_jitter_seconds"] = args.start_jitter
    start_workers(**kwargs)
