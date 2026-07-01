"""
结构化事件日志模块

职责：
  1. 在爬取流程的关键节点发射事件（非阻塞，lpush 到 Redis 队列）
  2. EventLogConsumer 作为独立守护进程，批量从队列消费并写入 MySQL crawler_event_log

用法：
    # 发射事件（在 worker/scraper 中调用，不阻塞主流程）
    from app.crawlers.amazon_crawler.shuler.util.event_logger import push_event, EventType
    push_event(redis_client, EventType.PAGE_FETCHED, username="acc1", asin="B001", country="US",
               page=1, http_status=200, worker_id="w1")

    # 启动消费者守护进程（在 get_reviews_main.py 的 __main__ 中启动一次）
    from app.crawlers.amazon_crawler.shuler.util.event_logger import EventLogConsumer
    consumer = EventLogConsumer()
    consumer.start()
"""
import json
import time
import traceback
from datetime import datetime
from multiprocessing import Process
from typing import Optional

import redis as redis_lib
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB,
)
from app.db import session_scope
from app.models import CrawlerEventLog

# Redis 队列 key
EVENT_QUEUE_KEY = "crawler:event_log"
EVENT_QUEUE_MAX_LEN = 50_000   # 防止积压撑爆内存

# 消费者批次大小和间隔
CONSUMER_BATCH_SIZE = 100
CONSUMER_INTERVAL_SECONDS = 5


class EventType:
    """所有事件类型常量"""
    # 任务级别
    TASK_START       = "task_start"         # worker 开始执行任务
    TASK_SUCCESS     = "task_success"       # 任务成功完成
    TASK_FAILED      = "task_failed"        # 任务最终失败

    # 页面级别
    PAGE_FETCHED     = "page_fetched"       # 成功获取一页评论
    PAGE_FAILED      = "page_failed"        # 获取页面失败（含 HTTP 状态）
    CAPTCHA_HIT      = "captcha_hit"        # 遭遇 captcha / 风控拦截
    LOGIN_REDIRECT   = "login_redirect"     # 页面被跳转到登录页
    ROBOT_CHECK      = "robot_check"        # 出现 robot check 页面

    # 账号级别
    SESSION_START    = "session_start"      # 账号开始新会话
    SESSION_END      = "session_end"        # 账号会话正常结束
    ACCOUNT_COOLDOWN = "account_cooldown"   # 账号进入冷却
    ACCOUNT_BANNED   = "account_banned"     # 账号被判定为封号

    # 其他
    RETRY            = "retry"              # 触发重试
    PROXY_ROTATE     = "proxy_rotate"       # 代理切换


