"""
静态IP池调度器

crawler_accounts.static_ip 存完整代理URL（如 http://user:pass@38.213.252.67:2333）。
Redis lock key 从URL中提取 hostname 作为唯一标识。

职责：协调评论任务（Playwright）和商品详情任务（curl_cffi）对同一批静态IP的使用。
  - 评论任务：绑定账号专属IP，优先级高，最多等30秒抢回被商品任务占用的IP
  - 商品任务：抢任意空闲静态IP，全忙自动降级动态代理

Redis lock key: sip_lock:{hostname}
lock value:     "review" | "product"
"""

import os
import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import redis
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)

LOCK_PREFIX = "sip_lock"
COOL_PREFIX = "sip_cool"    # IP冷却标记
FAIL_PREFIX = "sip_fail"    # IP连续失败计数
RATE_PREFIX = "sip_rate"    # IP滑动窗口请求记录（sorted set）
COOKIE_PREFIX = "sip_cookie"  # IP验证通行cookie缓存
REVIEW_TTL = 600    # 评论会话最多持有10分钟
PRODUCT_TTL = 60    # 商品请求最多持有60秒
IP_FAIL_LIMIT = 5   # 连续失败次数阈值
IP_COOL_TTL = 10 * 60       # 冷却时长（秒）
IP_RATE_HOURLY = 40         # 任意连续1小时内最多请求次数
IP_RATE_DAILY = 400         # 任意连续24小时内最多请求次数


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except (TypeError, ValueError):
        return default


ASIN_COOKIE_TTL_SECONDS = _env_int("ASIN_COOKIE_TTL_SECONDS", 7 * 86400)


def _hostname(proxy_url: str) -> str:
    return urlparse(proxy_url).hostname or proxy_url[:50]


def _lock_key(proxy_url: str) -> str:
    return f"{LOCK_PREFIX}:{_hostname(proxy_url)}"


def _cookie_key(proxy_url: str, region: str = None) -> str:
    hostname = _hostname(proxy_url)
    region_part = str(region or "").strip().upper()
    if region_part:
        return f"{COOKIE_PREFIX}:{region_part}:{hostname}"
    return f"{COOKIE_PREFIX}:{hostname}"


def _to_proxy_dict(proxy_url: str) -> dict:
    return {"http": proxy_url, "https": proxy_url}


