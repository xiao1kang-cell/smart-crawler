"""RedisSlidingWindowRateLimiter · Risk 2 fix · M2 多 Worker 共享配额方案.

| 维度 | InMemorySlidingWindowRateLimiter | RedisSlidingWindowRateLimiter |
|---|---|---|
| 范围 | 单进程内多线程 | 多进程 / 多机共享 |
| 后端 | `collections.deque` + `threading.Lock` | Redis ZSET + Lua atomic |
| 适用 | M1 单 Worker 范式 | M2+ 多 Worker 部署 |
| 依赖 | 无 | `pip install redis>=5` + Redis 6.0+ |

# 为什么 ZSET + Lua

- ZSET score = unix epoch ms → `ZADD` 加入当前时间 · `ZRANGEBYSCORE` 数窗口内 calls
- 整套逻辑必须 atomic(否则两 worker 同时 ZCARD → 都看到 99 → 都 +1 → 实际 101)
- Lua 脚本在 Redis 内单线程执行 · 自然 atomic · 不需 WATCH/MULTI 复杂事务

# 限制

- Lua + Redis 不在传输路径里 sleep · 触发限流时由调用端 sleep 重试(`acquire` 内部循环)
- Redis 不可用时 raise `RedisNotAvailable` · 调用端按业务策略 fail-open / fail-closed

# 工厂模式

不在调用方写死 InMemory · 用 `create_rate_limiter()` factory 按 env 选实现:

```python
from app.browser_pool.rate_limiter import create_rate_limiter

rl = create_rate_limiter(max_calls=100, period_seconds=60)
rl.acquire()  # 调用方不关心是 in-memory 还是 redis
```

env 控制:
- `SOURCERY_RATE_LIMITER_BACKEND=memory` (M1 默认) · 用 InMemory
- `SOURCERY_RATE_LIMITER_BACKEND=redis` + `SOURCERY_REDIS_URL=redis://...` · 用 Redis
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis as _redis_types  # 类型 hint only · 不强制装 redis


class RedisNotAvailable(RuntimeError):
    """Redis 包未装或 server 不可达."""


# Lua 脚本 · ZSET 滑动窗口 · 原子检查 + 加入.
# KEYS[1] = ZSET key (例 "sourcery:rl:tge")
# ARGV[1] = max_calls · ARGV[2] = period_ms · ARGV[3] = now_ms · ARGV[4] = member (uuid)
# 返回 1 = 已加入 · 0 = 已满 (调用方需等)
_ACQUIRE_LUA = """
local key = KEYS[1]
local max_calls = tonumber(ARGV[1])
local period_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local member = ARGV[4]

-- 1. 清理过期 entries
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - period_ms)

-- 2. 检查窗口大小
local count = redis.call('ZCARD', key)
if count < max_calls then
  redis.call('ZADD', key, now_ms, member)
  redis.call('PEXPIRE', key, period_ms + 1000)  -- TTL 兜底防泄漏
  return 1
end
return 0
"""


class RedisSlidingWindowRateLimiter:
    """Redis-backed sliding window · 接口与 InMemorySlidingWindowRateLimiter 一致.

    Args:
        max_calls: 窗口内最大调用次数
        period_seconds: 窗口长度(秒)
        redis_url: Redis URL · 不传则读 env SOURCERY_REDIS_URL
        key_prefix: ZSET key 前缀(多 vendor 共享 redis 时用)
        poll_interval_s: 满 时 sleep 多久再 retry · 默认 0.05s(50ms)
    """

    def __init__(
        self,
        max_calls: int,
        period_seconds: int,
        *,
        redis_url: str | None = None,
        key_prefix: str = "sourcery:rl",
        poll_interval_s: float = 0.05,
    ) -> None:
        if max_calls <= 0:
            raise ValueError(f"max_calls must be positive, got {max_calls}")
        if period_seconds <= 0:
            raise ValueError(f"period_seconds must be positive, got {period_seconds}")
        if poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be positive, got {poll_interval_s}")

        try:
            import redis  # noqa: PLC0415
        except ImportError as exc:
            raise RedisNotAvailable(
                "redis package not installed · `pip install redis>=5` to enable"
            ) from exc

        import os

        url = redis_url or os.environ.get("SOURCERY_REDIS_URL")
        if not url:
            raise RedisNotAvailable(
                "SOURCERY_REDIS_URL not set and redis_url not passed"
            )

        self._max_calls = max_calls
        self._period_ms = period_seconds * 1000
        self._period_seconds = period_seconds
        self._key = f"{key_prefix}:{id(self):x}"  # 实例隔离(避免 unit test 串扰)
        self._poll_interval_s = poll_interval_s

        self._client = redis.Redis.from_url(url, decode_responses=True)
        # ping 测连接 · 失败 raise RedisNotAvailable
        try:
            self._client.ping()
        except redis.RedisError as exc:
            raise RedisNotAvailable(f"redis ping failed: {exc}") from exc
        self._script = self._client.register_script(_ACQUIRE_LUA)

    def acquire(self) -> None:
        """Block 到能拿到一个 slot · 接口与 InMemory 版一致."""
        import uuid

        while True:
            now_ms = int(time.time() * 1000)
            ok = int(
                self._script(
                    keys=[self._key],
                    args=[self._max_calls, self._period_ms, now_ms, uuid.uuid4().hex],
                )
            )
            if ok == 1:
                return
            time.sleep(self._poll_interval_s)

    def try_acquire(self) -> bool:
        """非阻塞 · 返 True=拿到 · False=已满."""
        import uuid

        now_ms = int(time.time() * 1000)
        ok = int(
            self._script(
                keys=[self._key],
                args=[self._max_calls, self._period_ms, now_ms, uuid.uuid4().hex],
            )
        )
        return ok == 1

    def remaining(self) -> int:
        """窗口内剩余可调用次数(即时快照 · 别拿来做 race-condition free check)."""
        now_ms = int(time.time() * 1000)
        self._client.zremrangebyscore(self._key, "-inf", now_ms - self._period_ms)
        used = int(self._client.zcard(self._key))
        return max(0, self._max_calls - used)

    def reset_at(self) -> datetime:
        """估算下一个 slot 可用的时间(最早 entry + period)."""
        oldest = self._client.zrange(self._key, 0, 0, withscores=True)
        if not oldest:
            return datetime.utcnow()
        oldest_ms = int(oldest[0][1])
        return datetime.utcfromtimestamp((oldest_ms + self._period_ms) / 1000)
