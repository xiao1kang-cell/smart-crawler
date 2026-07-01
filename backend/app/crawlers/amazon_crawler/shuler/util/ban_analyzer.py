"""
BanAnalyzer — 封禁归因 + 告警 + 全局自适应调速

核心能力：
  1. 定时扫描 InfluxDB（或降级扫描 MySQL crawler_event_log）提取特征
  2. 规则引擎归因（速率过高 / IP 复用 / 请求模式被识别 / 验证码高发）
  3. 触发 DingTalk 告警（30 分钟冷却）
  4. 写 Redis 全局速率因子，worker 读取后动态延迟任务间隔（见 get_global_rate_factor）
  5. 账号异常次数超阈值时自动降低该账号每日配额（见 increment_account_error）

Redis key 约定：
  crawler:rate_factor          → float, 全局速率因子 [0.5, 1.0]，默认 1.0
  crawler:rate_factor_ts       → int timestamp, 上次调整时间
  crawler:ban_alert_ts         → int timestamp, 上次告警发送时间
  crawler:account:err:{name}   → int, 账号 24h 内累计异常次数
  crawler:account:quota:{name} → float, 账号配额因子 [0.3, 1.0]

BanAnalyzerDaemon：守护进程，每 ANALYZE_INTERVAL_SECONDS 秒运行一次分析。
在 get_reviews_main.py 的 __main__ 中调用 BanAnalyzerDaemon().start() 启动。

worker 集成（reviews.py）：
    from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import (
        get_global_rate_factor, increment_account_error, reset_account_error
    )
    rate_factor = get_global_rate_factor()          # 任务之间读取
    time.sleep(TASK_DELAY / rate_factor)            # 只放大任务间隔，不放大页内翻页
    # 成功后：reset_account_error(username)
    # 失败后：increment_account_error(username)
"""
import json
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from multiprocessing import Process
from typing import Optional

import redis as redis_lib
from loguru import logger
from sqlalchemy import case, func

from app.db import session_scope
from app.models import CrawlerEventLog
from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB,
)

# ─── 分析参数 ──────────────────────────────────────────────────────────────────
ANALYZE_INTERVAL_SECONDS   = 300       # 每 5 分钟分析一次
LOOKBACK_MINUTES           = 60        # 回溯 1 小时数据

