"""
DailyAggregator — 每日账号指标聚合守护进程

职责：
  1. 每小时从 crawler_event_log + account_usage_log 聚合写入 account_daily_summary
  2. 从 crawler_event_log 中提取 IP 记录写入 account_ip_log
  3. 交叉校验 event_log 和 usage_log 的任务数一致性

在 get_reviews_main.py 的 __main__ 中启动：
    from app.crawlers.amazon_crawler.shuler.util.daily_aggregator import DailyAggregator
    agg = DailyAggregator()
    agg.start()
"""
import json
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from multiprocessing import Process

from loguru import logger

from app.db import session_scope
from app.models import AccountDailySummary, AccountIpLog, AccountUsageLog, CrawlerEventLog
from app.crawlers.amazon_crawler.shuler.util.config import (
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB,
)

AGGREGATION_INTERVAL = 3600        # 每小时轻量聚合一次
EVENT_LOG_RETENTION_DAYS = 3       # crawler_event_log 只保留近3天
USAGE_LOG_RETENTION_DAYS = 90      # account_usage_log 保留近90天


class DailyAggregator(Process):
    """
    守护进程，每小时聚合当天数据写入 account_daily_summary / account_ip_log，
    每天凌晨00:30补跑昨日完整聚合（含 LAG 窗口函数），并清理过期原始事件日志。

    数据保留策略：
      crawler_event_log   → 3 天（原始素材，聚合后即可清理）
      account_usage_log   → 30 天（任务级钻取）
      account_daily_summary → 永久（历史分析主表）
    """

    def __init__(self):
        super().__init__(name="DailyAggregator", daemon=True)

    def run(self) -> None:
        logger.info("[DailyAggregator] 启动")
        last_daily_date = None   # 记录最近一次完整日聚合的日期

        while True:
            try:
                now = datetime.now()
                today = now.date()

                # 每小时：轻量聚合当天数据（跳过耗时的 interval_stddev 计算）
                self._aggregate_daily_summary_postgres(today, compute_interval_stddev=False)
                self._aggregate_ip_log_postgres(today)
                self._cross_validate_postgres(today)
                logger.info(f"[DailyAggregator] 轻量聚合完成: {today}")

                # 每天00:30~01:30之间：补跑昨日完整聚合 + 清理过期数据
                if now.hour == 0 and last_daily_date != today:
                    yesterday = today - timedelta(days=1)
                    self._aggregate_daily_summary_postgres(yesterday, compute_interval_stddev=True)
                    logger.info(f"[DailyAggregator] 昨日完整聚合完成: {yesterday}")
                    self._cleanup_old_logs_postgres()
                    last_daily_date = today

            except Exception:
                logger.error(f"[DailyAggregator] 聚合异常:\n{traceback.format_exc()}")

            time.sleep(AGGREGATION_INTERVAL)

    def _cleanup_old_logs_postgres(self) -> dict:
        event_cutoff = datetime.utcnow() - timedelta(days=EVENT_LOG_RETENTION_DAYS)
        usage_cutoff = datetime.utcnow() - timedelta(days=USAGE_LOG_RETENTION_DAYS)
        with session_scope() as s:
            deleted_events = s.query(CrawlerEventLog).filter(CrawlerEventLog.created_at < event_cutoff).delete(synchronize_session=False)
            deleted_usage = s.query(AccountUsageLog).filter(AccountUsageLog.created_at < usage_cutoff).delete(synchronize_session=False)
        logger.info(
            f"[DailyAggregator] 过期日志清理完成: event_log -{deleted_events} 行, "
            f"usage_log -{deleted_usage} 行"
        )
        return {"event_log": int(deleted_events or 0), "usage_log": int(deleted_usage or 0)}

    def _aggregate_daily_summary_postgres(self, target_date, compute_interval_stddev: bool = True) -> None:
        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        now = datetime.utcnow()
        with session_scope() as s:
            events = s.query(CrawlerEventLog).filter(
                CrawlerEventLog.created_at >= start,
                CrawlerEventLog.created_at < end,
                CrawlerEventLog.username != "",
            ).all()
            usages = s.query(AccountUsageLog).filter(
                AccountUsageLog.created_at >= start,
                AccountUsageLog.created_at < end,
                AccountUsageLog.username != "",
            ).all()

            summaries = {}
            hour_map = defaultdict(lambda: defaultdict(int))
            page_times = defaultdict(list)
            for event in events:
                key = (event.username or "", event.country or "")
                row = summaries.setdefault(key, {
                    "total_tasks": 0,
                    "success_tasks": 0,
                    "failed_tasks": 0,
                    "total_pages": 0,
                    "captcha_count": 0,
                    "ban_count": 0,
                    "login_redirect_count": 0,
                    "robot_check_count": 0,
                    "proxy_rotate_count": 0,
                    "proxies": set(),
                    "asins": set(),
                    "sessions": set(),
                })
                event_type = event.event_type or ""
                if event_type == "task_start":
                    row["total_tasks"] += 1
                elif event_type == "task_success":
                    row["success_tasks"] += 1
                elif event_type == "task_failed":
                    row["failed_tasks"] += 1
                elif event_type == "page_fetched":
                    row["total_pages"] += 1
                    page_times[key].append(event.created_at)
                elif event_type == "captcha_hit":
                    row["captcha_count"] += 1
                elif event_type == "account_banned":
                    row["ban_count"] += 1
                elif event_type == "login_redirect":
                    row["login_redirect_count"] += 1
                elif event_type == "robot_check":
                    row["robot_check_count"] += 1
                elif event_type == "proxy_rotate":
                    row["proxy_rotate_count"] += 1
                if event.proxy:
                    row["proxies"].add(event.proxy)
                if event.asin:
                    row["asins"].add(event.asin)
                if event.session_seq:
                    row["sessions"].add(event.session_seq)
                if event_type in {"page_fetched", "task_start"} and event.created_at:
                    hour_map[key][str(event.created_at.hour)] += 1

            usage_map = {}
            for usage in usages:
                key = (usage.username or "", usage.country or "")
                row = usage_map.setdefault(key, {"total_reviews": 0, "total_duration": 0, "durations": []})
                row["total_reviews"] += int(usage.review_count or 0)
                row["total_duration"] += int(usage.duration_seconds or 0)
                row["durations"].append(int(usage.duration_seconds or 0))

            for key, row in summaries.items():
                username, country = key
                usage = usage_map.get(key, {"total_reviews": 0, "total_duration": 0, "durations": []})
                total_tasks = int(row["total_tasks"] or 0)
                failed = int(row["failed_tasks"] or 0)
                durations = usage.get("durations") or []
                avg_duration = sum(durations) / len(durations) if durations else 0.0
                interval_stddev = None
                if compute_interval_stddev and len(page_times.get(key, [])) > 1:
                    timestamps = sorted(page_times[key])
                    deltas = [(timestamps[i] - timestamps[i - 1]).total_seconds() for i in range(1, len(timestamps))]
                    if len(deltas) > 1:
                        avg_delta = sum(deltas) / len(deltas)
                        interval_stddev = (sum((x - avg_delta) ** 2 for x in deltas) / (len(deltas) - 1)) ** 0.5
                    elif deltas:
                        interval_stddev = 0.0

                summary = s.query(AccountDailySummary).filter_by(username=username, country=country, date=target_date).first()
                if summary is None:
                    summary = AccountDailySummary(username=username, country=country, date=target_date, created_at=now)
                    s.add(summary)
                summary.total_tasks = total_tasks
                summary.success_tasks = int(row["success_tasks"] or 0)
                summary.failed_tasks = failed
                summary.total_pages = int(row["total_pages"] or 0)
                summary.total_reviews = int(usage.get("total_reviews") or 0)
                summary.captcha_count = int(row["captcha_count"] or 0)
                summary.ban_count = int(row["ban_count"] or 0)
                summary.login_redirect_count = int(row["login_redirect_count"] or 0)
                summary.robot_check_count = int(row["robot_check_count"] or 0)
                summary.proxy_rotate_count = int(row["proxy_rotate_count"] or 0)
                summary.avg_duration_seconds = float(avg_duration or 0)
                summary.total_duration_seconds = int(usage.get("total_duration") or 0)
                summary.session_count = len(row["sessions"])
                summary.active_hour_distribution = dict(hour_map.get(key, {}))
                summary.request_interval_stddev = interval_stddev
                summary.distinct_ips = len(row["proxies"])
                summary.distinct_asins = len(row["asins"])
                summary.error_rate = round(failed / total_tasks, 4) if total_tasks else 0.0
                summary.updated_at = now
        logger.info(f"[DailyAggregator] 聚合 {len(summaries)} 条账号日汇总")

    def _aggregate_ip_log_postgres(self, target_date) -> None:
        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        seen = {}
        with session_scope() as s:
            events = s.query(CrawlerEventLog).filter(
                CrawlerEventLog.created_at >= start,
                CrawlerEventLog.created_at < end,
                CrawlerEventLog.username != "",
                CrawlerEventLog.proxy != "",
            ).all()
            for event in events:
                key = (event.username or "", event.proxy or "", event.country or "")
                row = seen.setdefault(key, {"first": event.created_at, "last": event.created_at, "count": 0})
                row["first"] = min(row["first"], event.created_at)
                row["last"] = max(row["last"], event.created_at)
                row["count"] += 1
            for (username, ip, country), data in seen.items():
                item = s.query(AccountIpLog).filter_by(username=username, ip=ip).first()
                if item is None:
                    item = AccountIpLog(username=username, ip=ip, country=country, first_seen_at=data["first"], last_seen_at=data["last"], request_count=0)
                    s.add(item)
                item.country = country
                item.first_seen_at = min(item.first_seen_at, data["first"])
                item.last_seen_at = max(item.last_seen_at, data["last"])
                item.request_count = int(item.request_count or 0) + int(data["count"] or 0)
        logger.debug(f"[DailyAggregator] IP 记录聚合: {len(seen)} 条")

    def _cross_validate_postgres(self, target_date) -> None:
        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        with session_scope() as s:
            event_count = s.query(CrawlerEventLog).filter(
                CrawlerEventLog.event_type == "task_start",
                CrawlerEventLog.created_at >= start,
                CrawlerEventLog.created_at < end,
            ).count()
            usage_count = s.query(AccountUsageLog).filter(
                AccountUsageLog.created_at >= start,
                AccountUsageLog.created_at < end,
            ).count()
        if event_count > 0 and usage_count > 0:
            ratio = abs(event_count - usage_count) / max(event_count, usage_count)
            if ratio > 0.1:
                logger.warning(
                    f"[DailyAggregator] 数据偏差告警: event_log task_start={event_count}, "
                    f"usage_log={usage_count}, 偏差={ratio:.1%}")

    def _cleanup_old_logs(self, cursor, conn) -> None:
        """删除过期的原始事件日志，保持表体积可控"""
        try:
            cursor.execute(
                "DELETE FROM crawler_event_log WHERE created_at < NOW() - INTERVAL %s DAY LIMIT 200000",
                (EVENT_LOG_RETENTION_DAYS,)
            )
            deleted_events = cursor.rowcount
            cursor.execute(
                "DELETE FROM account_usage_log WHERE created_at < NOW() - INTERVAL %s DAY LIMIT 200000",
                (USAGE_LOG_RETENTION_DAYS,)
            )
            deleted_usage = cursor.rowcount
            conn.commit()
            logger.info(
                f"[DailyAggregator] 过期日志清理完成: event_log -{deleted_events} 行, "
                f"usage_log -{deleted_usage} 行"
            )
        except Exception:
            conn.rollback()
            logger.error(f"[DailyAggregator] 清理过期日志失败:\n{traceback.format_exc()}")

    def _aggregate_daily_summary(self, cursor, conn, target_date, compute_interval_stddev: bool = True) -> None:
        """从 crawler_event_log + account_usage_log 聚合写入 account_daily_summary"""
        start = target_date.strftime("%Y-%m-%d 00:00:00")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 从 crawler_event_log 聚合事件计数
        event_sql = """
            SELECT username, country,
                SUM(CASE WHEN event_type='task_start' THEN 1 ELSE 0 END) AS total_tasks,
                SUM(CASE WHEN event_type='task_success' THEN 1 ELSE 0 END) AS success_tasks,
                SUM(CASE WHEN event_type='task_failed' THEN 1 ELSE 0 END) AS failed_tasks,
                SUM(CASE WHEN event_type='page_fetched' THEN 1 ELSE 0 END) AS total_pages,
                SUM(CASE WHEN event_type='captcha_hit' THEN 1 ELSE 0 END) AS captcha_count,
                SUM(CASE WHEN event_type='account_banned' THEN 1 ELSE 0 END) AS ban_count,
                SUM(CASE WHEN event_type='login_redirect' THEN 1 ELSE 0 END) AS login_redirect_count,
                SUM(CASE WHEN event_type='robot_check' THEN 1 ELSE 0 END) AS robot_check_count,
                SUM(CASE WHEN event_type='proxy_rotate' THEN 1 ELSE 0 END) AS proxy_rotate_count,
                COUNT(DISTINCT proxy) AS distinct_ips,
                COUNT(DISTINCT asin) AS distinct_asins,
                COUNT(DISTINCT session_seq) AS session_count
            FROM crawler_event_log
            WHERE created_at >= %s AND created_at < %s
              AND username != ''
            GROUP BY username, country
        """
        cursor.execute(event_sql, (start, end))
        event_rows = cursor.fetchall() or []

        # 2. 从 account_usage_log 聚合任务结果
        usage_sql = """
            SELECT username, country,
                SUM(review_count) AS total_reviews,
                AVG(duration_seconds) AS avg_duration,
                SUM(duration_seconds) AS total_duration
            FROM account_usage_log
            WHERE created_at >= %s AND created_at < %s
              AND username != ''
            GROUP BY username, country
        """
        cursor.execute(usage_sql, (start, end))
        usage_rows = cursor.fetchall() or []
        usage_map = {(r["username"], r["country"]): r for r in usage_rows}

        # 3. 活跃时段分布
        hour_sql = """
            SELECT username, country, HOUR(created_at) AS hr, COUNT(*) AS cnt
            FROM crawler_event_log
            WHERE created_at >= %s AND created_at < %s
              AND username != ''
              AND event_type IN ('page_fetched', 'task_start')
            GROUP BY username, country, HOUR(created_at)
        """
        cursor.execute(hour_sql, (start, end))
        hour_rows = cursor.fetchall() or []
        hour_map = {}  # (username, country) -> {hour: count}
        for r in hour_rows:
            key = (r["username"], r["country"])
            if key not in hour_map:
                hour_map[key] = {}
            hour_map[key][str(r["hr"])] = r["cnt"]

        # 4. 请求间隔标准差（LAG 窗口函数，全表扫描代价较高，仅在每日完整聚合时计算）
        interval_map = {}
        if compute_interval_stddev:
            interval_sql = """
                SELECT username, country,
                    STDDEV_SAMP(TIMESTAMPDIFF(SECOND, lag_time, created_at)) AS interval_stddev
                FROM (
                    SELECT username, country, created_at,
                        LAG(created_at) OVER (PARTITION BY username, country ORDER BY created_at) AS lag_time
                    FROM crawler_event_log
                    WHERE created_at >= %s AND created_at < %s
                      AND event_type = 'page_fetched'
                      AND username != ''
                ) t
                WHERE lag_time IS NOT NULL
                GROUP BY username, country
            """
            try:
                cursor.execute(interval_sql, (start, end))
                interval_rows = cursor.fetchall() or []
                interval_map = {(r["username"], r["country"]): r.get("interval_stddev") for r in interval_rows}
            except Exception:
                interval_map = {}

        # 5. Upsert into account_daily_summary
        upsert_sql = """
            INSERT INTO account_daily_summary
            (username, `date`, country, total_tasks, success_tasks, failed_tasks,
             total_pages, total_reviews, captcha_count, ban_count,
             login_redirect_count, robot_check_count, proxy_rotate_count,
             avg_duration_seconds, total_duration_seconds, session_count,
             active_hour_distribution, request_interval_stddev,
             distinct_ips, distinct_asins, error_rate, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CAST(%s AS JSON), %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_tasks=VALUES(total_tasks),
                success_tasks=VALUES(success_tasks),
                failed_tasks=VALUES(failed_tasks),
                total_pages=VALUES(total_pages),
                total_reviews=VALUES(total_reviews),
                captcha_count=VALUES(captcha_count),
                ban_count=VALUES(ban_count),
                login_redirect_count=VALUES(login_redirect_count),
                robot_check_count=VALUES(robot_check_count),
                proxy_rotate_count=VALUES(proxy_rotate_count),
                avg_duration_seconds=VALUES(avg_duration_seconds),
                total_duration_seconds=VALUES(total_duration_seconds),
                session_count=VALUES(session_count),
                active_hour_distribution=VALUES(active_hour_distribution),
                request_interval_stddev=VALUES(request_interval_stddev),
                distinct_ips=VALUES(distinct_ips),
                distinct_asins=VALUES(distinct_asins),
                error_rate=VALUES(error_rate),
                updated_at=VALUES(updated_at)
        """

        for row in event_rows:
            key = (row["username"], row["country"])
            usage = usage_map.get(key, {})
            total_tasks = int(row.get("total_tasks") or 0)
            failed = int(row.get("failed_tasks") or 0)
            error_rate = round(failed / total_tasks, 4) if total_tasks > 0 else 0.0

            params = (
                row["username"], target_date, row["country"],
                total_tasks,
                int(row.get("success_tasks") or 0),
                failed,
                int(row.get("total_pages") or 0),
                int(usage.get("total_reviews") or 0),
                int(row.get("captcha_count") or 0),
                int(row.get("ban_count") or 0),
                int(row.get("login_redirect_count") or 0),
                int(row.get("robot_check_count") or 0),
                int(row.get("proxy_rotate_count") or 0),
                float(usage.get("avg_duration") or 0),
                int(usage.get("total_duration") or 0),
                int(row.get("session_count") or 0),
                json.dumps(hour_map.get(key, {}), ensure_ascii=False),
                interval_map.get(key),
                int(row.get("distinct_ips") or 0),
                int(row.get("distinct_asins") or 0),
                error_rate,
                now, now,
            )
            try:
                cursor.execute(upsert_sql, params)
            except Exception:
                logger.warning(f"[DailyAggregator] upsert 失败 {row['username']}: {traceback.format_exc()}")
        conn.commit()
        logger.info(f"[DailyAggregator] 聚合 {len(event_rows)} 条账号日汇总")

    def _aggregate_ip_log(self, cursor, conn, target_date) -> None:
        """从 crawler_event_log 提取 IP 记录写入 account_ip_log"""
        start = target_date.strftime("%Y-%m-%d 00:00:00")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

        ip_sql = """
            SELECT username, proxy AS ip, country,
                MIN(created_at) AS first_seen,
                MAX(created_at) AS last_seen,
                COUNT(*) AS req_count
            FROM crawler_event_log
            WHERE created_at >= %s AND created_at < %s
              AND proxy IS NOT NULL AND proxy != ''
              AND username != ''
            GROUP BY username, proxy, country
        """
        cursor.execute(ip_sql, (start, end))
        rows = cursor.fetchall() or []

        upsert_sql = """
            INSERT INTO account_ip_log (username, ip, country, first_seen_at, last_seen_at, request_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_seen_at = GREATEST(last_seen_at, VALUES(last_seen_at)),
                request_count = request_count + VALUES(request_count)
        """
        for r in rows:
            try:
                cursor.execute(upsert_sql, (
                    r["username"], r["ip"], r["country"],
                    r["first_seen"], r["last_seen"], r["req_count"],
                ))
            except Exception:
                pass
        conn.commit()
        logger.debug(f"[DailyAggregator] IP 记录聚合: {len(rows)} 条")

    def _cross_validate(self, cursor, target_date) -> None:
        """交叉校验 event_log 和 usage_log 的任务数"""
        start = target_date.strftime("%Y-%m-%d 00:00:00")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

        try:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM crawler_event_log "
                "WHERE event_type='task_start' AND created_at >= %s AND created_at < %s",
                (start, end))
            event_count = (cursor.fetchone() or {}).get("cnt", 0)

            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM account_usage_log "
                "WHERE created_at >= %s AND created_at < %s",
                (start, end))
            usage_count = (cursor.fetchone() or {}).get("cnt", 0)

            if event_count > 0 and usage_count > 0:
                ratio = abs(event_count - usage_count) / max(event_count, usage_count)
                if ratio > 0.1:
                    logger.warning(
                        f"[DailyAggregator] 数据偏差告警: event_log task_start={event_count}, "
                        f"usage_log={usage_count}, 偏差={ratio:.1%}")
        except Exception:
            pass

    @staticmethod
    def _init_mysql():
        import mysql.connector
        try:
            return mysql.connector.connect(
                host=MYSQL_HOST, port=MYSQL_PORT,
                user=MYSQL_USER, password=MYSQL_PASSWORD,
                database=MYSQL_DB, autocommit=False,
            )
        except Exception as exc:
            logger.warning(f"[DailyAggregator] MySQL 连接不可用，跳过 MySQL 聚合: {exc}")
            return None

    @staticmethod
    def _ensure_tables(cursor, conn):
        """确保 account_daily_summary / account_risk_profile / account_ip_log 表存在"""
        from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
        db = MySQLTaskDB()
        db.init_queue_tables()
        db.close()