def _make_redis_client() -> redis_lib.Redis:
    return redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        username=REDIS_USERNAME, password=REDIS_PASSWORD,
        db=REDIS_DB, decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def push_event(
    redis_client,
    event_type: str,
    *,
    username: str = "",
    asin: str = "",
    country: str = "",
    page: int = 0,
    http_status: int = 0,
    daily_pages: int = 0,
    session_seq: int = 0,
    worker_id: str = "",
    proxy: str = "",
    error_msg: str = "",
    extra: Optional[dict] = None,
):
    """
    非阻塞地将一条事件推送到 Redis 队列。
    即使 Redis 不可达，只记录警告，不抛出异常（不影响主流程）。
    """
    try:
        event = {
            "event_type": event_type,
            "username": username,
            "asin": asin,
            "country": country,
            "page": page,
            "http_status": http_status,
            "daily_pages": daily_pages,
            "session_seq": session_seq,
            "worker_id": worker_id,
            "proxy": proxy,
            "error_msg": error_msg[:500] if error_msg else "",
            "extra": extra or {},
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload = json.dumps(event, ensure_ascii=False)
        # 用 lpush + ltrim 防止无限积压
        pipe = redis_client.pipeline(transaction=False)
        pipe.lpush(EVENT_QUEUE_KEY, payload)
        pipe.ltrim(EVENT_QUEUE_KEY, 0, EVENT_QUEUE_MAX_LEN - 1)
        pipe.execute()
    except Exception as e:
        logger.warning(f"[EventLogger] push_event 失败（不影响主流程）: {e}")


# ==================== 消费者 ====================

class EventLogConsumer(Process):
    """
    独立进程，轮询 Redis 队列，批量写入 MySQL crawler_event_log。
    应在主进程启动时 start() 一次，设置为 daemon=True 随主进程退出。
    """

    def __init__(self):
        super().__init__(name="EventLogConsumer", daemon=True)

    def run(self):
        logger.info("[EventLogConsumer] 启动")
        redis_client = _make_redis_client()

        while True:
            try:
                events = []
                for _ in range(CONSUMER_BATCH_SIZE):
                    item = redis_client.rpop(EVENT_QUEUE_KEY)
                    if item is None:
                        break
                    try:
                        events.append(json.loads(item))
                    except Exception:
                        pass

                if events:
                    self._batch_insert_postgres(events)

                time.sleep(CONSUMER_INTERVAL_SECONDS)

            except redis_lib.RedisError as e:
                logger.warning(f"[EventLogConsumer] Redis 异常，5s 后重连: {e}")
                time.sleep(5)
                try:
                    redis_client = _make_redis_client()
                except Exception:
                    pass
            except Exception:
                logger.error(f"[EventLogConsumer] 未知异常:\n{traceback.format_exc()}")
                time.sleep(5)

    # ---------- 私有工具 ----------

    @staticmethod
    def _parse_created_at(value):
        if isinstance(value, datetime):
            return value
        if value:
            text = str(value)
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(text[:19], fmt)
                except Exception:
                    pass
        return datetime.utcnow()

    @staticmethod
    def _batch_insert_postgres(events: list):
        rows = []
        for e in events or []:
            rows.append(CrawlerEventLog(
                event_type=str(e.get("event_type", ""))[:32],
                username=str(e.get("username", ""))[:64],
                asin=str(e.get("asin", ""))[:32],
                country=str(e.get("country", "")).upper()[:10],
                page=int(e.get("page", 0) or 0),
                http_status=int(e.get("http_status", 0) or 0),
                daily_pages=int(e.get("daily_pages", 0) or 0),
                session_seq=int(e.get("session_seq", 0) or 0),
                worker_id=str(e.get("worker_id", ""))[:128],
                proxy=str(e.get("proxy", ""))[:512],
                error_msg=str(e.get("error_msg", ""))[:2000],
                extra=e.get("extra") or {},
                created_at=EventLogConsumer._parse_created_at(e.get("created_at")),
            ))
        if not rows:
            return
        with session_scope() as s:
            s.add_all(rows)
        logger.debug(f"[EventLogConsumer] 写入 {len(rows)} 条事件")

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
            logger.warning(f"[EventLogConsumer] MySQL 连接不可用，跳过 MySQL 日志落库: {exc}")
            return None

    @staticmethod
    def _ensure_table(cursor, conn):
        ddl = """
        CREATE TABLE IF NOT EXISTS `crawler_event_log` (
            `id`           BIGINT NOT NULL AUTO_INCREMENT,
            `event_type`   VARCHAR(32)  NOT NULL COMMENT '事件类型',
            `username`     VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '账号',
            `asin`         VARCHAR(32)  NOT NULL DEFAULT '' COMMENT 'ASIN',
            `country`      VARCHAR(10)  NOT NULL DEFAULT '' COMMENT '国家编码',
            `page`         INT          NOT NULL DEFAULT 0  COMMENT '当前页码',
            `http_status`  INT          NOT NULL DEFAULT 0  COMMENT 'HTTP 状态码',
            `daily_pages`  INT          NOT NULL DEFAULT 0  COMMENT '账号今日已抓页数',
            `session_seq`  INT          NOT NULL DEFAULT 0  COMMENT '账号会话序号',
            `worker_id`    VARCHAR(64)  NOT NULL DEFAULT '' COMMENT 'worker 进程标识',
            `proxy`        VARCHAR(128) NOT NULL DEFAULT '' COMMENT '代理标识（IP/zone）',
            `error_msg`    VARCHAR(512) NOT NULL DEFAULT '' COMMENT '错误摘要',
            `extra`        JSON                  NULL       COMMENT '扩展字段',
            `created_at`   DATETIME     NOT NULL            COMMENT '事件发生时间',
            PRIMARY KEY (`id`),
            KEY `idx_el_event_type`  (`event_type`),
            KEY `idx_el_username`    (`username`),
            KEY `idx_el_asin`        (`asin`),
            KEY `idx_el_country`     (`country`),
            KEY `idx_el_created_at`  (`created_at`),
            KEY `idx_el_username_date` (`username`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫结构化事件日志（风控分析）';
        """
        cursor.execute(ddl)
        conn.commit()

    @staticmethod
    def _batch_insert(cursor, conn, events: list):
        sql = """
            INSERT INTO crawler_event_log
            (event_type, username, asin, country, page, http_status,
             daily_pages, session_seq, worker_id, proxy, error_msg, extra, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CAST(%s AS JSON),%s)
        """
        rows = []
        for e in events:
            rows.append((
                e.get("event_type", ""),
                e.get("username", ""),
                e.get("asin", ""),
                e.get("country", ""),
                int(e.get("page", 0)),
                int(e.get("http_status", 0)),
                int(e.get("daily_pages", 0)),
                int(e.get("session_seq", 0)),
                e.get("worker_id", ""),
                e.get("proxy", ""),
                e.get("error_msg", "")[:500],
                json.dumps(e.get("extra") or {}, ensure_ascii=False),
                e.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ))
        try:
            cursor.executemany(sql, rows)
            conn.commit()
            logger.debug(f"[EventLogConsumer] 写入 {len(rows)} 条事件")
        except Exception:
            conn.rollback()
            logger.error(f"[EventLogConsumer] 批量写入失败:\n{traceback.format_exc()}")
