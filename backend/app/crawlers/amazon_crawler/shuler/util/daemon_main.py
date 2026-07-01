"""
daemon_main — 爬虫基础设施守护进程入口

职责：
  独立启动并管理以下4个守护进程，与 worker 进程完全解耦：
    1. EventLogConsumer  — Redis 事件队列 → MySQL crawler_event_log
    2. BanAnalyzerDaemon — 封禁归因 + 全局自动调速
    3. DailyAggregator   — crawler_event_log → account_daily_summary（每小时）
    4. LongTermAnalyzer  — account_daily_summary → account_risk_profile（风险画像）

  同时：
    - 每 HEARTBEAT_INTERVAL 秒向 Redis 写一次心跳 key（带 TTL）
    - worker 启动时检测此心跳，未检测到则发钉钉告警
    - 每 30 秒采集 Redis 队列深度并写入 MySQL，供 Grafana 面板和告警使用

使用方法：
    python -m amazon_crawler.shuler.util.daemon_main

    # 可选参数
    python -m amazon_crawler.shuler.util.daemon_main --api-url http://127.0.0.1:8000
"""
import argparse
import os
import sys
import time
import threading
import traceback
from datetime import datetime
from multiprocessing import Process

from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)
from app.crawlers.amazon_crawler.shuler.util.config import APP_ENV
from app.crawlers.amazon_crawler.shuler.util.event_logger import EventLogConsumer
from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import BanAnalyzerDaemon
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
from app.crawlers.amazon_crawler.shuler.util.task_queue_backfill import TaskQueueBackfill
from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import (
    queue_due_lengths_snapshot,
    queue_lengths_snapshot,
    QUEUE_KEYS,
)
from app.crawlers.amazon_crawler.shuler.util.task_table_archive import archive_oversized_task_tables

# Redis 心跳 key，worker 启动时检测此 key
DAEMON_HEARTBEAT_KEY = "crawler:daemon:heartbeat"
HEARTBEAT_INTERVAL   = 10   # 每 10 秒续期一次
HEARTBEAT_TTL        = 30   # key TTL，超过 30s 无续期则视为 daemon 已停

QUEUE_DEPTH_INTERVAL_SECONDS = int(os.getenv("QUEUE_DEPTH_INTERVAL_SECONDS", "30"))
QUEUE_DEPTH_RETAIN_HOURS = int(os.getenv("QUEUE_DEPTH_RETAIN_HOURS", "72"))
QUEUE_DEPTH_ALERT_COOLDOWN_SECONDS = int(os.getenv("QUEUE_DEPTH_ALERT_COOLDOWN_SECONDS", "600"))
QUEUE_DEPTH_FAIL_ALERT_THRESHOLD = int(os.getenv("QUEUE_DEPTH_FAIL_ALERT_THRESHOLD", "3"))

# 默认按 100 万/天量级设置为偏保守阈值；线上可用环境变量按真实消费能力调小/调大。
QUEUE_DEPTH_WARN_TOTAL = int(os.getenv("QUEUE_DEPTH_WARN_TOTAL", "100000"))
QUEUE_DEPTH_CRITICAL_TOTAL = int(os.getenv("QUEUE_DEPTH_CRITICAL_TOTAL", "300000"))
QUEUE_DEPTH_WARN_REVIEW = int(os.getenv("QUEUE_DEPTH_WARN_REVIEW", "60000"))
QUEUE_DEPTH_WARN_ASIN = int(os.getenv("QUEUE_DEPTH_WARN_ASIN", "60000"))
TASK_TABLE_ARCHIVE_INTERVAL_SECONDS = int(os.getenv("TASK_TABLE_ARCHIVE_INTERVAL_SECONDS", "3600"))
WORKER_HEARTBEAT_STALE_SECONDS = int(os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", "120"))
WORKER_PROGRESS_STALE_SECONDS = int(os.getenv("WORKER_PROGRESS_STALE_SECONDS", "300"))
CALLBACK_RETRY_INTERVAL_SECONDS = int(os.getenv("CALLBACK_RETRY_INTERVAL_SECONDS", "60"))
CALLBACK_RETRY_MIN_INTERVAL_SECONDS = int(os.getenv("CALLBACK_RETRY_MIN_INTERVAL_SECONDS", "300"))
CALLBACK_RETRY_MAX_ATTEMPTS = int(os.getenv("CALLBACK_RETRY_MAX_ATTEMPTS", "5"))
CALLBACK_RETRY_BATCH_SIZE = int(os.getenv("CALLBACK_RETRY_BATCH_SIZE", "50"))
# worker 消费告警只用于发现真实积压。少量 due 任务可能是刚入队、竞争 pop 后的残留、
# 或 worker 心跳版本未统一时的短暂状态，默认不触发 worker 异常告警。
WORKER_QUEUE_ALERT_MIN_DEPTH = int(os.getenv("WORKER_QUEUE_ALERT_MIN_DEPTH", "50"))

QUEUE_WORKER_COMPONENTS = {
    "single_us": ("review_single_us", "review_single_all"),
    "single_other": ("review_single_other", "review_single_all"),
    "temp": ("review_temp",),
}
QUEUE_PROGRESS_COMPONENTS = {
    "single_us": ("review_single_us_progress", "review_single_all_progress"),
    "single_other": ("review_single_other_progress", "review_single_all_progress"),
}


def _make_redis():
    import redis
    return redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        username=REDIS_USERNAME, password=REDIS_PASSWORD,
        db=REDIS_DB, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3,
    )


