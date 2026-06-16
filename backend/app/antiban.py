"""反封禁 —— 熔断器 + 每站限速档 + IP 日配额。

目标：模拟人操作、避免单 IP 被打死。
  · 熔断：站点返回 401/403/429 立即抛 BlockedError，采集器中止、不再猛打
  · 限速档：每类站点不同请求间隔，评论平台远慢于商品站
  · 站点冷却：被封的站点进入冷却期，调度跳过
  · IP 配额：每个出口 IP 每日请求上限，超额轮换
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta

BLOCK_STATUS = {401, 403, 429}

# 每平台请求间隔基准（秒）—— 评论平台刻意放慢，更像人
RATE_TIERS = {
    "shopify": 1.0,
    "vue_spa": 1.5,
    "nuxt": 2.0,
    "generic": 2.0,
    "flexispot": 2.0,
    "vonhaus": 2.0,
    "vidaxl": 2.5,
    "sephora": 2.5,
    "trustpilot": 6.0,
    "google_map": 8.0,
}

COOLDOWN_HOURS = int(os.environ.get("BLOCK_COOLDOWN_HOURS", "12"))
IP_DAILY_CAP = int(os.environ.get("IP_DAILY_CAP", "800"))

_lock = threading.Lock()
_cooldown: dict[str, datetime] = {}          # site -> 冷却到期时间
_ip_usage: dict[tuple, int] = {}             # (ip, date) -> 请求数


class BlockedError(Exception):
    """目标站点返回封禁信号 —— 触发熔断。"""


def check_blocked(status: int, where: str = "") -> None:
    """命中封禁状态码即抛 BlockedError（熔断）。"""
    if status in BLOCK_STATUS:
        raise BlockedError(f"{where or '站点'} 返回 {status} —— 疑似封禁，熔断")


def rate_delay(platform: str, default: float = 1.5) -> float:
    return RATE_TIERS.get(platform, default)


# ---------- 站点冷却 ----------
def set_cooldown(site: str) -> None:
    with _lock:
        _cooldown[site] = datetime.utcnow() + timedelta(hours=COOLDOWN_HOURS)


def in_cooldown(site: str) -> bool:
    with _lock:
        until = _cooldown.get(site)
        if until and until > datetime.utcnow():
            return True
        if until:
            del _cooldown[site]
        return False


def cooldown_status() -> dict:
    with _lock:
        now = datetime.utcnow()
        return {s: u.isoformat() for s, u in _cooldown.items() if u > now}


# ---------- IP 日配额 ----------
def ip_record(ip: str | None, n: int = 1) -> None:
    if not ip:
        return
    with _lock:
        key = (ip, date.today())
        _ip_usage[key] = _ip_usage.get(key, 0) + n


def ip_over_quota(ip: str | None) -> bool:
    if not ip:
        return False
    with _lock:
        return _ip_usage.get((ip, date.today()), 0) >= IP_DAILY_CAP


def ip_usage_today() -> dict:
    today = date.today()
    with _lock:
        return {ip: c for (ip, d), c in _ip_usage.items() if d == today}


def humanized_sleep(base: float) -> None:
    """带抖动的拟人停顿 —— 间隔随机化，不固定频率。"""
    time.sleep(base + (base * 0.6) * _rand())


def _rand() -> float:
    import random
    return random.random()