# 全局速率因子控制
RATE_BACKOFF_FACTOR        = 0.5       # 触发告警时速率乘以该因子（减半）
RATE_RECOVERY_FACTOR       = 1.05      # 正常时每轮缓慢恢复 5%
RATE_MIN                   = 0.5       # 最低速率因子（50% 原速，避免长页任务被拖到超时）
RATE_MAX                   = 1.0       # 最高速率因子（全速）
LEGACY_MYSQL_ANALYTICS_ENABLED = (
    os.getenv("AMAZON_VOC_ENABLE_LEGACY_MYSQL_ANALYTICS", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
RATE_FACTOR_TTL_SECONDS    = int(os.getenv("RATE_FACTOR_TTL_SECONDS", str(3 * 3600)))

# 触发全局降速的封禁阈值（lookback 期间内）
BAN_TRIGGER_COUNT          = 2         # ≥ 2 次封禁 → 触发降速 + 告警

# 告警冷却（30 分钟内不重复发警报，避免刷钉钉）
ALERT_COOLDOWN_SECONDS     = 1800

# 账号异常计数（24h 滑动窗口）
ACCOUNT_ERR_KEY_PREFIX     = "crawler:account:err:"
ACCOUNT_ERR_TTL            = 86400     # 24 小时 TTL
ACCOUNT_QUOTA_KEY_PREFIX   = "crawler:account:quota:"
ACCOUNT_QUOTA_STAGE_PREFIX = "crawler:account:quota_stage:"
ACCOUNT_QUOTA_TTL          = 86400

ACCOUNT_ERR_WARN_THRESHOLD = 3         # ≥ 3 次开始降配额
ACCOUNT_ERR_BAN_THRESHOLD  = 6         # ≥ 6 次强降配额（叠加）
ACCOUNT_QUOTA_REDUCE_STEP  = 0.7       # 每次降至当前的 70%
ACCOUNT_QUOTA_MIN          = 0.3       # 最低 30%

# ─── Redis key 常量 ────────────────────────────────────────────────────────────
RATE_FACTOR_KEY   = "crawler:rate_factor"       # 全局兜底
RATE_FACTOR_TS    = "crawler:rate_factor_ts"
BAN_ALERT_TS_KEY  = "crawler:ban_alert_ts"

LOOKBACK_MINUTES_LONG = 1440  # 24 小时长窗口（用于趋势分析）

# ─── 分站点阈值（JP/DE 更严格）────────────────────────────────────────────────
SITE_THRESHOLDS = {
    "US": {"rps_warn": 2.0, "ban_trigger": 2, "captcha_warn": 2},
    "JP": {"rps_warn": 1.5, "ban_trigger": 1, "captcha_warn": 1},
    "DE": {"rps_warn": 1.5, "ban_trigger": 2, "captcha_warn": 2},
    "UK": {"rps_warn": 1.8, "ban_trigger": 2, "captcha_warn": 2},
    "default": {"rps_warn": 2.0, "ban_trigger": 2, "captcha_warn": 2},
}

def _get_site_threshold(site: str) -> dict:
    return SITE_THRESHOLDS.get(site.upper(), SITE_THRESHOLDS["default"])

def _rate_key(site: str = "") -> str:
    """分站点 Redis key，无站点则回落全局"""
    return f"crawler:rate_factor:{site.upper()}" if site else RATE_FACTOR_KEY

def _rate_ts_key(site: str = "") -> str:
    return f"crawler:rate_factor_ts:{site.upper()}" if site else RATE_FACTOR_TS

def _alert_ts_key(site: str = "") -> str:
    return f"crawler:ban_alert_ts:{site.upper()}" if site else BAN_ALERT_TS_KEY


# ─── 工具 ──────────────────────────────────────────────────────────────────────

def _make_redis() -> redis_lib.Redis:
    return redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        username=REDIS_USERNAME, password=REDIS_PASSWORD,
        db=REDIS_DB, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3,
    )


# ─── 全局速率因子 API（供 worker / reviews.py 调用）────────────────────────────

def get_global_rate_factor(rc: Optional[redis_lib.Redis] = None,
                           site: str = "") -> float:
    """
    获取速率因子（默认 1.0，全速）。支持分站点和全局两级。
    优先读站点级，无值则读全局兜底。

    用法：
        rate = get_global_rate_factor(rc, site="US")
        time.sleep(base_interval * (1.0 / rate))
    """
    try:
        if rc is None:
            rc = _make_redis()
        # 优先读站点级
        if site:
            val = rc.get(_rate_key(site))
            if val:
                return max(RATE_MIN, min(RATE_MAX, float(val)))
        # 回落全局
        val = rc.get(RATE_FACTOR_KEY)
        return max(RATE_MIN, min(RATE_MAX, float(val))) if val else 1.0
    except Exception:
        return 1.0


def set_global_rate_factor(factor: float, rc: Optional[redis_lib.Redis] = None,
                           site: str = "") -> None:
    """设置速率因子（BanAnalyzer 内部调用），支持分站点"""
    try:
        if rc is None:
            rc = _make_redis()
        factor = max(RATE_MIN, min(RATE_MAX, factor))
        key = _rate_key(site)
        ts_key = _rate_ts_key(site)
        rc.set(key, str(factor), ex=RATE_FACTOR_TTL_SECONDS)
        rc.set(ts_key, str(int(time.time())), ex=RATE_FACTOR_TTL_SECONDS)
    except Exception:
        pass


# ─── 全局网络异常计数器 API（供 reviews.py 调用）──────────────────────────────
# 用于区分"网络层故障"与"风控封禁"：网络异常不降账号配额，但多进程同时异常时
# 需延长等待并告警，提示可能需要人工干预。

NETWORK_ERR_KEY            = "crawler:network:err"
NETWORK_ERR_TTL            = 300        # 5 分钟滑动窗口
NETWORK_ERR_MULTI_THRESHOLD = 3         # ≥3 个进程同时网络异常 → 延长等待
NETWORK_ERR_ALERT_THRESHOLD = 5         # ≥5 个进程同时网络异常 → DingTalk 告警（人工介入）
NETWORK_ERR_ALERT_KEY      = "crawler:network:alert_ts"
NETWORK_ERR_ALERT_COOLDOWN = 600        # 10 分钟内不重复告警


def increment_network_error(rc: Optional[redis_lib.Redis] = None) -> int:
    """
    记录一次全局网络异常（连接失败/超时），返回 5 分钟窗口内的并发计数。
    使用 INCR + EXPIRE 实现滑动计数，TTL 到期自动归零。
    """
    try:
        if rc is None:
            rc = _make_redis()
        count = rc.incr(NETWORK_ERR_KEY)
        rc.expire(NETWORK_ERR_KEY, NETWORK_ERR_TTL)
        return int(count)
    except Exception:
        return 0


def get_network_error_count(rc: Optional[redis_lib.Redis] = None) -> int:
    """读取当前 5 分钟窗口内的并发网络异常数"""
    try:
        if rc is None:
            rc = _make_redis()
        val = rc.get(NETWORK_ERR_KEY)
        return int(val) if val else 0
    except Exception:
        return 0


def clear_network_error(rc: Optional[redis_lib.Redis] = None) -> None:
    """网络恢复后主动清零（可选，TTL 到期也会自动清零）"""
    try:
        if rc is None:
            rc = _make_redis()
        rc.delete(NETWORK_ERR_KEY)
    except Exception:
        pass


def should_alert_network_error(rc: Optional[redis_lib.Redis] = None) -> bool:
    """
    判断是否需要发告警（冷却期内不重复发）。
    返回 True 时调用方负责发送告警，并调用 mark_network_alert_sent()。
    """
    try:
        if rc is None:
            rc = _make_redis()
        last_ts = rc.get(NETWORK_ERR_ALERT_KEY)
        if last_ts and (time.time() - float(last_ts)) < NETWORK_ERR_ALERT_COOLDOWN:
            return False
        return True
    except Exception:
        return False


def mark_network_alert_sent(rc: Optional[redis_lib.Redis] = None) -> None:
    """记录告警发送时间戳"""
    try:
        if rc is None:
            rc = _make_redis()
        rc.set(NETWORK_ERR_ALERT_KEY, str(time.time()), ex=NETWORK_ERR_ALERT_COOLDOWN)
    except Exception:
        pass


# ─── 账号异常计数 API（供 reviews.py 调用）────────────────────────────────────

def increment_account_error(username: str, rc: Optional[redis_lib.Redis] = None) -> int:
    """
    账号发生异常（失败/封禁）时调用，返回当前累计异常计数。
    计数带 24h TTL 自动过期（滑动计数窗口）。
    """
    try:
        if rc is None:
            rc = _make_redis()
        key = ACCOUNT_ERR_KEY_PREFIX + username
        count = rc.incr(key)
        rc.expire(key, ACCOUNT_ERR_TTL)
        return int(count)
    except Exception:
        return 0


def reset_account_error(username: str, rc: Optional[redis_lib.Redis] = None) -> None:
    """账号成功完成任务时调用，重置异常计数（表明账号状态恢复正常）"""
    try:
        if rc is None:
            rc = _make_redis()
        rc.delete(ACCOUNT_ERR_KEY_PREFIX + username)
        rc.delete(ACCOUNT_QUOTA_STAGE_PREFIX + username)
    except Exception:
        pass


def get_account_quota_factor(username: str, rc: Optional[redis_lib.Redis] = None) -> float:
    """
    获取账号每日配额因子（默认 1.0，有异常历史时 < 1.0）。
    优先级：DB 手动设置 > Redis 动态值 > 默认 1.0
    AccountScheduler._get_day_stats 会乘以该因子调低 daily_budget。
    """
    try:
        # 优先读 DB 持久化值（手动干预场景：解封账号降频）
        from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLClient
        db = MySQLClient()
        row = db.get_account_by_username(username)
        db.close()
        if row is not None:
            db_factor = float(row.get("quota_factor", 1.0))
            if db_factor < 1.0:
                return db_factor
    except Exception:
        pass
    # 回退到 Redis 动态因子
    try:
        if rc is None:
            rc = _make_redis()
        val = rc.get(ACCOUNT_QUOTA_KEY_PREFIX + username)
        return float(val) if val else 1.0
    except Exception:
        return 1.0


def _reduce_account_quota(username: str, rc: redis_lib.Redis) -> float:
    """降低账号配额因子，返回新值"""
    key = ACCOUNT_QUOTA_KEY_PREFIX + username
    cur = float(rc.get(key) or 1.0)
    new_val = max(ACCOUNT_QUOTA_MIN, cur * ACCOUNT_QUOTA_REDUCE_STEP)
    rc.set(key, str(round(new_val, 3)), ex=ACCOUNT_QUOTA_TTL)
    return new_val


def restore_account_quota(username: str, rc: Optional[redis_lib.Redis] = None) -> None:
    """账号连续成功后，逐步恢复配额（乘以 1/REDUCE_STEP 的开方恢复）"""
    try:
        if rc is None:
            rc = _make_redis()
        key = ACCOUNT_QUOTA_KEY_PREFIX + username
        cur = float(rc.get(key) or 1.0)
        if cur >= 1.0:
            return
        new_val = min(1.0, cur * (1.0 / ACCOUNT_QUOTA_REDUCE_STEP) ** 0.3)
        rc.set(key, str(round(new_val, 3)), ex=ACCOUNT_QUOTA_TTL)
    except Exception:
        pass


# ─── 封号原因常量 ─────────────────────────────────────────────────────────────

class BanReason:
    """区分封号/异常原因，用于 report_ban(reason=...) 和归因分析"""
    LOGIN_FAILED     = "login_failed"       # 登录失败
    ACCOUNT_BLOCKED  = "account_blocked"    # 账号被封
    CAPTCHA          = "captcha"            # 验证码拦截
    TLS_FINGERPRINT  = "tls_fingerprint"    # TLS 指纹被检测
    COOKIE_EXPIRED   = "cookie_expired"     # Cookie 过期
    RATE_LIMITED     = "rate_limited"       # 429/503 限速
    ROBOT_CHECK      = "robot_check"        # 机器人检测页面
    UNKNOWN          = "unknown"


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class BanAnalysisResult:
    cause: str          # rate_too_high / ip_reuse / pattern_detected / captcha / unknown
    confidence: float   # 0.0 ~ 1.0
    recommendation: str
    features: dict


# ─── BanAnalyzer（特征提取 + 规则归因）────────────────────────────────────────

class BanAnalyzer:
    """
    封禁归因分析器。
    优先查 InfluxDB（需配置 INFLUXDB_*），无 InfluxDB 时降级扫描 Redis event_log。
    """

    def __init__(self, influx_sink=None):
        self._influx = influx_sink  # InfluxDBSink 实例，可为 None

    def extract_features(self, rc: redis_lib.Redis,
                          lookback_minutes: int = LOOKBACK_MINUTES,
                          site: str = "") -> dict:
        """提取近 lookback_minutes 分钟内的封禁特征，支持按站点过滤"""
        postgres_features = self._from_postgres_events(lookback_minutes, site=site)
        if postgres_features.get("source") != "postgres_event_log_error":
            return postgres_features
        return self._from_redis_events(rc, lookback_minutes)

    def _from_postgres_events(self, minutes: int, site: str = "") -> dict:
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        try:
            with session_scope() as s:
                q = s.query(CrawlerEventLog).filter(CrawlerEventLog.created_at >= cutoff)
                if site:
                    q = q.filter(func.upper(CrawlerEventLog.country) == str(site).upper())

                metrics = q.with_entities(
                    func.coalesce(func.sum(case((CrawlerEventLog.event_type == "task_start", 1), else_=0)), 0).label("total_requests"),
                    func.coalesce(func.sum(case((CrawlerEventLog.event_type == "captcha_hit", 1), else_=0)), 0).label("captcha_count"),
                    func.coalesce(func.sum(case((
                        (CrawlerEventLog.event_type == "account_banned")
                        & (~CrawlerEventLog.error_msg.like("%登录失败%"))
                        & (~CrawlerEventLog.error_msg.like("%Non-base32%"))
                        & (~CrawlerEventLog.error_msg.like("%ProfileNotFoundError%"))
                        & (~CrawlerEventLog.error_msg.like("%AccountLoginError%"))
                        & (~CrawlerEventLog.error_msg.like("%COOKIE_REFRESH_EXHAUSTED%")),
                        1,
                    ), else_=0)), 0).label("ban_count"),
                    func.coalesce(func.sum(case((
                        (CrawlerEventLog.event_type == "page_failed")
                        & (CrawlerEventLog.http_status.in_((403, 429, 503))),
                        1,
                    ), else_=0)), 0).label("page_block_count"),
                    func.count(func.distinct(CrawlerEventLog.proxy)).label("ip_count"),
                ).one()

            total_requests = int(metrics.total_requests or 0)
            ban_count = int(metrics.ban_count or 0)
            captcha_count = int(metrics.captcha_count or 0)
            page_block_count = int(metrics.page_block_count or 0)
            block_count = ban_count + captcha_count + page_block_count
            avg_rps = round(total_requests / (minutes * 60), 4) if total_requests else 0.0
            ip_count = int(metrics.ip_count or 0)
            return {
                "total_requests": total_requests,
                "avg_rps": avg_rps,
                "block_count": block_count,
                "ip_count": ip_count if ip_count > 0 else 1,
                "ban_count": ban_count,
                "captcha_count": captcha_count,
                "source": "postgres_event_log",
            }
        except Exception as exc:
            logger.warning(f"[BanAnalyzer] Postgres event_log 查询不可用 site={site or 'GLOBAL'}: {exc}")
            return {
                "total_requests": 0,
                "avg_rps": 0.0,
                "block_count": 0,
                "ip_count": 1,
                "ban_count": 0,
                "captcha_count": 0,
                "source": "postgres_event_log_error",
            }

    def _from_influxdb(self, minutes: int, site: str = "") -> dict:
        bucket = self._influx._bucket
        window = f"-{minutes}m"
        site_filter = f' |> filter(fn:(r) => r.site == "{site.upper()}")' if site else ""

        # 1. 请求总数 & 被拦截数
        total = self._influx.query(
            f'from(bucket:"{bucket}") |> range(start:{window})'
            f' |> filter(fn:(r) => r._measurement == "crawler_request")'
            f'{site_filter}'
            f' |> group() |> count()'
        )
        blocked = self._influx.query(
            f'from(bucket:"{bucket}") |> range(start:{window})'
            f' |> filter(fn:(r) => r._measurement == "crawler_request"'
            f'   and r.is_blocked == true)'
            f'{site_filter}'
            f' |> group() |> count()'
        )
        # 2. 代理 IP 种类
        ip_data = self._influx.query(
            f'from(bucket:"{bucket}") |> range(start:{window})'
            f' |> filter(fn:(r) => r._measurement == "crawler_request")'
            f'{site_filter}'
            f' |> keep(columns:["proxy_ip"])'
            f' |> group() |> distinct(column:"proxy_ip")'
            f' |> count()'
        )
        # 3. 封禁事件（排除登录失败/Cookie过期等非风控原因，避免误触发降速）
        _ban_reason_filter = (
            ' |> filter(fn:(r) => r.reason != "login_failed" and r.reason != "cookie_expired")'
        )
        bans = self._influx.query(
            f'from(bucket:"{bucket}") |> range(start:{window})'
            f' |> filter(fn:(r) => r._measurement == "account_ban")'
            f'{site_filter}'
            f'{_ban_reason_filter}'
            f' |> group() |> sum(column:"_value")'
        )

        total_n = int(total[0].get("_value", 0)) if total else 0
        block_n = int(blocked[0].get("_value", 0)) if blocked else 0
        ip_n    = int(ip_data[0].get("_value", 1)) if ip_data else 1
        ban_n   = int(bans[0].get("_value", 0)) if bans else 0
        avg_rps = round(total_n / (minutes * 60), 4) if total_n else 0.0

        return {
            "total_requests": total_n,
            "avg_rps": avg_rps,
            "block_count": block_n,
            "ip_count": ip_n,
            "ban_count": ban_n,
            "source": "influxdb",
        }

    def _from_mysql_events(self, minutes: int, site: str = "") -> dict:
        """降级方案：从 MySQL crawler_event_log 聚合近 N 分钟事件，支持按站点过滤"""
        if not LEGACY_MYSQL_ANALYTICS_ENABLED:
            return {
                "total_requests": 0,
                "avg_rps": 0.0,
                "block_count": 0,
                "ip_count": 1,
                "ban_count": 0,
                "captcha_count": 0,
                "source": "mysql_event_log_disabled",
            }
        import mysql.connector

        cutoff = datetime.now() - timedelta(minutes=minutes)
        conn = None
        cursor = None
        # 站点过滤条件（country 列存储大写站点代码，如 US/UK/DE）
        site_filter = " AND country = %s" if site else ""
        base_params_1 = (cutoff, site.upper()) if site else (cutoff,)
        try:
            conn = mysql.connector.connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DB,
                autocommit=True,
                connection_timeout=5,
            )
            cursor = conn.cursor(dictionary=True)

            cursor.execute(
                f"""
                SELECT
                    SUM(event_type = 'task_start') AS total_requests,
                    SUM(event_type = 'captcha_hit') AS captcha_count,
                    SUM(
                        event_type = 'account_banned'
                        AND error_msg NOT LIKE '%%登录失败%%'
                        AND error_msg NOT LIKE '%%Non-base32%%'
                        AND error_msg NOT LIKE '%%ProfileNotFoundError%%'
                        AND error_msg NOT LIKE '%%AccountLoginError%%'
                        AND error_msg NOT LIKE '%%COOKIE_REFRESH_EXHAUSTED%%'
                    ) AS ban_count,
                    SUM(
                        event_type = 'page_failed'
                        AND http_status IN (403, 429, 503)
                    ) AS page_block_count
                FROM crawler_event_log
                WHERE created_at >= %s{site_filter}
                """,
                base_params_1
            )
            metrics = cursor.fetchone() or {}

            cursor.execute(
                f"""
                SELECT COUNT(DISTINCT proxy) AS ip_count
                FROM crawler_event_log
                WHERE created_at >= %s{site_filter}
                  AND proxy IS NOT NULL
                  AND proxy <> ''
                """,
                base_params_1
            )
            ip_row = cursor.fetchone() or {}
            ip_count = int(ip_row.get("ip_count", 0) or 0)

            total_requests = int(metrics.get("total_requests", 0) or 0)
            ban_count = int(metrics.get("ban_count", 0) or 0)
            captcha_count = int(metrics.get("captcha_count", 0) or 0)
            page_block_count = int(metrics.get("page_block_count", 0) or 0)
            block_count = ban_count + captcha_count + page_block_count
            avg_rps = round(total_requests / (minutes * 60), 4) if total_requests else 0.0

            return {
                "total_requests": total_requests,
                "avg_rps": avg_rps,
                "block_count": block_count,
                "ip_count": ip_count if ip_count > 0 else 1,
                "ban_count": ban_count,
                "captcha_count": captcha_count,
                "source": "mysql_event_log",
            }
        except Exception as exc:
            logger.warning(f"[BanAnalyzer] MySQL 降级查询不可用 site={site or 'GLOBAL'}: {exc}")
            return {
                "total_requests": 0,
                "avg_rps": 0.0,
                "block_count": 0,
                "ip_count": 1,
                "ban_count": 0,
                "captcha_count": 0,
                "source": "mysql_event_log_error",
            }
        finally:
            try:
                if cursor:
                    cursor.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _from_redis_events(self, rc: redis_lib.Redis, minutes: int) -> dict:
        """兜底降级方案：扫描 Redis event_log 队列近 N 分钟事件"""
        from app.crawlers.amazon_crawler.shuler.util.event_logger import EVENT_QUEUE_KEY

        cutoff = datetime.now() - timedelta(minutes=minutes)
        ban_count = 0
        captcha_count = 0
        total_tasks = 0
        block_count = 0
        try:
            items = rc.lrange(EVENT_QUEUE_KEY, 0, 2999)
            for raw in items:
                try:
                    ev = json.loads(raw)
                    ts_str = ev.get("created_at", "")
                    if not ts_str:
                        continue
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if ts < cutoff:
                        continue
                    et = ev.get("event_type", "")
                    err_msg = ev.get("error_msg", "") or ""
                    is_login_like = any(
                        token in err_msg
                        for token in (
                            "登录失败",
                            "Non-base32",
                            "ProfileNotFoundError",
                            "AccountLoginError",
                            "COOKIE_REFRESH_EXHAUSTED",
                        )
                    )
                    if et == "account_banned" and not is_login_like:
                        ban_count += 1
                    elif et == "captcha_hit":
                        captcha_count += 1
                    elif et == "task_start":
                        total_tasks += 1
                    status = int(ev.get("http_status", 0) or 0)
                    if (
                        (et == "account_banned" and not is_login_like)
                        or et == "captcha_hit"
                        or (et == "page_failed" and status in (403, 429, 503))
                    ):
                        block_count += 1
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "total_requests": total_tasks,
            "avg_rps": 0.0,
            "block_count": block_count,
            "ip_count": 1,
            "ban_count": ban_count,
            "captcha_count": captcha_count,
            "source": "redis_events",
        }

    def determine_cause(self, features: dict, site: str = "") -> BanAnalysisResult:
        """规则引擎：优先级匹配，返回封禁归因。支持分站点阈值。"""
        if not features:
            return BanAnalysisResult(
                cause="no_data", confidence=0.0,
                recommendation="无法提取特征，检查 InfluxDB / MySQL 连通性",
                features=features,
            )

        th = _get_site_threshold(site)

        # 规则 1：速率过高
        if features.get("avg_rps", 0) > th["rps_warn"]:
            return BanAnalysisResult(
                cause="rate_too_high",
                confidence=0.85,
                recommendation=(
                    f"降低 RPS 至当前的 60%，并增加随机抖动"
                    f"（current avg_rps={features['avg_rps']:.2f}, threshold={th['rps_warn']}）"
                ),
                features=features,
            )

        # 规则 2：IP 复用（0 或 1 个不同 IP）
        if features.get("ip_count", 2) <= 1:
            return BanAnalysisResult(
                cause="ip_reuse",
                confidence=0.80,
                recommendation="增大 IP 轮换频率，禁止单 IP 绑定单账号长时间使用",
                features=features,
            )

        # 规则 3：请求模式被识别（高拦截率 or 多次封禁）
        if features.get("block_count", 0) > 3:
            return BanAnalysisResult(
                cause="pattern_detected",
                confidence=0.70,
                recommendation="随机化 User-Agent / 增加星级切换延迟 / 调整请求时序",
                features=features,
            )

        # 规则 4：验证码高发
        if features.get("captcha_count", 0) >= th["captcha_warn"]:
            return BanAnalysisResult(
                cause="captcha_detection",
                confidence=0.75,
                recommendation="检查验证码识别服务，增加登录间隔，考虑降低请求频率",
                features=features,
            )

        return BanAnalysisResult(
            cause="unknown",
            confidence=0.3,
            recommendation="人工排查该时段日志，无法自动归因",
            features=features,
        )