def _heartbeat_loop(stop_event: threading.Event) -> None:
    """守护线程：每 HEARTBEAT_INTERVAL 秒刷新 Redis 心跳 key。"""
    rc = None
    while not stop_event.is_set():
        try:
            if rc is None:
                rc = _make_redis()
            rc.set(DAEMON_HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL)
        except Exception:
            logger.warning(f"[Heartbeat] Redis 写入失败，将重试: {traceback.format_exc()}")
            rc = None
        stop_event.wait(HEARTBEAT_INTERVAL)


def _setup_logger() -> None:
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
    setup_logger("daemon")


def _start_daemon(cls) -> Process:
    p = cls()
    p.start()
    logger.info(f"[DaemonMain] {cls.__name__} 已启动 pid={p.pid}")
    return p


def _watch_daemons(daemons: dict, stop_event: threading.Event) -> None:
    """
    监控线程：每 30 秒检查所有守护进程是否存活，
    意外退出则发钉钉告警并尝试重启（最多 3 次）。
    """
    restart_counts = {name: 0 for name in daemons}
    MAX_RESTARTS = 3

    while not stop_event.is_set():
        stop_event.wait(30)
        for name, p in list(daemons.items()):
            if p is None or p.is_alive():
                continue
            restart_counts[name] += 1
            if restart_counts[name] > MAX_RESTARTS:
                logger.error(f"[DaemonMain] {name} 反复崩溃，已停止重启")
                daemons[name] = None
                continue
            logger.warning(
                f"[DaemonMain] {name} 意外退出(exitcode={p.exitcode})，"
                f"第 {restart_counts[name]}/{MAX_RESTARTS} 次重启"
            )
            try:
                send_custom_robot_group_message(
                    f"[守护进程告警] {name} 意外退出，正在第 {restart_counts[name]} 次重启",
                    at_mobiles=["17398238551"],
                )
            except Exception:
                pass
            try:
                new_p = type(p)()
                new_p.start()
                daemons[name] = new_p
                logger.info(f"[DaemonMain] {name} 重启成功 pid={new_p.pid}")
            except Exception:
                logger.error(f"[DaemonMain] {name} 重启失败: {traceback.format_exc()}")


def _runtime_row_age_seconds(row: dict) -> float:
    updated_at = row.get("updated_at") if row else None
    if not updated_at:
        return float("inf")
    if isinstance(updated_at, datetime):
        return max(0.0, (datetime.now() - updated_at).total_seconds())
    try:
        parsed = datetime.fromisoformat(str(updated_at))
        return max(0.0, (datetime.now() - parsed).total_seconds())
    except Exception:
        return float("inf")


