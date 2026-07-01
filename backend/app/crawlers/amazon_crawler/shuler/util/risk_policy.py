"""
跨平台风控策略表。

每个平台的疲劳/冷却/会话/日预算等参数集中在 RiskPolicy 实例里，
AccountScheduler 在选号/释放/疲劳检测时按 account.platform 或 filter_conditions.platform
查找对应策略。新增平台只需在 POLICY_REGISTRY 注册一份。

设计原则：
- 与平台无关的国家/时区参数（ACTIVE_HOURS、TIMEZONE_OFFSETS）保留在 account_scheduler.py，不放这里
- 测试账号（stress_test）独立走 stress_test 路径，不混在 platform 策略里
"""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class RiskPolicy:
    platform: str

    # 失败 / 冷却
    max_fail: int                 # 连续失败 N 次进入冷却
    cooldown_seconds: int         # 冷却时长
    account_used_minutes: int     # 账号被占用超时（用于多 worker 抢占检测）

    # 会话粘性：一个 worker 连续使用同一账号的任务数
    session_min_tasks: int
    session_max_tasks: int

    # 会话间休息
    rest_min_seconds: int
    rest_max_seconds: int

    # 日预算
    daily_budget_base: int
    daily_budget_jitter: int
    daily_page_budget_base: int
    daily_page_budget_jitter: int

    # 疲劳衰减
    fatigue_threshold: int
    fatigue_prob_step: float


AMAZON_POLICY = RiskPolicy(
    platform="amazon",
    max_fail=2,
    cooldown_seconds=3600,
    account_used_minutes=60,
    session_min_tasks=8,
    session_max_tasks=12,
    rest_min_seconds=60 * 5,
    rest_max_seconds=60 * 10,
    daily_budget_base=300,
    daily_budget_jitter=5,
    daily_page_budget_base=400,
    daily_page_budget_jitter=50,
    fatigue_threshold=20,
    fatigue_prob_step=0.15,
)


# Twitter 默认值（保守起点，按实际封号率再调）
TWITTER_POLICY = RiskPolicy(
    platform="twitter",
    max_fail=3,
    cooldown_seconds=72 * 3600,   # Twitter 封号通常更久
    account_used_minutes=30,
    session_min_tasks=15,
    session_max_tasks=30,
    rest_min_seconds=60 * 8,
    rest_max_seconds=60 * 15,
    daily_budget_base=500,
    daily_budget_jitter=50,
    daily_page_budget_base=500,
    daily_page_budget_jitter=50,
    fatigue_threshold=40,
    fatigue_prob_step=0.10,
)


POLICY_REGISTRY: Dict[str, RiskPolicy] = {
    "amazon": AMAZON_POLICY,
    "twitter": TWITTER_POLICY,
}


def get_policy(platform: Optional[str] = None) -> RiskPolicy:
    """按 platform 取风控策略；未知平台或 None 返回 AMAZON_POLICY。"""
    if not platform:
        return AMAZON_POLICY
    return POLICY_REGISTRY.get(platform.lower(), AMAZON_POLICY)
