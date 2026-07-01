"""
仿真人账号调度器 —— 替代原 account_manage.py 中的轮询机制

核心理念：让每个账号的使用模式看起来像真人浏览行为
  1. 会话粘性：一个 worker 拿到账号后连续使用 N 个任务（模拟一次浏览会话）
  2. 作息时间：账号有"活跃时段"，模拟真实用户的上网习惯
  3. 日预算：每个账号每天有使用上限，带随机波动
  4. 疲劳衰减：连续使用越久，休息概率越大
  5. 随机休息：每次会话结束后，账号进入随机时长的"离线"期
  6. 加权随机：不是确定性选择，而是概率性选择（偏好"当前活跃中"的账号）

用法：
    scheduler = AccountScheduler()
    account = scheduler.acquire(worker_id="w1", filter_conditions={"country": "US"})
    # ... 执行任务 ...
    scheduler.complete_task(worker_id="w1", success=True)
    # 下次调用 acquire 时，同一 worker 可能继续复用同一账号

    # 任务全部完成后释放
    scheduler.release(worker_id="w1")
"""
import json
import os
import random
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from typing import Optional, Dict, Any, List

import redis
from loguru import logger
from retrying import retry

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import (
    STRESS_TEST_LABEL, STRESS_TEST_REST_MIN_SECONDS, STRESS_TEST_REST_MAX_SECONDS,
)
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.redis_ import RedisDistLock
from app.crawlers.amazon_crawler.shuler.util.risk_policy import RiskPolicy, AMAZON_POLICY, get_policy
from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)
from app.crawlers.amazon_crawler.shuler.util.event_logger import push_event, EventType

# Redis key 前缀
REDIS_DAY_STATS_PREFIX = "acc_day"       # acc_day:{username}:{date}
REDIS_DAY_STATS_TTL = 48 * 3600          # 48 小时过期自动清理
REDIS_ACCOUNT_LOCK_PREFIX = "acc_lock"   # acc_lock:{username} - 账号分布式锁
REDIS_ACCOUNT_LOCK_TTL = 3600            # 账号锁默认过期时间（1小时，防止死锁）


class SchedulerLockTimeout(Exception):
    """账号调度分布式锁等待超时。"""


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return default


# ========== 调度策略参数 ==========
# 以下常量自 2026-05 起由 risk_policy.AMAZON_POLICY 提供平台特定值，
# 这里保留只是为了向后兼容外部 import（实际逻辑均走 self._policy_for(...)）。

# 会话粘性：一个 worker 连续使用同一账号的任务数范围
SESSION_MIN_TASKS = 8
SESSION_MAX_TASKS = 12

# 日预算：每个账号每天最多执行的任务数（带随机波动）
DAILY_BUDGET_BASE = 300
DAILY_BUDGET_JITTER = 5  # 实际预算 = base ± jitter

# 页面日预算：每个账号每天最多抓取的页面数（更精确的资源控制）
DAILY_PAGE_BUDGET_BASE = 400   # 基准页面数（每任务平均 10 页）
DAILY_PAGE_BUDGET_JITTER = 50

# 会话间休息时间（秒）—— 提升以降低风控风险
REST_MIN_SECONDS = 60 * 10   # 最少 40 分钟
REST_MAX_SECONDS = 60 * 20  # 最多 2 小时

# worker 切换国家打断短会话时，不按完整会话休息处理。
COUNTRY_SWITCH_SHORT_SESSION_TASKS = _env_int("COUNTRY_SWITCH_SHORT_SESSION_TASKS", 3)
COUNTRY_SWITCH_SHORT_SESSION_PAGES = _env_int("COUNTRY_SWITCH_SHORT_SESSION_PAGES", 20)
COUNTRY_SWITCH_REST_MIN_SECONDS = _env_int("COUNTRY_SWITCH_REST_MIN_SECONDS", 60)
COUNTRY_SWITCH_REST_MAX_SECONDS = _env_int("COUNTRY_SWITCH_REST_MAX_SECONDS", 120)
SCHEDULER_LOCK_TTL_SECONDS = _env_int("SCHEDULER_LOCK_TTL_SECONDS", 30, minimum=1)
SCHEDULER_LOCK_WAIT_SECONDS = _env_int("SCHEDULER_LOCK_WAIT_SECONDS", 10, minimum=1)
SCHEDULER_ACQUIRE_RETRY_ATTEMPTS = _env_int("SCHEDULER_ACQUIRE_RETRY_ATTEMPTS", 3, minimum=1)
SCHEDULER_ACQUIRE_RETRY_WAIT_MIN_MS = _env_int("SCHEDULER_ACQUIRE_RETRY_WAIT_MIN_MS", 500, minimum=0)
SCHEDULER_ACQUIRE_RETRY_WAIT_MAX_MS = _env_int("SCHEDULER_ACQUIRE_RETRY_WAIT_MAX_MS", 1500, minimum=0)
SCHEDULER_SELECT_SLOW_LOG_SECONDS = _env_int("SCHEDULER_SELECT_SLOW_LOG_SECONDS", 2, minimum=1)


# 疲劳衰减：连续使用 N 个任务后，每多用一个任务，休息概率增加
FATIGUE_THRESHOLD = 20     # 超过此数量后开始疲劳
FATIGUE_PROB_STEP = 0.15  # 每多一个任务，主动休息概率 +15%

# 活跃时段（按账号国家的本地时间，24h 制）
# 真人通常在 7:00~23:00 活跃，凌晨很少上网
ACTIVE_HOURS = {
    "default": (7, 23),   # 默认
    "US": (3, 24),        # 美国人熬夜多
    "UK": (7, 23),
    "DE": (7, 22),
    "JP": (8, 24),
    "CA": (8, 23),
    "FR": (7, 23),
    "IT": (8, 23),
    "ES": (9, 24),        # 西班牙人晚睡
    "AU": (7, 23),
    "IN": (7, 23),
    "MX": (6, 24),
}