def _queue_worker_liveness_issues(db, valid_lengths: dict, due_lengths: dict) -> list:
    """队列有积压时，检查对应 worker 父进程心跳是否存在且新鲜。"""
    components = sorted(
        {c for items in QUEUE_WORKER_COMPONENTS.values() for c in items}
        | {c for items in QUEUE_PROGRESS_COMPONENTS.values() for c in items}
    )
    try:
        statuses = db.get_runtime_statuses(components)
    except Exception:
        logger.warning(f"[WorkerLiveness] 读取运行时状态失败: {traceback.format_exc()[:800]}")
        return []

    issues = []
    for queue_name, candidates in QUEUE_WORKER_COMPONENTS.items():
        depth = int(due_lengths.get(queue_name, valid_lengths.get(queue_name, 0)) or 0)
        if depth < WORKER_QUEUE_ALERT_MIN_DEPTH:
            continue

        healthy = False
        details = []
        for component in candidates:
            row = statuses.get(component)
            if not row:
                details.append(f"{component}=missing")
                continue
            age = _runtime_row_age_seconds(row)
            status = str(row.get("status") or "")
            details.append(f"{component}={status},age={age:.0f}s")
            if status == "ok" and age <= WORKER_HEARTBEAT_STALE_SECONDS:
                healthy = True
                break

        if not healthy:
            issues.append(f"{queue_name} due_depth={depth} worker心跳异常({'; '.join(details)})")
            continue

        progress_candidates = QUEUE_PROGRESS_COMPONENTS.get(queue_name) or ()
        if not progress_candidates:
            continue
        progress_ok = False
        progress_details = []
        for component in progress_candidates:
            row = statuses.get(component)
            if not row:
                progress_details.append(f"{component}=missing")
                continue
            age = _runtime_row_age_seconds(row)
            status = str(row.get("status") or "")
            message = str(row.get("message") or "")[:160]
            progress_details.append(f"{component}={status},age={age:.0f}s,msg={message}")
            if status == "ok" and age <= WORKER_PROGRESS_STALE_SECONDS:
                progress_ok = True
                break
        if not progress_ok:
            issues.append(
                f"{queue_name} due_depth={depth} 最近无成功claim"
                f"({'; '.join(progress_details)})"
            )
    return issues


def _queue_depth_reporter(stop_event: threading.Event) -> None:
    """每 30 秒写一次 Redis 队列深度到 MySQL，供 Grafana 和告警使用。"""
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB

    db = None
    cleanup_tick = 0
    last_alert_ts = 0.0
    depth_fail_count = 0

    def _maybe_alert(message: str) -> None:
        nonlocal last_alert_ts
        now = time.time()
        if now - last_alert_ts < QUEUE_DEPTH_ALERT_COOLDOWN_SECONDS:
            return
        last_alert_ts = now
        try:
            send_custom_robot_group_message(message, at_mobiles=["17398238551"])
        except Exception:
            logger.warning(f"[QueueDepth] 告警发送失败: {traceback.format_exc()}")

    def _ensure_db():
        nonlocal db
        if db is None:
            db = MySQLTaskDB()
            db.ensure_monitoring_tables()
        return db

    while not stop_event.is_set():
        try:
            snapshot = queue_lengths_snapshot()
            due_snapshot = queue_due_lengths_snapshot()

            current_db = _ensure_db()
            current_db.record_queue_depth_snapshot(snapshot, QUEUE_KEYS)

            valid_lengths = {k: v for k, v in snapshot.items() if v >= 0}
            if len(valid_lengths) != len(snapshot):
                depth_fail_count += 1
                current_db.update_runtime_status(
                    "queue_depth_reporter",
                    "degraded",
                    f"Redis 队列深度采集失败({depth_fail_count}/{QUEUE_DEPTH_FAIL_ALERT_THRESHOLD}): {snapshot}",
                )
                current_db.update_runtime_status("daemon_main", "ok", "heartbeat")
                if depth_fail_count >= QUEUE_DEPTH_FAIL_ALERT_THRESHOLD:
                    _maybe_alert(
                        f"[队列监控告警] Redis 队列深度连续采集失败 "
                        f"{depth_fail_count} 次: {snapshot}"
                    )
            else:
                depth_fail_count = 0
                total_depth = sum(valid_lengths.values())
                status = "ok"
                valid_due_lengths = {k: v for k, v in due_snapshot.items() if v >= 0}
                message = f"total={total_depth}, snapshot={snapshot}, due={due_snapshot}"
                worker_issues = _queue_worker_liveness_issues(current_db, valid_lengths, valid_due_lengths)

                if worker_issues:
                    status = "critical"
                    issue_text = " | ".join(worker_issues)
                    message = f"{message}, worker_issues={issue_text}"
                    _maybe_alert(
                        f"[Worker消费告警] Redis 队列有积压但对应 worker 心跳异常: "
                        f"{issue_text}; snapshot={snapshot}"
                    )
                elif total_depth >= QUEUE_DEPTH_CRITICAL_TOTAL:
                    status = "critical"
                    _maybe_alert(
                        f"[队列积压严重] Redis 总队列深度={total_depth}，"
                        f"阈值={QUEUE_DEPTH_CRITICAL_TOTAL}，snapshot={snapshot}"
                    )
                elif (
                    total_depth >= QUEUE_DEPTH_WARN_TOTAL
                    or valid_lengths.get("single_us", 0) + valid_lengths.get("single_other", 0) >= QUEUE_DEPTH_WARN_REVIEW
                    or valid_lengths.get("asin", 0) >= QUEUE_DEPTH_WARN_ASIN
                ):
                    status = "warning"
                    _maybe_alert(
                        f"[队列积压告警] Redis 总队列深度={total_depth}，"
                        f"snapshot={snapshot}"
                    )

                current_db.update_runtime_status("queue_depth_reporter", status, message)
                current_db.update_runtime_status("daemon_main", "ok", "heartbeat")

            cleanup_tick += 1
            if cleanup_tick >= 120:
                cleanup_tick = 0
                deleted = current_db.cleanup_queue_depth_snapshots(QUEUE_DEPTH_RETAIN_HOURS)
                if deleted:
                    logger.info(f"[QueueDepth] 清理旧采样 {deleted} 条")
        except Exception:
            logger.warning(f"[QueueDepth] 采集失败: {traceback.format_exc()}")
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass
            db = None
        stop_event.wait(QUEUE_DEPTH_INTERVAL_SECONDS)


