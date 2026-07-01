"""
压测专用账号管理器
"""
import random
import threading
import time
from datetime import date
from typing import Optional

from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL, STRESS_TEST_SCHEDULE
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)

# Redis key：记录压测起始日期，用于计算测试第几天
STRESS_START_DATE_KEY = "stress_test:start_date"


class StressTestAccountManager:
    """
    压测专用账号管理器。

    与 HumanLikeAccountManager 接口完全兼容，但去掉所有仿人行为限制：
      - 无强制休息时间、无疲劳分、无冷却
      - 保留每日任务上限，且每天自动递增（找封号阈值的核心机制）
      - session 粘性：同一 worker 连续使用同一账号 SESSION_MIN~MAX 个任务后轮换

    每日上限由 STRESS_TEST_SCHEDULE 档位表决定，test_day 由
    Redis key stress_test:start_date 推算，第一次启动时自动写入。

    ❯ 重置压测：
      redis-cli DEL stress_test:start_date
      redis-cli --scan --pattern "acc_day:*" | xargs redis-cli DEL
    """

    # session 粘性：连续使用同一账号的任务数范围
    SESSION_MIN_TASKS = 7
    SESSION_MAX_TASKS = 10

    # NX 锁初始 TTL（秒）：由心跳线程续期，进程崩溃后最多 90s 自动解锁
    _LOCK_TTL = 90
    # 心跳续期间隔（秒）：必须 < _LOCK_TTL
    _HEARTBEAT_INTERVAL = 30

    def __init__(self, worker_id: str):
        import redis as redis_lib
        self.worker_id = worker_id
        self.platform = "amazon"
        self.account_label = STRESS_TEST_LABEL
        self._redis = redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        self._mysql = MySQLTaskDB()
        self._current_account = None    # 当前持有的 Account 对象
        self._session_tasks = 0          # 当前 session 已跑任务数
        self._session_budget = 0         # 当前 session 预算（7~10）
        self._account_pool: list = []    # 缓存的账号列表（dict）
        self._pool_loaded_at: float = 0.0
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    # ── 心跳续期 ──────────────────────────────────────────────────────────

    def _start_heartbeat(self, username: str):
        """启动心跳线程，每 _HEARTBEAT_INTERVAL 秒续期 NX 锁 TTL"""
        self._stop_heartbeat()
        self._heartbeat_stop.clear()

        def _beat():
            while not self._heartbeat_stop.wait(timeout=self._HEARTBEAT_INTERVAL):
                try:
                    key = self._inuse_key(username)
                    if self._redis.get(key) == self.worker_id:
                        self._redis.expire(key, self._LOCK_TTL)
                except Exception as e:
                    logger.debug(f"[压测] 心跳续期失败（非致命）: {e}")

        self._heartbeat_thread = threading.Thread(
            target=_beat, daemon=True, name=f"stress-hb-{self.worker_id}")
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        """停止心跳线程"""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        self._heartbeat_thread = None

    # ── 公开接口（与 HumanLikeAccountManager 兼容）────────────────────────

    def get_account(self, filter_conditions=None):
        """返回当前账号（如仍可用且未超 session 预算）或切换到下一个可用账号"""
        country = (filter_conditions or {}).get("country")

        if self._current_account is not None:
            acc = self._current_account
            if self._session_tasks >= self._session_budget:
                logger.info(f"[压测] worker={self.worker_id} 账号={acc.username} "
                            f"完成 {self._session_tasks} 个任务，轮换账号")
                self._release_claim(acc.username)
                self._current_account = None
            elif not self._account_ok(acc, country):
                self._release_claim(acc.username)
                self._current_account = None
            else:
                # 会话中途校验锁是否仍归本 worker（防止心跳挂掉后锁过期被其他 worker 抢走）
                try:
                    held = self._redis.get(self._inuse_key(acc.username))
                except Exception:
                    held = self.worker_id  # Redis 不可用时保守继续，不中断会话
                if held != self.worker_id:
                    # 锁已丢失，尝试重新抢占；失败则放弃本账号
                    if self._try_claim_account(acc.username):
                        logger.warning(f"[压测] worker={self.worker_id} 账号={acc.username} "
                                       f"锁已过期，重新占用成功，会话继续")
                    else:
                        logger.warning(f"[压测] worker={self.worker_id} 账号={acc.username} "
                                       f"锁已被其他 worker 抢走，中断 session 切换账号")
                        self._current_account = None

        if self._current_account is None:
            max_retries = 100
            for attempt in range(1, max_retries + 1):
                self._current_account = self._pick_account(country)
                if self._current_account:
                    break
                logger.warning(f"[压测] worker={self.worker_id} 无可用账号，等待重试 ({attempt}/{max_retries})")
                time.sleep(3)
            self._session_tasks = 0
            self._session_budget = random.randint(self.SESSION_MIN_TASKS, self.SESSION_MAX_TASKS)
            if self._current_account:
                logger.info(f"[压测] worker={self.worker_id} 使用账号={self._current_account.username} "
                            f"session 预算={self._session_budget}，今日上限={self._daily_limit()}")
            else:
                logger.error(f"[压测] worker={self.worker_id} 重试 {max_retries} 次后仍无可用账号")

        return self._current_account

    def has_configured_account(self, filter_conditions=None) -> bool:
        """账号库里是否存在匹配国家的压测账号，不判断当前是否可用。"""
        conditions = dict(filter_conditions or {})
        conditions["label"] = STRESS_TEST_LABEL
        conditions.setdefault("platform", self.platform)
        return self._mysql.count_accounts_by_filter(conditions, active_only=False) > 0

    def release_account(self, account, asin: str, success: bool, task_id: str, pages_fetched: int = 0):
        """任务完成后更新 session 计数和 Redis 日统计"""
        if not account:
            return
        self._session_tasks += 1
        username = account.username
        today = date.today().isoformat()
        key = f"acc_day:{username}:{today}"
        try:
            self._redis.hincrby(key, "task_count", 1)
            if pages_fetched > 0:
                self._redis.hincrby(key, "page_count", pages_fetched)
            # 记录最后使用时间戳，供 _pick_account 排序（最久未使用优先）
            self._redis.hset(key, "last_used_ts", int(time.time()))
            self._redis.expire(key, 48 * 3600)
        except Exception as e:
            logger.warning(f"[压测] Redis 更新日统计失败: {e}")

        if self._session_tasks >= self._session_budget:
            self._release_claim(username)

    def force_release(self):
        """释放当前会话（进程退出时调用）"""
        if self._current_account:
            self._release_claim(self._current_account.username)
        self._current_account = None
        self._session_tasks = 0

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _get_test_day(self) -> int:
        """返回今天是测试第几天（从1开始），第一次调用时自动记录起始日期"""
        try:
            start_str = self._redis.get(STRESS_START_DATE_KEY)
            if not start_str:
                today_str = date.today().isoformat()
                self._redis.set(STRESS_START_DATE_KEY, today_str)
                return 1
            return (date.today() - date.fromisoformat(start_str)).days + 1
        except Exception:
            return 1

    def _daily_limit(self) -> int:
        """根据档位表查今天的任务上限"""
        test_day = self._get_test_day()
        elapsed = 0
        for limit, days in STRESS_TEST_SCHEDULE:
            if days == 0:
                return limit
            elapsed += days
            if test_day <= elapsed:
                return limit
        return STRESS_TEST_SCHEDULE[-1][0]

    def _day_task_count(self, username: str) -> int:
        """从 Redis 读取账号今日已执行任务数"""
        key = f"acc_day:{username}:{date.today().isoformat()}"
        try:
            return int(self._redis.hget(key, "task_count") or 0)
        except Exception:
            return 0

    def _get_last_used_ts(self, username: str) -> int:
        """从 Redis 读取账号最后使用时间戳，未使用过的账号返回 0（优先被选中）"""
        key = f"acc_day:{username}:{date.today().isoformat()}"
        try:
            return int(self._redis.hget(key, "last_used_ts") or 0)
        except Exception:
            return 0

    def _account_ok(self, account, country=None) -> bool:
        """判断账号是否仍可继续使用"""
        try:
            row = self._mysql.get_account_by_username(account.username)
            if not row or row.get("state") == -1:
                logger.warning(f"[压测] 账号 {account.username} 已封禁，跳过")
                return False
        except Exception:
            pass
        if country and account.country.upper() != country.upper():
            return False
        if self._day_task_count(account.username) >= self._daily_limit():
            logger.info(f"[压测] 账号 {account.username} 今日任务已达上限 {self._daily_limit()}，切换")
            return False
        return True

    def _load_pool(self):
        """加载/刷新压测账号列表（每 5 分钟刷新一次）"""
        now = time.time()
        if self._account_pool and now - self._pool_loaded_at < 300:
            return
        try:
            rows = self._mysql.load_all_accounts({"label": STRESS_TEST_LABEL})
            self._account_pool = rows or []
            self._pool_loaded_at = now
        except Exception as e:
            logger.warning(f"[压测] 加载账号池失败: {e}")

    def _inuse_key(self, username: str) -> str:
        return f"stress:inuse:{username}"

    def _try_claim_account(self, username: str) -> bool:
        """尝试 Redis SET NX 占用账号，成功则启动心跳续期线程"""
        try:
            ok = bool(self._redis.set(
                self._inuse_key(username), self.worker_id,
                nx=True, ex=self._LOCK_TTL,
            ))
            if ok:
                self._start_heartbeat(username)
            return ok
        except Exception as e:
            logger.warning(f"[压测] Redis 占用账号失败，降级允许使用: {e}")
            return True  # Redis 不可用时降级，不阻塞压测

    def _release_claim(self, username: str):
        """停止心跳并释放账号占用锁（仅释放本 worker 持有的）"""
        self._stop_heartbeat()
        try:
            key = self._inuse_key(username)
            if self._redis.get(key) == self.worker_id:
                self._redis.delete(key)
        except Exception as e:
            logger.warning(f"[压测] Redis 释放账号占用失败: {e}")

    def _pick_account(self, country=None):
        """
        从账号池中选出当日未达上限且未封的账号，优先选最久未使用的账号。
        Redis NX 防止多进程重复选中同一账号。
        """
        from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
        self._load_pool()
        daily_limit = self._daily_limit()

        # 第一步：收集候选账号并读取最后使用时间戳
        candidates = []
        for row in self._account_pool:
            if row.get("state") == -1:
                continue
            if country and row.get("country", "").upper() != country.upper():
                continue
            username = row["username"]
            if self._day_task_count(username) >= daily_limit:
                continue
            candidates.append((self._get_last_used_ts(username), username, row))

        if not candidates:
            return None

        # 第二步：按最后使用时间升序（0=从未使用今天，优先级最高）
        candidates.sort(key=lambda x: x[0])

        # 第三步：按顺序尝试 NX 加锁
        for last_ts, username, row in candidates:
            if not self._try_claim_account(username):
                logger.debug(f"[压测] 账号 {username} 已被其他进程占用，跳过")
                continue
            logger.debug(f"[压测] 选中账号 {username}，last_used_ts={last_ts}")
            return Account.from_dict(row)

        return None