# ─── AlertManager（告警 + 速率调节 + 账号配额管理）────────────────────────────

class AlertManager:
    """
    每轮执行：
      1. 提取特征 → 归因
      2. 触发封禁告警 + 全局降速
      3. 检查账号异常计数 → 降低配额
    """

    def __init__(self, influx_sink=None):
        self._analyzer = BanAnalyzer(influx_sink)

    # 需要分析的站点列表（从 amazon_config 获取）
    ACTIVE_SITES = ["US", "UK", "DE", "JP", "CA", "FR", "IT", "ES"]

    def _can_alert(self, rc: redis_lib.Redis, site: str = "") -> bool:
        key = _alert_ts_key(site)
        ts = rc.get(key)
        return not (ts and time.time() - float(ts) < ALERT_COOLDOWN_SECONDS)

    def run_once(self, rc: redis_lib.Redis) -> None:
        """分站点分析 + 全局兜底 + 24h 趋势检测"""
        # 1. 分站点短窗口分析（1h）
        alerted_sites: list[str] = []
        for site in self.ACTIVE_SITES:
            try:
                triggered = self._analyze_site(rc, site)
                if triggered:
                    alerted_sites.append(site)
            except Exception:
                logger.warning(f"[BanAnalyzer] 站点 {site} 分析异常: {traceback.format_exc()}")

        # 2. 全局兜底分析：若已有站点触发告警则跳过，避免重复告警
        if alerted_sites:
            logger.info(f"[BanAnalyzer] 全局兜底跳过，已触发告警的站点: {alerted_sites}")
        else:
            try:
                self._analyze_site(rc, site="")
            except Exception:
                logger.warning(f"[BanAnalyzer] 全局分析异常: {traceback.format_exc()}")

        # 3. 24h 长窗口趋势检测（从 MySQL）
        try:
            self._check_24h_trend(rc)
        except Exception:
            logger.warning(f"[BanAnalyzer] 24h 趋势检测异常: {traceback.format_exc()}")

        # 4. 账号配额检查
        self._check_account_quotas(rc)

    def _analyze_site(self, rc: redis_lib.Redis, site: str) -> bool:
        """对单个站点执行 1h 窗口分析，返回是否触发了告警"""
        features = self._analyzer.extract_features(rc, site=site)
        if not features:
            return False

        th = _get_site_threshold(site) if site else SITE_THRESHOLDS["default"]
        ban_count = features.get("ban_count", 0)
        result = self._analyzer.determine_cause(features, site=site)
        current_factor = get_global_rate_factor(rc, site=site)

        site_label = site or "GLOBAL"
        logger.info(
            f"[BanAnalyzer:{site_label}] cause={result.cause} "
            f"confidence={result.confidence:.0%} "
            f"ban_count={ban_count} "
            f"rate_factor={current_factor:.2f} "
            f"features={features}"
        )

        triggered = False
        trigger = th.get("ban_trigger", BAN_TRIGGER_COUNT)
        if ban_count >= trigger and result.cause not in ("unknown", "no_data"):
            new_factor = max(RATE_MIN, current_factor * RATE_BACKOFF_FACTOR)
            set_global_rate_factor(new_factor, rc, site=site)
            logger.warning(
                f"[BanAnalyzer:{site_label}] 封禁数={ban_count} ≥ {trigger}，"
                f"速率: {current_factor:.2f} → {new_factor:.2f}  原因={result.cause}"
            )
            if self._can_alert(rc, site=site):
                self._send_alert(rc, result, ban_count, current_factor, new_factor, site=site)
            triggered = True
        else:
            if current_factor < RATE_MAX:
                new_factor = min(RATE_MAX, current_factor * RATE_RECOVERY_FACTOR)
                set_global_rate_factor(new_factor, rc, site=site)
            else:
                new_factor = current_factor

        try:
            from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
            _rpt = get_reporter()
            if _rpt:
                _rpt.rate.report(
                    site=site or "all",
                    current_rps=new_factor,
                    target_rps=RATE_MAX,
                    throttled=(new_factor < RATE_MAX),
                )
        except Exception:
            pass

        return triggered

    def _check_24h_trend(self, rc: redis_lib.Redis) -> None:
        """24h 长窗口趋势检测：当前小时封号数 > 24h 平均值 3 倍时触发强响应"""
        try:
            long_features = self._analyzer._from_postgres_events(LOOKBACK_MINUTES_LONG)
            short_features = self._analyzer._from_postgres_events(LOOKBACK_MINUTES)

            if long_features.get("source") == "postgres_event_log_error":
                return

            long_bans = long_features.get("ban_count", 0)
            short_bans = short_features.get("ban_count", 0)

            # 24h 平均每小时封号数
            hourly_avg = long_bans / 24.0 if long_bans > 0 else 0

            if hourly_avg > 0 and short_bans > hourly_avg * 3:
                logger.warning(
                    f"[BanAnalyzer:24h] 趋势异常：近1h封号={short_bans}, "
                    f"24h平均每小时={hourly_avg:.1f}, 倍数={short_bans/hourly_avg:.1f}x"
                )
                # 全局强降速
                current = get_global_rate_factor(rc)
                new_factor = max(RATE_MIN, current * 0.4)
                set_global_rate_factor(new_factor, rc)
                try:
                    from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
                    _rpt = get_reporter()
                    if _rpt:
                        _rpt.rate.report(
                            site="all",
                            current_rps=new_factor,
                            target_rps=RATE_MAX,
                            throttled=True,
                        )
                except Exception:
                    pass
            elif long_bans > 0:
                logger.info(
                    f"[BanAnalyzer:24h] 趋势正常：近1h封号={short_bans}, "
                    f"24h总封号={long_bans}, 平均每小时={hourly_avg:.1f}"
                )
        except Exception:
            logger.warning(f"[BanAnalyzer:24h] 趋势分析失败: {traceback.format_exc()}")

    @staticmethod
    def _send_alert(rc, result: BanAnalysisResult,
                    ban_count: int, old_factor: float, new_factor: float,
                    site: str = "") -> None:
        try:
            from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
            site_label = site or "全局"
            msg = (
                f"🚨 爬虫风控告警 [{site_label}]\n"
                f"归因: {result.cause}  置信度: {result.confidence:.0%}\n"
                f"封禁次数(1h): {ban_count}\n"
                f"速率因子: {old_factor:.2f} → {new_factor:.2f}\n"
                f"建议: {result.recommendation}\n"
                f"特征: {json.dumps(result.features, ensure_ascii=False)}"
            )
            send_custom_robot_group_message(msg, at_mobiles=["17398238551"])
            rc.set(_alert_ts_key(site), str(time.time()))
            logger.info(f"[BanAnalyzer] 告警已发送 site={site_label}")
        except Exception:
            logger.warning(f"[BanAnalyzer] 发送告警失败: {traceback.format_exc()}")

    @staticmethod
    def _check_account_quotas(rc: redis_lib.Redis) -> None:
        """扫描所有账号异常计数，超阈值时降低其每日配额"""
        try:
            keys = rc.keys(ACCOUNT_ERR_KEY_PREFIX + "*")
            for key in keys:
                username = key[len(ACCOUNT_ERR_KEY_PREFIX):]
                err_count = int(rc.get(key) or 0)
                stage_key = ACCOUNT_QUOTA_STAGE_PREFIX + username
                current_stage = int(rc.get(stage_key) or 0)  # 0=未降,1=已触发warn,2=已触发ban
                if err_count >= ACCOUNT_ERR_BAN_THRESHOLD:
                    # 保留“叠加”语义：若尚未触发 warn 阶段，先补做一次，再做 ban 阶段
                    if current_stage < 1:
                        _reduce_account_quota(username, rc)
                    if current_stage < 2:
                        new_q = _reduce_account_quota(username, rc)
                        rc.set(stage_key, "2", ex=ACCOUNT_ERR_TTL)
                        logger.warning(
                            f"[BanAnalyzer] 账号 {username} 异常={err_count}"
                            f"（≥{ACCOUNT_ERR_BAN_THRESHOLD}），"
                            f"配额因子降至 {new_q:.2f}"
                        )
                elif err_count >= ACCOUNT_ERR_WARN_THRESHOLD:
                    if current_stage < 1:
                        new_q = _reduce_account_quota(username, rc)
                        rc.set(stage_key, "1", ex=ACCOUNT_ERR_TTL)
                        logger.info(
                            f"[BanAnalyzer] 账号 {username} 异常={err_count}"
                            f"（≥{ACCOUNT_ERR_WARN_THRESHOLD}），"
                            f"配额因子降至 {new_q:.2f}"
                        )
        except Exception:
            logger.warning(f"[BanAnalyzer] 账号配额检查失败: {traceback.format_exc()}")


# ─── BanAnalyzerDaemon（守护进程）──────────────────────────────────────────────

class BanAnalyzerDaemon(Process):
    """
    独立守护进程，每 ANALYZE_INTERVAL_SECONDS 秒运行一次 AlertManager.run_once()。
    在 get_reviews_main.py 的 __main__ 入口中启动：
        daemon = BanAnalyzerDaemon()
        daemon.start()
    """

    def __init__(self):
        super().__init__(name="BanAnalyzerDaemon", daemon=True)

    def run(self) -> None:
        logger.info("[BanAnalyzerDaemon] 启动")
        rc = _make_redis()

        manager = AlertManager(None)
        while True:
            try:
                manager.run_once(rc)
            except Exception:
                logger.error(f"[BanAnalyzerDaemon] 分析异常: {traceback.format_exc()}")
            time.sleep(ANALYZE_INTERVAL_SECONDS)