def _task_table_archive_loop(stop_event: threading.Event) -> None:
    """定期轮转过大的任务表，避免单表长期膨胀。"""
    while not stop_event.is_set():
        try:
            archived = archive_oversized_task_tables()
            for item in archived:
                logger.warning(
                    f"[TaskTableArchive] table={item['table']} rows={item['rows']} "
                    f"archived={item['archived_rows']} deleted={item['deleted_rows']} "
                    f"active={item['active_rows']} backup={item['backup_table']}"
                )
        except Exception:
            logger.warning(f"[TaskTableArchive] 扫描异常: {traceback.format_exc()[:800]}")
        stop_event.wait(TASK_TABLE_ARCHIVE_INTERVAL_SECONDS)


def _callback_retry_loop(stop_event: threading.Event) -> None:
    """补发 single 任务 callback。OSS 地址和 callback 状态保存在 crawl_single_tasks。"""
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
    from app.crawlers.amazon_crawler.shuler.util.oss_callback import dispatch_existing_single_task_callback

    db = None

    def _ensure_db():
        nonlocal db
        if db is None:
            db = MySQLTaskDB()
            db.ensure_single_task_callback_columns()
        return db

    while not stop_event.is_set():
        try:
            current_db = _ensure_db()
            rows = current_db.list_retryable_single_callbacks(
                limit=CALLBACK_RETRY_BATCH_SIZE,
                max_attempts=CALLBACK_RETRY_MAX_ATTEMPTS,
                min_retry_interval_seconds=CALLBACK_RETRY_MIN_INTERVAL_SECONDS,
            )
            if rows:
                logger.info(f"[CallbackRetry] 待补发 callback 数量={len(rows)}")
            success_count = 0
            failed_count = 0
            for row in rows:
                task_id = str(row.get("task_id") or "")
                try:
                    callback_result = dispatch_existing_single_task_callback(row)
                    current_db.update_single_task_callback_state(
                        task_id=task_id,
                        callback_url=row.get("callback_url") or "",
                        oss_object_key=callback_result.get("oss_object_key", row.get("oss_object_key") or ""),
                        oss_result_url=callback_result.get("oss_result_url", row.get("oss_result_url") or ""),
                        snapshot_object_key=callback_result.get("snapshot_object_key", row.get("snapshot_object_key") or ""),
                        snapshot_url=callback_result.get("snapshot_url", row.get("snapshot_url") or ""),
                        snapshot_html=callback_result.get("snapshot_html", row.get("snapshot_html") or ""),
                        callback_status=callback_result.get("callback_status", 2),
                        callback_last_error=callback_result.get("callback_error", ""),
                        increment_attempts=True,
                    )
                    if int(callback_result.get("callback_status") or 0) == 1:
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception:
                    failed_count += 1
                    logger.warning(f"[CallbackRetry] 补发异常 task_id={task_id}: {traceback.format_exc()[:800]}")
                    try:
                        current_db.update_single_task_callback_state(
                            task_id=task_id,
                            callback_status=2,
                            callback_last_error=traceback.format_exc()[:1000],
                            increment_attempts=True,
                        )
                    except Exception:
                        pass
            if rows:
                logger.info(
                    f"[CallbackRetry] 本轮处理完成 processed={len(rows)}, "
                    f"success={success_count}, failed={failed_count}"
                )
                current_db.update_runtime_status(
                    "callback_retry",
                    "ok" if failed_count == 0 else "degraded",
                    f"processed={len(rows)}, success={success_count}, failed={failed_count}",
                )
        except Exception:
            logger.warning(f"[CallbackRetry] 扫描失败: {traceback.format_exc()[:800]}")
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass
            db = None
        stop_event.wait(CALLBACK_RETRY_INTERVAL_SECONDS)