# 国家 → UTC 偏移（小时），用于推算账号的"本地时间"
TIMEZONE_OFFSETS = {
    "US": -5, "UK": 0, "DE": 1, "JP": 9, "CA": -5,
    "FR": 1, "IT": 1, "ES": 1, "AU": 10, "IN": 5,
    "BR": -3, "MX": -6, "SG": 8, "SE": 1, "AE": 4,
    "NL": 1, "PL": 1, "BE": 1,
}


# ========== 会话状态 ==========

@dataclass
class WorkerSession:
    """单个 worker 的当前会话状态"""
    worker_id: str
    account: Optional[Account] = None
    tasks_in_session: int = 0       # 本次会话已完成的任务数
    pages_in_session: int = 0       # 本次会话已抓取页面数
    session_budget: int = 0         # 本次会话的任务上限
    session_start_time: float = 0   # 会话开始时间戳
    success_count: int = 0
    fail_count: int = 0
    account_acquired_time: float = 0  # 获取账号时的 last_used_time，用于检测账号是否被其他 worker 占用


@dataclass
class AccountDayStats:
    """账号当日使用统计（内存缓存，每天重置）"""
    date: str = ""             # YYYY-MM-DD
    task_count: int = 0        # 当日已执行任务数
    page_count: int = 0        # 当日已抓取页面数（比 task_count 更准确的资源指标）
    daily_budget: int = 0      # 当日任务预算
    daily_page_budget: int = 0 # 当日页面预算
    quota_factor_applied: float = 1.0  # 当日已应用的配额因子（同日只降不升）
    last_session_end: float = 0  # 上次会话结束时间戳
    rest_until: float = 0      # 休息到什么时候（时间戳）
    session_seq: int = 0       # 今日会话序号（每次开新会话 +1，用于事件日志关联）


