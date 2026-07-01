"""
通用 Cookie 刷新调度器。

用法：
    refresher = CookieRefresher(mysql_db, redis_client)
    refresher.register_handler(TwitterLoginHandler())
    refresher.start()

    # Worker 遇到 401 时：
    event = refresher.request_refresh(username, platform="twitter")
    event.wait(timeout=120)
    # 然后从 DB 重新读取 cookies 重试
"""
import json
import queue
import threading
import time
from typing import Dict

from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account

_REDIS_LOCK_TTL = 300
_MAX_RETRY = 3


class CookieRefresher:
    """
    平台无关的 Cookie 刷新调度器。
    每个注册平台独立一个后台线程，互不阻塞。
    """

    def __init__(self, mysql_db, redis_client):
        self._mysql_db = mysql_db
        self._redis = redis_client
        self._handlers: Dict = {}
        self._queues: Dict[str, queue.Queue] = {}
        self._pending_events: Dict[str, threading.Event] = {}
        self._pending_lock = threading.Lock()
        self._threads: Dict[str, threading.Thread] = {}

    def register_handler(self, handler):
        """注册平台登录实现。必须在 start() 前调用。"""
        platform = handler.platform
        self._handlers[platform] = handler
        self._queues[platform] = queue.Queue()
        logger.info(f"[CookieRefresher] 注册平台: {platform}")

    def start(self):
        """为每个已注册平台启动一个后台刷新线程。"""
        for platform, q in self._queues.items():
            t = threading.Thread(
                target=self._refresh_loop,
                args=(platform, q),
                name=f"cookie-refresher-{platform}",
                daemon=True,
            )
            t.start()
            self._threads[platform] = t
            logger.info(f"[CookieRefresher] 启动刷新线程: {platform}")

    def request_refresh(self, username: str, platform: str) -> threading.Event:
        """
        请求刷新指定账号的 Cookie。
        同一账号重复调用返回同一个 Event（进程内去重）。
        返回的 Event 在刷新完成后被 set。
        """
        key = f"{platform}:{username}"
        with self._pending_lock:
            if key in self._pending_events:
                logger.debug(f"[CookieRefresher] 已有刷新请求: {key}，复用 Event")
                return self._pending_events[key]
            event = threading.Event()
            self._pending_events[key] = event

        if platform in self._queues:
            self._queues[platform].put(username)
        else:
            logger.error(f"[CookieRefresher] 未注册平台: {platform}")
            event.set()
        return event

    def _refresh_loop(self, platform: str, q: queue.Queue):
        """每平台独立后台线程的主循环。"""
        while True:
            try:
                username = q.get(timeout=5)
            except queue.Empty:
                continue
            key = f"{platform}:{username}"
            with self._pending_lock:
                event = self._pending_events.get(key)
            if event is None:
                event = threading.Event()
            self._do_refresh(platform, username, event)

    def _do_refresh(self, platform: str, username: str, event: threading.Event):
        """执行实际刷新逻辑（可单独调用，便于测试）。"""
        key = f"{platform}:{username}"
        redis_lock_key = f"cookie_refresh:{platform}:{username}"

        acquired = self._redis.set(redis_lock_key, "1", nx=True, ex=_REDIS_LOCK_TTL)
        if not acquired:
            logger.info(f"[CookieRefresher] 其他进程正在刷新 {key}，等待")
            time.sleep(10)
            event.set()
            self._clear_pending(key)
            return

        handler = self._handlers.get(platform)
        if handler is None:
            logger.error(f"[CookieRefresher] 无登录处理器: {platform}")
            event.set()
            self._clear_pending(key)
            self._redis.delete(redis_lock_key)
            return

        account_row = self._mysql_db.get_account_by_username(username, platform=platform)
        if not account_row:
            logger.error(f"[CookieRefresher] 账号不存在: {username}")
            event.set()
            self._clear_pending(key)
            self._redis.delete(redis_lock_key)
            return

        account = Account.from_dict(account_row)
        success = False
        for attempt in range(1, _MAX_RETRY + 1):
            try:
                logger.info(f"[CookieRefresher] 刷新 {key}（第{attempt}次）")
                new_cookies = handler.login(account)
                account.cookies = new_cookies
                account_dict = account.to_dict()
                account_dict["cookies"] = json.dumps(new_cookies)
                self._mysql_db.update_account(account_dict)
                logger.info(f"[CookieRefresher] 刷新成功: {key}")
                success = True
                break
            except Exception as e:
                logger.warning(f"[CookieRefresher] 刷新失败({attempt}/{_MAX_RETRY}): {key} - {e}")
                time.sleep(5 * attempt)

        if not success:
            logger.error(f"[CookieRefresher] 刷新彻底失败: {key}，禁用账号")
            self._mysql_db.update_account({"username": username, "platform": platform, "state": 0})
            try:
                from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
                send_custom_robot_group_message(f"[{platform}] 账号 {username} Cookie 刷新失败，已禁用")
            except Exception:
                pass

        event.set()
        self._clear_pending(key)
        self._redis.delete(redis_lock_key)

    def _clear_pending(self, key: str):
        with self._pending_lock:
            self._pending_events.pop(key, None)
