"""
TaskQueueBackfill — Redis 队列 ↔ MySQL 任务表最终一致性兜底守护进程。

每 60 秒 tick 一次，对 3 种任务源分别做：

(A) 超时处理
    - single: 按 need_crawler_time 的业务硬 SLA 暂时关闭；callback 任务需要允许较长时间完成
    - single: status=1 AND updated_at < NOW() - 4 MINUTE
              → status=0（worker 卡死兜底，重新入队重试）
    - temp:   status=1 AND update_time < NOW() - 30 MINUTE
              → status=0（worker 死亡兜底，重新入队重试）
    - asin:   status=1 AND updated_at < NOW() - 5 MINUTE
              → status=0（curl 任务超时即重试，换 IP）

(B) Redis 队列补齐
    - 查 MySQL status=0 的任务
    - 对比对应 Redis 队列里已有的 id / 旧 task_id
    - single 缺失的按 id:asin 写入 ZSET，score=priority + need_crawler_time，同时写 priority hash
    - temp/asin 缺失的按 id:asin RPUSH 补回（防止 Redis 重启 / 异常丢数据）

注册方式：daemon_main.py 启动一个子进程跑 TaskQueueBackfill。
"""
import os
import time
import traceback
from datetime import datetime
from multiprocessing import Process

from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import (
    KEY_ASIN,
    KEY_REVIEW_SINGLE_OTHER,
    KEY_REVIEW_SINGLE_US,
    KEY_REVIEW_TEMP,
    list_queue_members,
    missing_single_payloads,
    make_queue_payload,
    push_single_to_key,
    push_to_key,
    queue_length,
    queue_payload_identities,
    queue_lengths_snapshot,
    remove_single_payloads,
)

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


TICK_INTERVAL_SECONDS = 60

# single/temp/asin 这里的超时阈值仅用于检测 worker 死亡或卡死
SINGLE_RUNNING_STUCK_MINUTES = _env_int("SINGLE_RUNNING_STUCK_MINUTES", 4)
TEMP_STUCK_MINUTES = 10
ASIN_STUCK_MINUTES = 5

# 一次性扫描的最大 task 数量（防止内存爆炸）
MAX_SCAN_ROWS = 20000

# 异常单轮超时数量阈值，超过发钉钉
ALERT_TIMEOUT_THRESHOLD = 50

# callback 模式下暂不按 need_crawler_time + N 分钟把 single 任务置失败。
# 后续如需要恢复业务 SLA，把这个值改为 true，并恢复 claim/refill 的窗口过滤。
SINGLE_NEED_TIME_SLA_ENABLED = False