class AccountScheduler:
    """
    仿真人账号调度器。
    替代原 AccountManager.get_account() 的轮询机制。
    """

    def __init__(self):
        self.mysql_db = MySQLTaskDB()
        self.redis_lock = RedisDistLock()
        self.local_lock = RLock()

        # Redis 客户端（用于持久化日统计）
        self.redis = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )

        # worker_id → WorkerSession
        self._sessions: Dict[str, WorkerSession] = {}

        # username → AccountDayStats（内存缓存，Redis 持久化）
        self._day_stats: Dict[str, AccountDayStats] = {}

    def _get_session_key(self, worker_id: str, country: str = None) -> str:
        """每个 worker 只维护一个 session，不区分国家（国家切换时由 acquire 内的不匹配检测结束旧 session）。"""
        return worker_id

    def _policy_for(self, account: Optional[Account] = None,
                    filter_conditions: Optional[Dict[str, Any]] = None) -> RiskPolicy:
        """
        查找当前操作适用的风控策略。
        优先级：account.platform > filter_conditions['platform'] > AMAZON_POLICY。

        所有疲劳/冷却/会话/日预算的判断都应该通过这个方法拿 policy，
        而不是直接 import 模块级常量——后者只是向后兼容用的。
        """
        if account is not None:
            platform = getattr(account, "platform", None)
            if platform:
                return get_policy(platform)
        if filter_conditions:
            platform = filter_conditions.get("platform")
            if platform:
                return get_policy(platform)
        return AMAZON_POLICY

    # ========================== 核心接口 ==========================
    @retry(
        stop_max_attempt_number=SCHEDULER_ACQUIRE_RETRY_ATTEMPTS,
        wait_random_min=SCHEDULER_ACQUIRE_RETRY_WAIT_MIN_MS,
        wait_random_max=SCHEDULER_ACQUIRE_RETRY_WAIT_MAX_MS,
    )
    def acquire(self, worker_id: str,
                filter_conditions: Optional[Dict[str, Any]] = None) -> Optional[Account]:
        """
        为指定 worker 获取账号。

        如果 worker 当前有活跃会话且未用完，直接返回同一账号（会话粘性）。
        否则选择新账号并开始新会话。

        :param worker_id: 工作进程/线程标识
        :param filter_conditions: 筛选条件（如 {"country": "US"}）
        :return: Account 对象，或 None（无可用账号）
        """
        # 从 filter_conditions 中提取国家
        country = filter_conditions.get("country") if filter_conditions else None
        session_key = self._get_session_key(worker_id, country)

        with self.local_lock:
            session = self._sessions.get(session_key)
            # 检查是否可以复用当前会话（必须国家匹配）
            if session and session.account:
                # 检查 session 的国家是否匹配当前任务的国家
                session_country = getattr(session.account, 'country', None)
                if country and session_country and session_country.upper() != country.upper():
                    # 国家不匹配，结束当前 session，为当前国家创建新 session
                    logger.info(
                        f"[调度] worker={worker_id} 国家变更 {session_country} → {country}，"
                        f"结束旧会话并创建新会话"
                    )
                    self._end_session(session, reason="country_switch")
                    session = None
                elif self._can_continue_session(session):
                    logger.info(
                        f"[调度] worker={worker_id} 复用账号 {session.account.username} "
                        f"(国家={session_country}, 会话 {session.tasks_in_session}/{session.session_budget})"
                    )
                    return session.account
                else:
                    # 会话结束，释放当前账号
                    self._end_session(session)

        # 需要分配新账号（按平台+国家分锁，减少跨平台/跨国竞争）
        platform = (filter_conditions.get("platform") or "amazon") if filter_conditions else "amazon"
        lock_key = f"scheduler_account_lock:{platform}:{(country or 'all').lower()}"
        lock_wait_started = time.monotonic()
        if not self.redis_lock.acquire(
                lock_key,
                timeout=SCHEDULER_LOCK_TTL_SECONDS,
                blocking=True,
                wait_timeout=SCHEDULER_LOCK_WAIT_SECONDS,
                retry_interval=0.05,
                jitter=0.35,
        ):
            elapsed = time.monotonic() - lock_wait_started
            logger.warning(
                f"[调度] 获取分布式锁失败 lock={lock_key} "
                f"wait={elapsed:.1f}s ttl={SCHEDULER_LOCK_TTL_SECONDS}s"
            )
            raise SchedulerLockTimeout('获取分布式锁失败,稍等')

        lock_acquired_at = time.monotonic()
        try:
            with self.local_lock:
                select_started = time.monotonic()
                account = self._select_account(filter_conditions)
                select_elapsed = time.monotonic() - select_started
                if select_elapsed >= SCHEDULER_SELECT_SLOW_LOG_SECONDS:
                    logger.warning(
                        f"[调度] 选号耗时较长 {select_elapsed:.2f}s "
                        f"worker={worker_id} filter={filter_conditions}"
                    )
                if not account:
                    logger.info(f"[调度] 无可用账号: {filter_conditions}")
                    return None

                # 创建新会话
                # 非活跃时段缩短会话（模拟凌晨偶尔刷一会儿就走）
                policy = self._policy_for(account=account)
                active_w = self._get_active_hours_weight(account.country)
                if active_w >= 1.0:
                    # 活跃时段：正常会话长度
                    session_budget = random.randint(policy.session_min_tasks, policy.session_max_tasks)
                elif active_w >= 0.7:
                    # 边缘时段（早起/晚睡）：偏短会话
                    session_budget = random.randint(max(1, policy.session_min_tasks - 1), max(2, policy.session_max_tasks // 2))
                else:
                    # 深夜：只做 1~2 个任务就"睡了"
                    session_budget = random.randint(1, 2)

                # 记录当前时间作为获取时间戳
                acquired_time = time.time()

                stats = self._get_day_stats(account.username, platform=getattr(account, "platform", None))
                stats.session_seq += 1
                self._save_day_stats(account.username, stats)

                session = WorkerSession(
                    worker_id=worker_id,
                    account=account,
                    tasks_in_session=0,
                    session_budget=session_budget,
                    session_start_time=time.time(),
                    account_acquired_time=acquired_time,  # 记录获取账号时的 last_used_time
                )
                self._sessions[session_key] = session

                # 标记账号为使用中
                account.is_used = True
                account.last_used_time = acquired_time
                self._save_account(account)

                # 发射会话开始事件
                try:
                    push_event(
                        self.redis, EventType.SESSION_START,
                        username=account.username,
                        country=country or "",
                        worker_id=worker_id,
                        daily_pages=stats.page_count,
                        session_seq=stats.session_seq,
                        extra={"session_budget": session_budget},
                    )
                except Exception:
                    pass

                logger.info(
                    f"[调度] worker={worker_id} (国家={country}) 分配账号 {account.username} "
                    f"(会话预算={session_budget}个任务, 今日会话序号={stats.session_seq})"
                )
                return account
        finally:
            held = time.monotonic() - lock_acquired_at
            if held >= SCHEDULER_SELECT_SLOW_LOG_SECONDS:
                logger.warning(
                    f"[调度] 分布式锁持有较久 {held:.2f}s lock={lock_key} "
                    f"worker={worker_id} filter={filter_conditions}"
                )
            self.redis_lock.release(lock_key)

    def complete_task(self, worker_id: str, success: bool, asin: str = "",
                       account: Optional[Account] = None, pages_fetched: int = 0):
        """
        worker 完成一个任务后调用。
        更新会话计数和账号状态。

        :param worker_id: worker 标识
        :param success: 任务是否成功
        :param asin: ASIN
        :param account: 账号对象（用于确定国家，生成正确的 session_key）
        :param pages_fetched: 本次任务实际抓取的页面数（用于页面预算统计）
        """
        # 从 account 或 session 确定国家
        country = None
        if account:
            country = getattr(account, 'country', None)

        session_key = self._get_session_key(worker_id, country)

        with self.local_lock:
            session = self._sessions.get(session_key)
            if not session or not session.account:
                return

            session.tasks_in_session += 1
            if pages_fetched > 0:
                session.pages_in_session += pages_fetched
            if success:
                session.success_count += 1
            else:
                session.fail_count += 1

            # 更新当日统计（含页面数）
            stats = self._get_day_stats(session.account.username, platform=getattr(session.account, "platform", None))
            stats.task_count += 1
            if pages_fetched > 0:
                stats.page_count += pages_fetched
            self._save_day_stats(session.account.username, stats)

            # 更新数据库中的 last_used_time（心跳，防止超时误释放）
            current_time = time.time()
            session.account.last_used_time = current_time
            # 关键：同步更新 session 的 account_acquired_time，防止下次检查时误判为被其他 worker 占用
            session.account_acquired_time = current_time
            if not success:
                session.account.fail_count += 1
                policy = self._policy_for(account=session.account)
                if session.account.fail_count >= policy.max_fail:
                    session.account.cooldown_until = time.time() + policy.cooldown_seconds
                    logger.warning(
                        f"[调度] 账号 {session.account.username} 失败{policy.max_fail}次，冷却{policy.cooldown_seconds}秒"
                    )
                    # 发射冷却事件
                    try:
                        push_event(
                            self.redis, EventType.ACCOUNT_COOLDOWN,
                            username=session.account.username,
                            asin=asin,
                            country=country or "",
                            worker_id=worker_id,
                            daily_pages=stats.page_count,
                            session_seq=stats.session_seq,
                            error_msg=f"fail_count={session.account.fail_count} cooldown={policy.cooldown_seconds}s",
                        )
                    except Exception:
                        pass
                    self._end_session(session)
            else:
                session.account.fail_count = 0
                # 成功时：重置 ban_analyzer 异常计数，逐步恢复配额
                try:
                    from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import (
                        reset_account_error, restore_account_quota,
                    )
                    reset_account_error(session.account.username, self.redis)
                    restore_account_quota(session.account.username, self.redis)
                except Exception:
                    pass

            self._save_account(session.account)

            # 疲劳检测：连续使用后有概率主动结束会话
            # if self._should_fatigue_rest(session):
            #     logger.info(
            #         f"[调度] 账号 {session.account.username} 疲劳休息 "
            #         f"(连续使用 {session.tasks_in_session} 个任务)"
            #     )
            #     self._end_session(session)

    def release(self, worker_id: str, success: bool = True, country: str = None):
        """
        worker 全部任务完成后主动释放账号。

        :param worker_id: worker 标识
        :param success: 是否成功
        :param country: 国家（如果指定，只释放该国家的 session；否则释放所有）
        """
        with self.local_lock:
            if country:
                # 释放特定国家的 session
                session_key = self._get_session_key(worker_id, country)
                session = self._sessions.get(session_key)
                if session:
                    self._end_session(session)
            else:
                # 释放该 worker 的所有 session（所有国家）
                sessions_to_end = []
                for key, session in self._sessions.items():
                    if key.startswith(f"{worker_id}:") or key == worker_id:
                        sessions_to_end.append(session)
                for session in sessions_to_end:
                    self._end_session(session)

    def get_current_account(self, worker_id: str, country: str = None) -> Optional[Account]:
        """
        获取 worker 当前持有的账号（不触发新分配）。

        :param worker_id: worker 标识
        :param country: 国家（如果指定，获取该国家的 session）
        """
        session_key = self._get_session_key(worker_id, country)
        session = self._sessions.get(session_key)
        return session.account if session else None

    # ========================== 账号选择策略 ==========================

    def _select_account(self, filter_conditions: Optional[Dict] = None) -> Optional[Account]:
        """按 last_used_time 从久到近选择第一个通过风控校验的账号。"""
        # 多进程场景下，默认隔离级别 REPEATABLE READ 会导致读到旧快照（其他进程已写入 is_used=1
        # 但本连接事务还没刷新），与分布式锁配合使用时会出现多进程选中同一账号的竞态。
        # 先 commit 结束当前隐式事务，让后续读走最新已提交数据。
        try:
            self.mysql_db.conn.commit()
        except Exception:
            pass

        policy = self._policy_for(filter_conditions=filter_conditions)
        now = time.time()
        timeout_threshold = (datetime.now() - timedelta(minutes=policy.account_used_minutes)).timestamp()
        try:
            released = self.mysql_db.release_timeout_accounts_by_filter(
                filter_conditions,
                timeout_threshold_ts=timeout_threshold,
            )
            if released:
                logger.info(f"[调度] 批量释放超时账号 {released} 个: {filter_conditions}")
        except Exception:
            logger.warning(f"[调度] 批量释放超时账号失败: {traceback.format_exc()[:500]}")

        def _pick_first_available(rows: List[Dict]) -> Optional[Account]:
            accounts = [Account.from_dict(row) for row in rows]

            # 从 Redis 快速读取最近被其他进程标记为封号的账号（每账号独立24h TTL）
            try:
                pipe = self.redis.pipeline()
                for acc in accounts:
                    pipe.exists(f'crawler:banned:{acc.username}')
                _ban_flags = pipe.execute()
                _redis_banned = {acc.username for acc, flag in zip(accounts, _ban_flags) if flag}
            except Exception:
                _redis_banned = set()

            for acc in accounts:
                # 实时封号广播：其他进程通过 Redis SET 标记的封号账号，立即跳过
                if acc.username in _redis_banned:
                    logger.debug(f"[调度] 跳过 Redis 广播封号账号: {acc.username}")
                    continue

                if acc.is_used:
                    continue

                # 冷却中
                if acc.cooldown_until > now:
                    continue

                # 休息中（会话间休息）
                stats = self._get_day_stats(acc.username, platform=getattr(acc, "platform", None))
                if stats.rest_until > now:
                    continue

                # 日预算耗尽（任务数 或 页面数，任一超上限则跳过）
                if stats.task_count >= stats.daily_budget:
                    continue
                if stats.daily_page_budget > 0 and stats.page_count >= stats.daily_page_budget:
                    continue

                return acc
            return None

        rows = self.mysql_db.load_available_account_candidates(
            filter_conditions,
            now_ts=now,
            limit=0,
        )
        account = _pick_first_available(rows)
        if account:
            return account

        return None

    def has_configured_account(self, filter_conditions: Optional[Dict] = None) -> bool:
        """账号库里是否存在匹配 country/platform/label 的账号，不判断当前是否可用。"""
        try:
            self.mysql_db.conn.commit()
        except Exception:
            pass
        return self.mysql_db.count_accounts_by_filter(filter_conditions, active_only=False) > 0

    # ========================== 会话管理 ==========================

    def _can_continue_session(self, session: WorkerSession) -> bool:
        """判断当前会话是否可以继续"""
        if not session.account:
            return False

        acc = session.account

        # 超过会话预算
        if session.tasks_in_session >= session.session_budget:
            return False
        # 会话页面上限（默认按每任务5页估算）
        if session.pages_in_session >= session.session_budget * 5:
            return False

        # 账号状态异常（内存快照检查，可能过时，后续 fresh_account 会再次校验）
        if acc.state != 1:
            return False

        # Redis 封禁广播：_disable_account 会将账号写入 crawler:banned:{username}（24h TTL）
        try:
            if self.redis.exists(f'crawler:banned:{acc.username}'):
                logger.warning(f"[调度] 账号 {acc.username} 已在 Redis 封禁集合，结束 session")
                return False
        except Exception:
            pass

        # 关键检查：从数据库重新加载账号状态，确认账号仍被本 worker 占用
        # 防止其他 worker 通过超时释放后重新获取该账号，导致两个 worker 同时使用同一账号
        try:
            fresh_account = self.mysql_db.get_account_by_username(
                acc.username,
                platform=getattr(acc, "platform", "amazon"),
            )
            if not fresh_account:
                logger.warning(f"[调度] 账号 {acc.username} 在数据库中不存在，结束 session")
                return False

            # 检查账号是否仍被标记为使用中（fresh_account 是 dict 类型）
            if not fresh_account.get('is_used', False):
                logger.info(
                    f"[调度] 账号 {acc.username} 已被释放（is_used=False），"
                    f"worker={session.worker_id} 的 session 结束"
                )
                return False

            # 检查账号 state 是否仍然有效（其他进程 / _disable_account 可能已将 state 设为 0）
            db_state = fresh_account.get('state', 1)
            if db_state != 1:
                logger.warning(
                    f"[调度] 账号 {acc.username} 已被停用 (state={db_state})，"
                    f"worker={session.worker_id} 的 session 强制结束"
                )
                # 同步内存对象，避免后续误判
                acc.state = db_state
                return False

            # 核心检查：比较 session 中记录的获取时间戳和数据库中的 last_used_time
            # 如果不同，说明其他 worker 获取并更新了该账号
            # 允许 1 秒的误差（浮点数精度问题）
            db_last_used = fresh_account.get('last_used_time', 0)
            time_diff = abs(float(db_last_used) - session.account_acquired_time)
            if time_diff > 1.0:
                logger.info(
                    f"[调度] 账号 {acc.username} 已被其他 worker 占用 "
                    f"(时间差={time_diff:.1f}s)，worker={session.worker_id} 的 session 结束"
                )
                return False

            # 更新内存中的对象状态（保持时间戳一致）
            acc.is_used = True
            acc.last_used_time = session.account_acquired_time
            # 同步 cookies（reviews.py 可能在执行过程中刷新了 cookies 并写回 MySQL）
            try:
                if fresh_account.get('cookies'):
                    acc.cookies = json.loads(fresh_account['cookies'])
            except:
                pass
        except Exception as e:
            logger.warning(f"[调度] 重新加载账号 {acc.username} 状态失败: {e}，保守起见结束 session")
            return False

        # 日预算耗尽（任务数 或 页面数）
        stats = self._get_day_stats(acc.username, platform=getattr(acc, "platform", None))
        if stats.task_count >= stats.daily_budget:
            return False
        if stats.daily_page_budget > 0 and stats.page_count >= stats.daily_page_budget:
            return False

        # 会话时间过长（超过1小时强制结束）
        if time.time() - session.session_start_time > 3600:
            return False

        return True

    def _end_session(self, session: WorkerSession, reason: str = "normal"):
        """结束会话，释放账号，设置休息时间"""
        if not session.account:
            return

        acc = session.account
        username = acc.username
        is_country_switch_short_session = (
            reason == "country_switch"
            and session.tasks_in_session <= COUNTRY_SWITCH_SHORT_SESSION_TASKS
            and session.pages_in_session <= COUNTRY_SWITCH_SHORT_SESSION_PAGES
        )

        # 先计算休息时间并写入 Redis，再释放 MySQL 的 is_used。
        # 顺序很重要：如果先写 is_used=False，其他进程可能在 rest_until 写入前就看到账号空闲，
        # 导致跳过休息期直接选中该账号。
        stats = self._get_day_stats(username, platform=getattr(acc, "platform", None))
        stats.last_session_end = time.time()

        if is_country_switch_short_session:
            rest_min = max(0, COUNTRY_SWITCH_REST_MIN_SECONDS)
            rest_max = max(rest_min, COUNTRY_SWITCH_REST_MAX_SECONDS)
            rest_seconds = random.uniform(rest_min, rest_max)
            stats.rest_until = time.time() + rest_seconds
            self._save_day_stats(username, stats)
            logger.info(
                f"[调度] 国家切换打断短会话: {username} | "
                f"本次完成 {session.tasks_in_session} 个任务/{session.pages_in_session} 页 | "
                f"短休息 {int(rest_seconds)}秒后可重新分配"
            )
        # 压力测试账号：极短休息，从快恢复以接受更多任务
        elif self._is_stress_test_account(username):
            rest_seconds = random.uniform(STRESS_TEST_REST_MIN_SECONDS, STRESS_TEST_REST_MAX_SECONDS)
            stats.rest_until = time.time() + rest_seconds
            self._save_day_stats(username, stats)
            logger.info(
                f"[调度][压测] 会话结束: {username} | "
                f"本次完成 {session.tasks_in_session} 个任务 | "
                f"今日累计 {stats.task_count} 任务/{stats.page_count} 页 | "
                f"休息 {int(rest_seconds)}秒"
            )
        else:
            # 休息时长和今日使用量正相关（用得越多休息越久）
            policy = self._policy_for(account=acc)
            usage_ratio = stats.task_count / max(1, stats.daily_budget)
            base_rest = policy.rest_min_seconds + (policy.rest_max_seconds - policy.rest_min_seconds) * usage_ratio
            rest_seconds = base_rest * random.uniform(0.7, 1.3)

            # 非活跃时段休息更久（凌晨刷完一会儿就"睡了"，不会马上回来）
            active_w = self._get_active_hours_weight(acc.country)
            if active_w < 0.7:
                rest_seconds *= random.uniform(2.0, 4.0)  # 深夜：休息翻 2~4 倍
            elif active_w < 1.0:
                rest_seconds *= random.uniform(1.3, 2.0)  # 边缘：休息翻 1.3~2 倍
            stats.rest_until = time.time() + rest_seconds
            self._save_day_stats(username, stats)

        # rest_until 已落盘到 Redis，此时再释放账号，确保其他进程读到 is_used=False 时
        # Redis 里已经有正确的 rest_until，不会提前抢走
        acc.is_used = False
        acc.update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_account(acc)

        rest_minutes = int(rest_seconds / 60)
        if not self._is_stress_test_account(username) and not is_country_switch_short_session:
            logger.info(
                f"[调度] 会话结束: {username} | "
                f"本次完成 {session.tasks_in_session} 个任务 | "
                f"今日累计 {stats.task_count} 任务/{stats.page_count} 页 | "
                f"休息 {rest_minutes} 分钟"
            )

        # 发射会话结束事件
        try:
            push_event(
                self.redis, EventType.SESSION_END,
                username=username,
                country=getattr(acc, 'country', ""),
                worker_id=session.worker_id,
                daily_pages=stats.page_count,
                session_seq=stats.session_seq,
                extra={
                    "tasks_in_session": session.tasks_in_session,
                    "success_count": session.success_count,
                    "fail_count": session.fail_count,
                    "rest_minutes": rest_minutes,
                    "rest_seconds": int(rest_seconds),
                    "reason": reason,
                    "short_rest": is_country_switch_short_session,
                },
            )
        except Exception:
            pass

        # 上报 InfluxDB: 账号状态 → resting
        try:
            from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
            rpt = get_reporter()
            if rpt:
                rpt.account.report_status(
                    account_id=username,
                    site=getattr(acc, 'country', ""),
                    status="resting",
                )
        except Exception:
            pass

        # 清空 worker session
        session.account = None
        session.tasks_in_session = 0
        session.pages_in_session = 0

    def _should_fatigue_rest(self, session: WorkerSession) -> bool:
        """疲劳检测：连续使用后有概率触发休息"""
        policy = self._policy_for(account=session.account)
        if session.tasks_in_session <= policy.fatigue_threshold:
            return False

        # 超过阈值后，每多一个任务，休息概率增加
        over_count = session.tasks_in_session - policy.fatigue_threshold
        prob = min(0.9, over_count * policy.fatigue_prob_step)
        return random.random() < prob

    # ========================== 活跃时段 ==========================

    def _get_active_hours_weight(self, country: str) -> float:
        """
        根据账号国家的本地时间返回权重乘数（不再硬阻断）：
          - 活跃时段（如 US 8:00-24:00）→ 1.5（正常使用）
          - 边缘时段（活跃时段前后 2 小时）→ 0.7（早起/晚睡）
          - 深夜时段 → 0.25（偶尔凌晨上网，不完全阻断）
        这样中国下午（= 美国凌晨）也能抓数据，只是频率自然降低。
        """
        return 1.0 # 先不按这个时间来做判定
        country_upper = country.upper()
        utc_offset = TIMEZONE_OFFSETS.get(country_upper, 0)
        utc_now = datetime.utcnow()
        local_hour = (utc_now + timedelta(hours=utc_offset)).hour

        start, end = ACTIVE_HOURS.get(country_upper, ACTIVE_HOURS["default"])

        # 判断是否在活跃时段
        if end > 24:
            in_active = local_hour >= start or local_hour < (end - 24)
        else:
            in_active = start <= local_hour < end

        if in_active:
            return 1.5

        # 边缘时段（活跃时段前后 2 小时）
        edge_before_start = (start - 2) % 24
        edge_after_end = (end if end <= 24 else end - 24)
        edge_after_end_2 = (edge_after_end + 2) % 24

        in_edge = False
        # 活跃前 2 小时
        if edge_before_start < start:
            in_edge = edge_before_start <= local_hour < start
        else:  # 跨午夜
            in_edge = local_hour >= edge_before_start or local_hour < start
        # 活跃后 2 小时
        if not in_edge:
            if edge_after_end < edge_after_end_2:
                in_edge = edge_after_end <= local_hour < edge_after_end_2
            else:
                in_edge = local_hour >= edge_after_end or local_hour < edge_after_end_2

        if in_edge:
            return 0.7

        # 深夜：大幅降权但不阻断
        return 0.25

    def _is_in_active_hours(self, country: str) -> bool:
        """判断账号对应国家当前是否在活跃时段（便捷方法）"""
        return self._get_active_hours_weight(country) >= 1.0

    # ========================== 日统计（Redis 持久化） ==========================

    def _redis_key(self, username: str, date: str) -> str:
        return f"{REDIS_DAY_STATS_PREFIX}:{username}:{date}"

    def _get_quota_factor(self, username: str) -> float:
        quota_factor = 1.0
        try:
            from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import get_account_quota_factor
            quota_factor = get_account_quota_factor(username, self.redis)
            if quota_factor < 1.0:
                logger.info(
                    f"[调度] 账号 {username} 配额因子={quota_factor:.2f}（有异常历史，自动降低日预算）"
                )
        except Exception:
            pass
        return quota_factor

    def _apply_conservative_quota(self, username: str, stats: AccountDayStats, quota_factor: float) -> bool:
        """
        当天预算即时生效（只降不升）：
          - 若新配额因子更低，则按比例下调 daily_budget / daily_page_budget
          - 若新因子相同或更高，不调整（次日新建 stats 时再恢复）
        """
        if quota_factor >= stats.quota_factor_applied:
            return False

        prev_factor = max(0.001, stats.quota_factor_applied)
        ratio = quota_factor / prev_factor

        old_daily_budget = stats.daily_budget
        old_page_budget = stats.daily_page_budget

        stats.daily_budget = max(1, int(stats.daily_budget * ratio))
        stats.daily_page_budget = max(10, int(stats.daily_page_budget * ratio))
        stats.quota_factor_applied = quota_factor

        logger.warning(
            f"[调度] 账号 {username} 当天预算即时收敛(只降不升): "
            f"任务 {old_daily_budget}->{stats.daily_budget}, "
            f"页面 {old_page_budget}->{stats.daily_page_budget}, "
            f"因子 {prev_factor:.2f}->{quota_factor:.2f}"
        )
        return True

    def _is_stress_test_account(self, username: str) -> bool:
        """判断账号是否为压力测试账号（label = stress_test）"""
        try:
            row = self.mysql_db.get_account_by_username(username)
            return bool(row and row.get("label") == STRESS_TEST_LABEL)
        except Exception:
            return False

    def _get_day_stats(self, username: str, platform: Optional[str] = None) -> AccountDayStats:
        """获取账号当日统计：内存缓存 → Redis → 新建"""
        today = datetime.now().strftime("%Y-%m-%d")
        stats = self._day_stats.get(username)
        quota_factor = self._get_quota_factor(username)

        if stats and stats.date == today:
            # 跨进程刷新：其他 worker 可能已修改 Redis 中的 rest_until / task_count / page_count，
            # 内存缓存不感知，必须补读一次以避免选中"正在休息"的账号
            try:
                key = self._redis_key(username, today)
                data = self.redis.hmget(key, "rest_until", "task_count", "page_count", "last_session_end")
                if data[0] is not None:
                    stats.rest_until = float(data[0])
                if data[1] is not None:
                    stats.task_count = int(data[1])
                if data[2] is not None:
                    stats.page_count = int(data[2])
                if data[3] is not None:
                    stats.last_session_end = float(data[3])
            except Exception:
                pass
            if self._apply_conservative_quota(username, stats, quota_factor):
                self._save_day_stats(username, stats)
            return stats

        # 内存没有或过期，尝试从 Redis 加载
        try:
            key = self._redis_key(username, today)
            data = self.redis.hgetall(key)
            if data and data.get("date") == today:
                stats = AccountDayStats(
                    date=today,
                    task_count=int(data.get("task_count", 0)),
                    page_count=int(data.get("page_count", 0)),
                    daily_budget=int(data.get("daily_budget", get_policy(platform).daily_budget_base)),
                    daily_page_budget=int(data.get("daily_page_budget", get_policy(platform).daily_page_budget_base)),
                    quota_factor_applied=float(data.get("quota_factor_applied", 1.0)),
                    last_session_end=float(data.get("last_session_end", 0)),
                    rest_until=float(data.get("rest_until", 0)),
                    session_seq=int(data.get("session_seq", 0)),
                )
                if self._apply_conservative_quota(username, stats, quota_factor):
                    self._save_day_stats(username, stats)
                self._day_stats[username] = stats
                logger.debug(
                    f"[调度] 从 Redis 恢复 {username} 日统计: "
                    f"任务={stats.task_count}/{stats.daily_budget} "
                    f"页={stats.page_count}/{stats.daily_page_budget}"
                )
                return stats
        except Exception as e:
            logger.warning(f"[调度] Redis 读取日统计失败: {e}")

        # Redis 也没有，新建（应用当前配额因子）

        # 压力测试账号：无限日预算，由 stress_test_runner 控制实际注入量
        if self._is_stress_test_account(username):
            stats = AccountDayStats(
                date=today,
                task_count=0,
                page_count=0,
                daily_budget=999999,
                daily_page_budget=999999,
                quota_factor_applied=1.0,
                last_session_end=0,
                rest_until=0,
                session_seq=0,
            )
            self._day_stats[username] = stats
            self._save_day_stats(username, stats)
            return stats

        policy = get_policy(platform)
        daily_budget = int(
            (policy.daily_budget_base + random.randint(-policy.daily_budget_jitter, policy.daily_budget_jitter))
            * quota_factor
        )
        daily_page_budget = int(
            (policy.daily_page_budget_base + random.randint(-policy.daily_page_budget_jitter, policy.daily_page_budget_jitter))
            * quota_factor
        )
        stats = AccountDayStats(
            date=today,
            task_count=0,
            page_count=0,
            daily_budget=max(1, daily_budget),
            daily_page_budget=max(10, daily_page_budget),
            quota_factor_applied=quota_factor,
            last_session_end=0,
            rest_until=0,
            session_seq=0,
        )
        self._day_stats[username] = stats
        self._save_day_stats(username, stats)
        return stats

    def _save_day_stats(self, username: str, stats: AccountDayStats):
        """将日统计写入 Redis（覆盖式更新）"""
        try:
            key = self._redis_key(username, stats.date)
            self.redis.hset(key, mapping={
                "date": stats.date,
                "task_count": str(stats.task_count),
                "page_count": str(stats.page_count),
                "daily_budget": str(stats.daily_budget),
                "daily_page_budget": str(stats.daily_page_budget),
                "quota_factor_applied": str(stats.quota_factor_applied),
                "last_session_end": str(stats.last_session_end),
                "rest_until": str(stats.rest_until),
                "session_seq": str(stats.session_seq),
            })
            self.redis.expire(key, REDIS_DAY_STATS_TTL)
        except Exception as e:
            logger.warning(f"[调度] Redis 写入日统计失败: {e}")

    # ========================== 数据库操作 ==========================

    def _save_account(self, account: Account):
        """回写账号状态到 MySQL"""
        if account is None:
            logger.warning("[调度] _save_account 收到 None，跳过保存")
            return
        try:
            account.update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.mysql_db.update_account(account.to_dict())
        except Exception:
            logger.error(f"[调度] 保存账号状态失败: {traceback.format_exc()}")

    # ========================== 监控/调试 ==========================

    def get_status_report(self) -> Dict:
        """返回当前调度器状态（用于监控和调试）"""
        with self.local_lock:
            sessions = {}
            for wid, s in self._sessions.items():
                sessions[wid] = {
                    "account": s.account.username if s.account else None,
                    "tasks_in_session": s.tasks_in_session,
                    "session_budget": s.session_budget,
                }

            day_stats = {}
            for uname, ds in self._day_stats.items():
                day_stats[uname] = {
                    "date": ds.date,
                    "task_count": ds.task_count,
                    "daily_budget": ds.daily_budget,
                    "resting": ds.rest_until > time.time(),
                    "rest_minutes_left": max(0, int((ds.rest_until - time.time()) / 60)),
                }

            return {
                "active_sessions": sessions,
                "day_stats": day_stats,
                "timestamp": datetime.now().isoformat(),
            }

    def force_release_all(self):
        """强制释放所有会话（紧急用）"""
        with self.local_lock:
            for wid in list(self._sessions.keys()):
                session = self._sessions[wid]
                if session.account:
                    session.account.is_used = False
                    self._save_account(session.account)
                    session.account = None
            self._sessions.clear()
            logger.info("[调度] 已强制释放所有会话")


# ========================== 兼容层 ==========================

class HumanLikeAccountManager:
    """
    兼容原 AccountManager 接口的包装类。
    可以直接替换原来的 AccountManager 使用。

    用法（和原来一样）：
        manager = HumanLikeAccountManager()
        account = manager.get_account({"country": "US"})
        # ... 执行任务 ...
        manager.release_account(account, asin, success, task_id)
    """

    # 单例调度器（所有 manager 实例共享）
    _scheduler: Optional[AccountScheduler] = None
    _init_lock = RLock()

    def __init__(self, worker_id: str = "default", account_label: str = None,
                 platform: str = "amazon"):
        self.worker_id = worker_id
        self.account_label = account_label  # 压测 worker 传 'stress_test'，生产 worker 传 None
        self.platform = platform            # 平台标识；会自动注入 get_account 的 filter_conditions
        if HumanLikeAccountManager._scheduler is None:
            with HumanLikeAccountManager._init_lock:
                if HumanLikeAccountManager._scheduler is None:
                    HumanLikeAccountManager._scheduler = AccountScheduler()

    @property
    def scheduler(self) -> AccountScheduler:
        return HumanLikeAccountManager._scheduler

    def _effective_conditions(self, filter_conditions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        effective_conditions = dict(filter_conditions or {})
        if self.account_label:
            effective_conditions["label"] = self.account_label
        effective_conditions.setdefault("platform", self.platform)
        return effective_conditions

    def get_account(self, filter_conditions: Optional[Dict[str, Any]] = None) -> Optional[Account]:
        """兼容原 AccountManager.get_account() 接口"""
        # 注入 label/platform 过滤，让 SQL 过滤、风控策略一致
        effective_conditions = self._effective_conditions(filter_conditions)
        return self.scheduler.acquire(
            worker_id=self.worker_id,
            filter_conditions=effective_conditions,
        )

    def has_configured_account(self, filter_conditions: Optional[Dict[str, Any]] = None) -> bool:
        """账号库里是否存在匹配条件的账号，不判断当前是否可用。"""
        return self.scheduler.has_configured_account(self._effective_conditions(filter_conditions))

    def release_account(self, account: Account, asin: str, success: bool, task_id: str, pages_fetched: int = 0):
        """兼容原 AccountManager.release_account() 接口

        :param account: Account 对象
        :param asin: ASIN
        :param success: 任务是否成功
        :param task_id: 任务 ID
        :param pages_fetched: 本次任务实际抓取的页面数（可选，用于精确页面预算）
        """
        # 先记录任务完成，传递 account 以便确定国家，以及页面数
        self.scheduler.complete_task(
            worker_id=self.worker_id,
            success=success,
            asin=asin,
            account=account,
            pages_fetched=pages_fetched,
        )

    def force_release(self, country: str = None):
        """强制释放当前 worker 的会话"""
        self.scheduler.release(self.worker_id, country=country)

    def _save_account(self, account: Account):
        """兼容原接口：直接保存账号状态"""
        self.scheduler._save_account(account)


if __name__ == '__main__':
    scheduler = AccountScheduler()

    # 模拟 worker 获取账号（美国）
    acc = scheduler.acquire("worker_1", {"country": "us"})
    if acc:
        print(f"分配: {acc.username} (国家={acc.country})")
        for i in range(3):
            print(f"  执行美国任务 {i + 1}...")
            scheduler.complete_task("worker_1", success=True, asin=f"ASIN_US_{i}", account=acc)

            # 再次 acquire 会复用同一账号（会话粘性）
            acc2 = scheduler.acquire("worker_1", {"country": "us"})
            print(f"  当前账号: {acc2.username}")

        # 模拟切换国家（日本）- 应该创建新的 session
        print("\n切换到日本任务...")
        acc_jp = scheduler.acquire("worker_1", {"country": "jp"})
        if acc_jp:
            print(f"分配日本账号: {acc_jp.username} (国家={acc_jp.country})")
            # 美国的 session 应该还在，只是不活跃
            scheduler.complete_task("worker_1", success=True, asin="ASIN_JP_1", account=acc_jp)

            # 切换回美国 - 应该复用之前的美国 session
            print("\n切换回美国任务...")
            acc_us = scheduler.acquire("worker_1", {"country": "us"})
            print(f"复用美国账号: {acc_us.username} (如果和之前相同则 session 保持)")

        # 释放所有 session
        scheduler.release("worker_1")

    # 查看状态
    print(json.dumps(scheduler.get_status_report(), indent=2, ensure_ascii=False))