def main() -> None:
    _setup_logger()

    parser = argparse.ArgumentParser(description="爬虫基础设施守护进程")
    parser.add_argument("--api-url", default="https://crawler.shulex.com",
                        help="task_api 地址，供 ApiWatchdog 使用（默认 https://crawler.shulex.com）")
    args = parser.parse_args()

    logger.info("[DaemonMain] 启动，env={}", APP_ENV)

    # 1. 启动心跳线程
    _stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(_stop,),
                     daemon=True, name="DaemonHeartbeat").start()
    logger.info(f"[DaemonMain] 心跳线程已启动 key={DAEMON_HEARTBEAT_KEY} TTL={HEARTBEAT_TTL}s")

    # 3. 启动 API 心跳监控线程（API 挂了发钉钉）
    from app.crawlers.amazon_crawler.shuler.services.amazon.task_watchdog import ApiWatchdog
    _api_watchdog = ApiWatchdog(
        base_url=args.api_url.strip(),
        interval_seconds=15,
        fail_threshold=3,
        alert_cooldown_seconds=300,
    )
    threading.Thread(target=_api_watchdog.run_forever,
                     daemon=True, name="ApiWatchdogThread").start()
    logger.info(f"[DaemonMain] ApiWatchdog 已启动 target={args.api_url}")

    # 4. 启动守护子进程
    from app.crawlers.amazon_crawler.shuler.util.daily_aggregator import DailyAggregator
    from app.crawlers.amazon_crawler.shuler.util.long_term_analyzer import LongTermAnalyzer

    daemons = {
        "EventLogConsumer": _start_daemon(EventLogConsumer),
        "BanAnalyzerDaemon": _start_daemon(BanAnalyzerDaemon),
        "DailyAggregator": _start_daemon(DailyAggregator),
        "LongTermAnalyzer": _start_daemon(LongTermAnalyzer),
        "TaskQueueBackfill": _start_daemon(TaskQueueBackfill),
    }

    # 5. 启动守护进程看护线程
    threading.Thread(target=_watch_daemons, args=(daemons, _stop),
                     daemon=True, name="DaemonWatcher").start()
    logger.info("[DaemonMain] 所有守护进程已启动，进入主循环")

    # 6. 启动队列深度采集线程（写 MySQL，Grafana 直接查 crawler_queue_depth_snapshot）
    threading.Thread(target=_queue_depth_reporter, args=(_stop,),
                     daemon=True, name="QueueDepthReporter").start()
    threading.Thread(target=_task_table_archive_loop, args=(_stop,),
                     daemon=True, name="TaskTableArchive").start()
    logger.info("[DaemonMain] TaskTableArchive 已启动")
    threading.Thread(target=_callback_retry_loop, args=(_stop,),
                     daemon=True, name="CallbackRetry").start()
    logger.info("[DaemonMain] CallbackRetry 已启动")

    # 主进程保持运行，Ctrl+C 优雅退出
    try:
        while True:
            time.sleep(60)
            alive = [n for n, p in daemons.items() if p and p.is_alive()]
            logger.info(f"[DaemonMain] 存活守护进程: {alive}")
    except KeyboardInterrupt:
        logger.info("[DaemonMain] 收到中断信号，退出")
        _stop.set()


if __name__ == "__main__":
    main()
