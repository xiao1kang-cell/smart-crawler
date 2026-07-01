# ------------------------------
# 3. Redis 分布式锁（多机器/多进程安全）
# ------------------------------
import os
import random
import socket
import time
import uuid

import redis

from app.crawlers.amazon_crawler.shuler.util.config import *


class RedisDistLock:
    def __init__(self):
        self.client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            username=REDIS_USERNAME,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        self._tokens = {}

    def acquire(
            self,
            lock_key: str,
            timeout: int = 10,
            blocking: bool = False,
            wait_timeout: float = None,
            retry_interval: float = 0.05,
            jitter: float = 0.15,
    ) -> bool:
        """
        获取锁：SET NX EX。

        timeout 是锁 TTL（秒），保持老接口语义；blocking=True 时会在 wait_timeout
        时间内用随机退避等待，避免多进程同时抢锁时固定睡眠造成惊群。
        """
        ttl = max(1, int(timeout or 1))
        token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        deadline = None
        if blocking:
            deadline = time.monotonic() + max(0.0, float(wait_timeout if wait_timeout is not None else timeout))

        while True:
            if self.client.set(lock_key, token, nx=True, ex=ttl):
                self._tokens[lock_key] = token
                return True
            if not blocking:
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            sleep_seconds = max(0.01, float(retry_interval or 0.05)) + random.uniform(0.0, max(0.0, float(jitter or 0.0)))
            if deadline is not None:
                sleep_seconds = min(sleep_seconds, max(0.0, deadline - time.monotonic()))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def release(self, lock_key: str):
        """只释放当前实例持有的锁，避免锁过期后误删其他进程的新锁。"""
        token = self._tokens.pop(lock_key, None)
        if not token:
            return False
        script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        end
        return 0
        """
        return bool(self.client.eval(script, 1, lock_key, token))
