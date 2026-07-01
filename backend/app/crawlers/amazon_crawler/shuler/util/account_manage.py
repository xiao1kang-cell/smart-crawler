import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from threading import RLock

from loguru import logger
from retrying import retry

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import *
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.redis_ import RedisDistLock


# ------------------------------
# 4. 核心：账号调度管理器（MySQL 版）
# ------------------------------
class AccountManager:
    def __init__(self):
        self.mysql_db = MySQLTaskDB()
        self.redis_lock = RedisDistLock()
        self.accounts = self._load_accounts()
        self.local_lock = RLock()  # 线程锁

    def _load_accounts(self, filter_conditions: Optional[Dict[str, Any]] = None) -> list:
        """从 MySQL 加载账号并转为 Account 对象"""
        rows = self.mysql_db.load_all_accounts(filter_conditions)
        return [Account.from_dict(row) for row in rows]

    def reload_accounts(self, filter_conditions: Optional[Dict[str, Any]] = None):
        """热重载账号"""
        with self.local_lock:
            self.accounts = self._load_accounts(filter_conditions)

    def _save_account(self, account: Account):
        """将 Account 对象状态回写 MySQL"""
        self.mysql_db.update_account(account.to_dict())

    @retry(stop_max_attempt_number=40, wait_random_min=1000, wait_random_max=2000, )
    def get_account(self, filter_conditions: Optional[Dict[str, Any]] = None) -> Optional[Account]:
        """
         :param filter_conditions: 可选，自定义筛选条件字典（如 {"country": "US"} 等）
        获取符合条件的账号：
        1. 未被使用 2. 未冷却 3. 失败未超限 4. 频率未超限 5. 国家匹配 6. 最久未使用优先
        """
        # 分布式锁：全局唯一，避免多进程抢账号
        lock_key = "global_account_lock"
        if not self.redis_lock.acquire(lock_key, timeout=50):
            logger.warning("获取分布式锁失败，稍等")
            time.sleep(2)
            raise Exception('获取分布式锁失败,稍等')

        try:
            with self.local_lock:
                # 1. 预加载最新账号数据（避免内存数据过期）
                self.reload_accounts(filter_conditions)
                now = time.time()
                available = []
                released_count = 0

                # 计算超时阈值（当前时间 - 超时分钟数 → 时间戳）
                timeout_threshold_ts = (datetime.now() - timedelta(minutes=ACCOUNT_USED_MINUTES)).timestamp()

                for acc in self.accounts:
                    # 安全获取字段（兼容没有此字段的老账号）
                    is_used = acc.is_used if hasattr(acc, 'is_used') else False
                    last_used_ts = acc.last_used_time if hasattr(acc, 'last_used_time') else 0.0

                    # ========== 实时检测并释放超时账号 ==========
                    if (is_used
                            and last_used_ts > 0
                            and last_used_ts < timeout_threshold_ts
                    ):
                        # 强制释放超时账号
                        acc.is_used = False
                        acc.cooldown_until = 0.0
                        acc.update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._save_account(acc)
                        released_count += 1
                        logger.info(
                            f"【实时释放】超时账号：{acc.username} | 最后使用时间：{datetime.fromtimestamp(last_used_ts)} | 超时阈值：{datetime.fromtimestamp(timeout_threshold_ts)}")
                        is_used = False

                    # 过滤条件
                    if acc.state != 1:
                        continue
                    if is_used:
                        continue

                    if filter_conditions and acc.country.lower() != filter_conditions.get('country', '').lower():
                        continue

                    available.append(acc)

                if not available:
                    logger.info(f"[{filter_conditions}] 无可用账号")
                    return None

                # 最久未使用优先
                available.sort(key=lambda x: x.last_used_time)
                selected = available[0]

                # 更新账号状态
                selected.is_used = True
                selected.last_used_time = now
                self._save_account(selected)
                logger.info(f"分配账号：{selected.username} | 国家：{filter_conditions} | 指纹ID：{selected.fingerprint_id}")
                return selected
        finally:
            self.redis_lock.release(lock_key)

    def release_account(self, account: Account, asin: str, success: bool, task_id: str):
        """
        释放账号：
        - 成功：重置失败次数
        - 失败：累计失败次数，达到阈值则冷却
        """
        lock_key = f"account_lock_{account.username}"
        self.redis_lock.acquire(lock_key, 5)
        try:
            with self.local_lock:
                now = time.time()
                if success:
                    account.fail_count = 0
                    account.cooldown_until = 0.0
                else:
                    account.fail_count += 1
                    if account.fail_count >= MAX_FAIL:
                        account.cooldown_until = now + COOLDOWN_SECONDS
                        logger.warning(f"账号 {account.username} 失败{MAX_FAIL}次，进入冷却{COOLDOWN_SECONDS}秒")
                # 标记为未使用
                account.is_used = False
                account.update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._save_account(account)
                logger.info(f"释放账号：{account.username} | 执行结果：{'成功' if success else '失败'}")
        finally:
            self.redis_lock.release(lock_key)

if __name__ == '__main__':
    manager = AccountManager()
    account = manager.get_account({"country": "us"})
    if account:
        print("使用账号:", account.username)
        manager.release_account(account, "TEST_ASIN", True, "test_task")