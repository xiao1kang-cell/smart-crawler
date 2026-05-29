"""RateLimiter · sliding window 内存实现 · 单进程 thread-safe.

对应 plan §5.2 · §13.1 锁的 sync 范式(用 `threading.Lock` + `time.sleep` ·
不用 asyncio)。

设计:
- Sliding window:不是 fixed bucket · 减少 bucket 边缘"突发翻倍"问题
- `acquire()` 阻塞调用 · 必要时 sleep 到下一个 slot 可用
- M2 升 Redis-backed `RateLimiter` 后 · 多 Worker 共享配额(否则会爆 vendor rate limit)·
  接口保持兼容 · 仅替换实现

为什么用 deque 而不是 collections.OrderedDict:
- deque popleft O(1) · 一致丢早期 timestamp
- 不需要 key→value 查找(只关心时间窗内多少次)

┌────────────────────────────────────────────────────────────┐
│         Sliding window 示意                                 │
│                                                             │
│       窗口 = 60s · 上限 = 100 calls                        │
│                                                             │
│    t=0 ─── 调 1 → deque=[0]                                │
│    t=1 ─── 调 2 → deque=[0,1]                              │
│    ...                                                      │
│    t=59 ── 调 100 → deque=[0,1,...,59] · 满                │
│    t=60 ── 调 101 → 先 popleft 过期(0)→ 剩 99 → 加入 60   │
│    t=61 ── 调 102 → popleft(1)→ 加入 61                    │
│                                                             │
│    达到 100 时 · acquire() 等到 deque[0]+period <= now      │
└────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta


class RateLimiter:
    """单进程 sliding window rate limiter.

    Thread-safe via `threading.Lock` · 多 worker thread 共享一个 RateLimiter
    实例可正确串行化(M1 单进程范式)。M2 切 Redis 后接口不变。

    Usage::

        rl = RateLimiter(max_calls=100, period_seconds=60)
        rl.acquire()  # 必要时阻塞 · 返回时保证未超限
        # ... 调 vendor API ...
    """

    def __init__(self, max_calls: int, period_seconds: int) -> None:
        if max_calls <= 0:
            raise ValueError(f"max_calls must be positive, got {max_calls}")
        if period_seconds <= 0:
            raise ValueError(f"period_seconds must be positive, got {period_seconds}")

        self._max = max_calls
        self._period = period_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    # ── 主接口 ──────────────────────────────────────────────

    def acquire(self) -> None:
        """阻塞直到可调一次 · 然后记录这次调用.

        如果当前窗口未满 · 立即返回 · 几乎零开销。
        如果当前窗口满 · sleep 到 `deque[0] + period` 时刻 · 让最早一次调用过期。

        线程安全:不持锁睡眠(避免 thundering herd 时所有线程被一个长锁卡住)·
        sleep 后重入循环 · 重新评估窗口。
        """
        while True:
            with self._lock:
                now = time.monotonic()
                # 弹出窗口外的过期 timestamp
                self._evict_expired(now)
                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return
                # 窗口满 · 算睡多久(到最早一次调用过期)
                sleep_s = self._calls[0] + self._period - now
            # 持锁外 sleep · 让其他线程可见(虽然它们也会被自己 acquire 卡住)
            if sleep_s > 0:
                time.sleep(sleep_s)
            # 循环重入 · 再次检查(避免 wakeup 时窗口又满)

    def try_acquire(self) -> bool:
        """非阻塞尝试 · True 表示成功 · False 表示窗口满.

        给"宁可失败也不要等"的场景用 · 如 health probe(失败标 vendor unhealthy)。
        """
        with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            if len(self._calls) < self._max:
                self._calls.append(now)
                return True
            return False

    # ── 查询 ────────────────────────────────────────────────

    def remaining(self) -> int:
        """当前窗口余量 · UI / quota() 用."""
        with self._lock:
            self._evict_expired(time.monotonic())
            return self._max - len(self._calls)

    def reset_at(self) -> datetime:
        """下次窗口完全清空的时刻 · 给 VendorQuota.rate_limit_reset_at 用.

        如果窗口空 → now;否则 deque[0] + period 是最早一次过期的时刻 ·
        当下次窗口"完全清空"是 deque[-1] + period(最晚一次过期)。
        """
        with self._lock:
            self._evict_expired(time.monotonic())
            if not self._calls:
                return datetime.now()
            # deque[-1] 是最晚一次调用 monotonic 秒 · 转换成 wall-clock 时刻
            seconds_until_full_reset = (self._calls[-1] + self._period) - time.monotonic()
            return datetime.now() + timedelta(seconds=max(0.0, seconds_until_full_reset))

    # ── 内部 ────────────────────────────────────────────────

    def _evict_expired(self, now: float) -> None:
        """弹出窗口外的 timestamp · 调用方持锁."""
        cutoff = now - self._period
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