class StaticIPPool:

    # IP列表内存缓存（key=region, value=(timestamp, list)），避免高并发时频繁查DB
    _IP_CACHE_TTL = 30  # 秒，账号 is_used 状态变化后最多30秒生效
    _ip_cache: dict = {}
    _ip_cache_lock = threading.Lock()

    def __init__(self, mysql_db=None, redis_client: redis.Redis = None):
        self.mysql_db = mysql_db
        self.redis = redis_client or redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
        )
        self._rng = random.Random()  # 独立随机实例，不与全局 random 共享状态

    def _all_static_ips(self, region: str = None) -> list[tuple[str, str]]:
        """从DB加载静态代理URL列表（带30秒内存缓存），返回 [(proxy_url, username)]"""
        cache_key = region or "__all__"
        now = time.time()
        with self._ip_cache_lock:
            cached = self._ip_cache.get(cache_key)
            if cached and now - cached[0] < self._IP_CACHE_TTL:
                return list(cached[1])  # 返回副本，避免外部修改缓存

        if not self.mysql_db:
            return []
        try:
            rows = self.mysql_db.get_all_static_ips(country=region)
            seen: dict[str, str] = {}
            for r in rows:
                ip = r.get('static_ip')
                if ip and ip not in seen:
                    seen[ip] = r['username']
            result = list(seen.items())
            with self._ip_cache_lock:
                self._ip_cache[cache_key] = (now, result)
            return list(result)
        except Exception as e:
            logger.warning(f"[StaticIPPool] 加载静态IP列表失败: {e}")
            return []


    def acquire_for_product(self, region: str = None) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """
        为商品任务获取任意空闲、未冷却、且滑动窗口未超频的静态IP。
        非US站点没有专属静态IP时，回退使用US静态IP；仍无可用IP则降级动态代理。
        用一次 pipeline 批量查冷却+频率，减少 Redis round trip。
        返回 (proxy_url, proxy_dict, account_username)，无可用IP返回 (None, None, None)。
        """
        now = time.time()
        requested_region = str(region or "").strip().upper()
        entries = self._all_static_ips(region=requested_region or None)
        if not entries and requested_region and requested_region != "US":
            entries = self._all_static_ips(region="US")
            if entries:
                logger.debug(f"[StaticIPPool] {requested_region} 无静态IP，回退US静态IP")
        if not entries:
            return None, None, None
        self._rng.shuffle(entries)

        hostnames = [urlparse(url).hostname for url, _ in entries]

        # ── 第一次 pipeline：批量查冷却状态 + 滑动窗口计数 ──────────────────
        pipe = self.redis.pipeline(transaction=False)
        for h in hostnames:
            pipe.exists(f"{COOL_PREFIX}:{h}")
        for h in hostnames:
            rate_key = f"{RATE_PREFIX}:{h}"
            pipe.zremrangebyscore(rate_key, 0, now - 86400)
            pipe.zcount(rate_key, now - 3600, '+inf')
            pipe.zcard(rate_key)
        results = pipe.execute()

        n = len(entries)
        cool_flags = results[:n]           # n个 EXISTS 结果
        rate_results = results[n:]         # 剩余 n×3 个结果

        # ── 本地过滤，找出候选IP ─────────────────────────────────────────────
        candidates = []
        for i, (proxy_url, username) in enumerate(entries):
            if cool_flags[i]:
                continue
            _, hourly, daily = rate_results[i * 3], rate_results[i * 3 + 1], rate_results[i * 3 + 2]
            if hourly >= IP_RATE_HOURLY or daily >= IP_RATE_DAILY:
                continue
            candidates.append((proxy_url, username, hourly, daily))

        if not candidates:
            logger.debug(f"[StaticIPPool] 所有静态IP忙碌/冷却/超频({n}个)，降级动态代理")
            return None, None, None

        # 按使用量升序排列，优先选最久未使用的IP，避免同一个IP被反复选中
        candidates.sort(key=lambda c: (c[2], c[3]))  # (hourly, daily) 升序

        # ── 对候选IP逐一抢锁（通常第一个就能抢到）──────────────────────────
        for proxy_url, username, hourly, daily in candidates:
            hostname = urlparse(proxy_url).hostname
            key = _lock_key(proxy_url)
            if self.redis.set(key, "product", nx=True, ex=PRODUCT_TTL):
                rate_key = f"{RATE_PREFIX}:{hostname}"
                pipe = self.redis.pipeline()
                pipe.zadd(rate_key, {str(time.time_ns()): now})
                pipe.expire(rate_key, 86400 + 60)
                pipe.execute()
                logger.debug(
                    f"[StaticIPPool] product锁定: {hostname} 账号={username} "
                    f"近1h={hourly+1} 近24h={daily+1}"
                )
                return proxy_url, _to_proxy_dict(proxy_url), username

        logger.debug(f"[StaticIPPool] 候选IP({len(candidates)}个)全被抢占，降级动态代理")
        return None, None, None

    def release(self, proxy_url: str):
        """释放IP锁"""
        try:
            self.redis.delete(_lock_key(proxy_url))
            logger.debug(f"[StaticIPPool] 释放: {urlparse(proxy_url).hostname}")
        except Exception as e:
            logger.warning(f"[StaticIPPool] 释放锁失败: {e}")

    def mark_ip_failed(self, proxy_url: str):
        """记录IP失败一次，达到阈值进入冷却"""
        hostname = urlparse(proxy_url).hostname or proxy_url[:50]
        fail_key = f"{FAIL_PREFIX}:{hostname}"
        count = int(self.redis.incr(fail_key))
        self.redis.expire(fail_key, 3600)
        if count >= IP_FAIL_LIMIT:
            self.redis.set(f"{COOL_PREFIX}:{hostname}", "1", ex=IP_COOL_TTL)
            self.redis.delete(fail_key)
            logger.warning(
                f"[StaticIPPool] {hostname} 连续失败{count}次，冷却{IP_COOL_TTL // 60}分钟"
            )

    def mark_ip_success(self, proxy_url: str):
        """成功后清除该IP的失败计数"""
        hostname = urlparse(proxy_url).hostname or proxy_url[:50]
        self.redis.delete(f"{FAIL_PREFIX}:{hostname}")

    def get_ip_cookies(self, proxy_url: str, region: str = None) -> str:
        """获取该IP缓存的验证通行cookie，无则返回空串"""
        key = _cookie_key(proxy_url, region)
        cookie = self.redis.get(key)
        if cookie:
            logger.debug(f"[StaticIPPool] cookie命中: {key}")
            return cookie

        if region:
            legacy_key = _cookie_key(proxy_url)
            cookie = self.redis.get(legacy_key)
            if cookie:
                logger.debug(f"[StaticIPPool] cookie命中旧key: {legacy_key}")
                return cookie
        return ""

    def set_ip_cookies(self, proxy_url: str, cookie_str: str,
                       region: str = None, ttl: int = None) -> bool:
        """缓存该IP的验证通行cookie"""
        cookie_str = (cookie_str or "").strip()
        if not cookie_str:
            logger.debug(f"[StaticIPPool] 跳过空cookie: {_cookie_key(proxy_url, region)}")
            return False
        ttl_seconds = ttl or ASIN_COOKIE_TTL_SECONDS
        key = _cookie_key(proxy_url, region)
        self.redis.set(key, cookie_str, ex=ttl_seconds)
        logger.debug(f"[StaticIPPool] 缓存cookie: {key} ({len(cookie_str)}字符, ttl={ttl_seconds}s)")
        return True

    def delete_ip_cookies(self, proxy_url: str, region: str = None):
        """删除该IP的cookie缓存；region存在时顺带清理旧版无region key。"""
        keys = [_cookie_key(proxy_url, region)]
        if region:
            keys.append(_cookie_key(proxy_url))
        self.redis.delete(*keys)
        logger.debug(f"[StaticIPPool] 删除cookie: {keys}")