class TaskQueueBackfill(Process):
    """守护子进程：定期对账 Redis 队列与 MySQL 任务表。"""

    daemon = False  # 由 daemon_main 显式管理，不用 Python daemon 模式

    def __init__(self):
        super().__init__(name="TaskQueueBackfill")

    def run(self) -> None:
        from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
        setup_logger("backfill")
        logger.info(f"[Backfill] 启动 tick_interval={TICK_INTERVAL_SECONDS}s")

        # 每个子进程独立连接（mysql.connector 非线程/进程安全）
        db = MySQLTaskDB()

        while True:
            try:
                self._tick(db)
            except Exception:
                logger.error(f"[Backfill] tick 异常: {traceback.format_exc()}")
                # MySQL 连接可能挂了，重建
                try:
                    db.close()
                except Exception:
                    pass
                db = MySQLTaskDB()
            time.sleep(TICK_INTERVAL_SECONDS)

    def _tick(self, db: MySQLTaskDB) -> None:
        # 1. single：超时设为 status=3（业务硬 SLA）
        self._timeout_single_to_failed(db)

        # 2. single：执行中卡死 status=1→0（下一步 refill 会补回 Redis）
        self._timeout_to_retry(
            db,
            table="crawl_single_tasks",
            time_field="updated_at",
            stuck_minutes=SINGLE_RUNNING_STUCK_MINUTES,
            label="single",
        )

        # 3. temp：超时 status=1→0（重试）
        self._timeout_to_retry(
            db,
            table="crawler_asin_tasks_temp",
            time_field="update_time",
            stuck_minutes=TEMP_STUCK_MINUTES,
            label="temp",
        )

        # 4. asin：超时 status=1→0（重试）
        self._timeout_to_retry(
            db,
            table="crawl_asin_detail_tasks",
            time_field="updated_at",
            stuck_minutes=ASIN_STUCK_MINUTES,
            label="asin",
        )

        # 5. Redis 队列补齐
        if not getattr(db, "supports_legacy_mysql_tables", True):
            snapshot = queue_lengths_snapshot()
            logger.info(f"[Backfill] Postgres adapter skips legacy MySQL refill, queue_depths={snapshot}")
            return

        self._refill_single_us(db)
        self._refill_single_other(db)
        self._refill_temp(db)
        self._refill_asin(db)

        # 6. 周期性打印队列长度
        snapshot = queue_lengths_snapshot()
        logger.info(f"[Backfill] queue_depths={snapshot}")

    # ─────────────────────────────────────────────────────────────────────
    #  超时处理
    # ─────────────────────────────────────────────────────────────────────

    def _timeout_single_to_failed(self, db: MySQLTaskDB) -> None:
        """
        single 任务硬 SLA：need_crawler_time 早于 NOW() - SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES
        且仍未完成（status NOT IN (2,3)），直接设 status=3。

        语义对齐现有 claim_single_tasks 内的同名逻辑——把它独立成独立守护进程跑，
        即使 worker 全部切到 Redis 队列消费（不再调用 claim_single_tasks）也能保证 SLA。
        """
        if not SINGLE_NEED_TIME_SLA_ENABLED:
            return
        from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import (
            SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES,
        )
        try:
            db._check_connection()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.cursor.execute(
                """
                SELECT id, task_id, asin, region
                FROM crawl_single_tasks
                WHERE status NOT IN (2, 3)
                AND need_crawler_time < DATE_SUB(NOW(), INTERVAL %s MINUTE)
                """,
                (SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES,),
            )
            timeout_rows = db.cursor.fetchall() or []
            sql = (
                "UPDATE crawl_single_tasks "
                "SET status=3, updated_at=%s, error_msg=%s "
                "WHERE status NOT IN (2, 3) "
                "AND need_crawler_time < DATE_SUB(NOW(), INTERVAL %s MINUTE)"
            )
            db.cursor.execute(
                sql, (now, "任务超时(backfill)", SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES)
            )
            affected = db.cursor.rowcount
            db.conn.commit()
            if affected:
                stale_by_key = {
                    KEY_REVIEW_SINGLE_US: set(),
                    KEY_REVIEW_SINGLE_OTHER: set(),
                }
                for row in timeout_rows:
                    queue_key = (
                        KEY_REVIEW_SINGLE_US
                        if str(row.get("region") or "").upper() == "US"
                        else KEY_REVIEW_SINGLE_OTHER
                    )
                    payload = make_queue_payload(row.get("id"), row.get("asin", ""))
                    stale_by_key[queue_key].update(queue_payload_identities(payload))
                    if row.get("task_id"):
                        stale_by_key[queue_key].add(str(row["task_id"]))
                removed = 0
                for queue_key, payloads in stale_by_key.items():
                    removed += remove_single_payloads(queue_key, payloads)
                logger.warning(
                    f"[Backfill] single 超时设 status=3: {affected} 条 "
                    f"(threshold={SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES}min, redis_removed={removed})"
                )
                if affected > ALERT_TIMEOUT_THRESHOLD:
                    self._maybe_alert(f"⚠️ single 单轮超时 {affected} 条，请检查 worker")
        except Exception:
            logger.error(f"[Backfill] _timeout_single_to_failed 异常: {traceback.format_exc()}")
            try:
                db.conn.rollback()
            except Exception:
                pass

    def _timeout_to_retry(
        self,
        db: MySQLTaskDB,
        table: str,
        time_field: str,
        stuck_minutes: int,
        label: str,
    ) -> None:
        """检测 status=1 + 长时间无更新 → 重置 status=0 等下一轮 refill 补回队列。"""
        try:
            db._check_connection()
            if hasattr(db, "reset_stuck_tasks_to_retry"):
                affected = db.reset_stuck_tasks_to_retry(table, time_field, stuck_minutes)
            else:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sql = (
                    f"UPDATE {table} SET status=0, {time_field}=%s "
                    f"WHERE status=1 AND {time_field} < DATE_SUB(NOW(), INTERVAL %s MINUTE)"
                )
                db.cursor.execute(sql, (now, stuck_minutes))
                affected = db.cursor.rowcount
                db.conn.commit()
            if affected:
                logger.warning(
                    f"[Backfill] {label} 僵尸任务重置 status=0: {affected} 条 (threshold={stuck_minutes}min)"
                )
                if affected > ALERT_TIMEOUT_THRESHOLD:
                    self._maybe_alert(f"⚠️ {label} 单轮重置 {affected} 条僵尸任务，可能 worker 大规模死亡")
        except Exception:
            logger.error(f"[Backfill] _timeout_to_retry({label}) 异常: {traceback.format_exc()}")
            try:
                db.conn.rollback()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    #  Redis 队列补齐
    # ─────────────────────────────────────────────────────────────────────

    def _refill_single_us(self, db: MySQLTaskDB) -> None:
        self._refill_review_single(db, region_filter="US", queue_key=KEY_REVIEW_SINGLE_US)

    def _refill_single_other(self, db: MySQLTaskDB) -> None:
        self._refill_review_single(db, region_filter="OTHER", queue_key=KEY_REVIEW_SINGLE_OTHER)

    def _refill_review_single(self, db: MySQLTaskDB, region_filter: str, queue_key: str) -> None:
        """
        从 crawl_single_tasks 拉 status=0 的任务，
        对比 Redis 队列里已有的 id / 旧 task_id，缺失的用 id:asin 写回 ZSET。

        region_filter:
          "US"    → 只取 region='US'
          "OTHER" → 排除 region='US'
        """
        try:
            db._check_connection()
            if region_filter == "US":
                where_region = "region = 'US'"
            else:
                where_region = "region != 'US'"
            sql = (
                "SELECT id, task_id, asin, priority, need_crawler_time FROM crawl_single_tasks "
                f"WHERE status = 0 AND {where_region} "
                "ORDER BY priority ASC, need_crawler_time ASC, id ASC "
                "LIMIT %s"
            )
            db.cursor.execute(sql, (MAX_SCAN_ROWS,))
            rows = db.cursor.fetchall() or []
            if not rows:
                return
            candidates = []
            for r in rows:
                payload = make_queue_payload(r["id"], r.get("asin", ""))
                identities = (payload, str(r["id"]), str(r.get("task_id") or ""))
                candidates.append((payload, identities, r.get("need_crawler_time"), r.get("priority")))
            missing = missing_single_payloads(queue_key, candidates)
            if missing:
                pushed = push_single_to_key(queue_key, missing)
                logger.warning(
                    f"[Backfill] refill {queue_key}: 补 {pushed} 条（MySQL pending={len(rows)}, "
                    f"Redis depth={queue_length(queue_key)}）"
                )
        except Exception:
            logger.error(f"[Backfill] _refill_review_single({region_filter}) 异常: {traceback.format_exc()}")

    def _refill_temp(self, db: MySQLTaskDB) -> None:
        """temp 任务用 MySQL 自增 id:asin 入队。"""
        try:
            db._check_connection()
            db.cursor.execute(
                "SELECT id, asin FROM crawler_asin_tasks_temp WHERE status = 0 LIMIT %s",
                (MAX_SCAN_ROWS,)
            )
            rows = db.cursor.fetchall() or []
            if not rows:
                return
            in_redis = list_queue_members(KEY_REVIEW_TEMP)
            redis_ids = set()
            for item in in_redis:
                redis_ids.update(queue_payload_identities(item))
            missing = [
                make_queue_payload(r["id"], r.get("asin", ""))
                for r in rows
                if str(r["id"]) not in redis_ids
            ]
            if missing:
                pushed = push_to_key(KEY_REVIEW_TEMP, missing)
                logger.warning(
                    f"[Backfill] refill {KEY_REVIEW_TEMP}: 补 {pushed} 条"
                )
        except Exception:
            logger.error(f"[Backfill] _refill_temp 异常: {traceback.format_exc()}")

    def _refill_asin(self, db: MySQLTaskDB) -> None:
        """ASIN 详情任务用 MySQL 自增 id:asin 入队，兼容旧 task_id 队列值。"""
        try:
            db._check_connection()
            db.cursor.execute(
                "SELECT id, task_id, asin FROM crawl_asin_detail_tasks "
                "WHERE status = 0 "
                "LIMIT %s",
                (MAX_SCAN_ROWS,)
            )
            rows = db.cursor.fetchall() or []
            if not rows:
                return
            in_redis = list_queue_members(KEY_ASIN)
            redis_ids = set()
            for item in in_redis:
                redis_ids.update(queue_payload_identities(item))
            missing = [
                make_queue_payload(r["id"], r.get("asin", ""))
                for r in rows
                if str(r["id"]) not in redis_ids
                and str(r.get("task_id") or "") not in redis_ids
            ]
            if missing:
                pushed = push_to_key(KEY_ASIN, missing)
                logger.warning(
                    f"[Backfill] refill {KEY_ASIN}: 补 {pushed} 条"
                )
        except Exception:
            logger.error(f"[Backfill] _refill_asin 异常: {traceback.format_exc()}")

    # ─────────────────────────────────────────────────────────────────────
    #  报警（带 10 分钟冷却，防止刷屏）
    # ─────────────────────────────────────────────────────────────────────

    _last_alert_ts = 0.0

    def _maybe_alert(self, msg: str) -> None:
        now = time.time()
        if now - self._last_alert_ts < 600:
            return
        self._last_alert_ts = now
        try:
            from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
            send_custom_robot_group_message(msg)
        except Exception:
            pass
