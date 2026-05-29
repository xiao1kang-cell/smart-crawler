"""BrowserPool · borrow / release + failover chain.

对应 plan §3.2 + §13.1 sync 范式.

核心算法(borrow):
  for vendor_name in vendor_chain:
    vendor = registry[vendor_name]
    health = cluster.get_health(vendor_name) or vendor.health()  # cache or fresh
    if not health.healthy: errors.append; continue
    quota = vendor.quota()
    if quota.concurrent_in_use >= quota.concurrent_limit: errors.append; continue
    with per_vendor_lock:
      profile_id = profile_lookup(vendor_name) if provided else None
      session = vendor.provision(profile_id, proxy, fp_hint, ttl, initial_cookies?)
      cluster.track(session)
      return session
  raise PoolExhausted(chain, errors)

设计要点:
- Per-vendor `threading.Lock`(不是 RLock · 防 reentrancy 误用)· 限同一 vendor 并发 provision
- `profile_lookup: Callable[[str], str | None]` 让调用方注入 account-based profile 选择 ·
  避免 Pool 跟 Account 实体强耦合(Lane F 加 vendor_profile_refs 字段后再桥接)
- `initial_cookies` 仅传给 `supports_cookies_at_create=True` 的 vendor(P1-1)·
  其余 vendor 静默忽略 · 调用方在 attach 后走 `context.add_cookies()` 兜底
- `cluster_status()` 给 UI / CLI · 每 vendor 当前 quota 快照
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import structlog

from app.browser_pool.base import (
    BrowserSession,
    FingerprintHint,
    PoolExhausted,
    VendorQuota,
)

if TYPE_CHECKING:
    from app.browser_pool.base import BrowserVendor
    from app.browser_pool.cluster import ClusterManager
    from app.browser_pool._stubs import Proxy

logger = structlog.get_logger(__name__)


_PROVISION_LOCK_TIMEOUT_S = 30.0


class BrowserPool:
    """Failover-aware 借/还池 · 按 vendor_chain 顺序尝试 · 第一个可借的返回."""

    def __init__(
        self,
        registry: Mapping[str, BrowserVendor],
        cluster: ClusterManager,
    ) -> None:
        self._registry = registry
        self._cluster = cluster
        # Per-vendor provision lock(避免同一 vendor 并发 provision 时数据竞争)
        self._provision_locks: dict[str, threading.Lock] = {
            name: threading.Lock() for name in registry
        }

    # ── 主接口 ──────────────────────────────────────────────

    def borrow(
        self,
        vendor_chain: list[str],
        proxy: Proxy | None = None,
        fingerprint_hint: FingerprintHint | None = None,
        ttl_seconds: int = 300,
        initial_cookies: list[dict[str, Any]] | None = None,
        profile_lookup: Callable[[str], str | None] | None = None,
    ) -> BrowserSession:
        """按 vendor_chain 顺序 · 返回第一个 healthy + 有配额 + provision 成功的 vendor.

        Args:
            vendor_chain: vendor 名称列表 · 如 `["tge", "bit", "local_playwright"]`.
                顺序即 failover 顺序 · 第一个可用就停.
            proxy: Sourcery Proxy 实体 · vendor 内部按需翻译(见 TgeBrowserVendor._proxy_to_tge).
            fingerprint_hint: vendor 偏好提示 · None 时让 vendor 完全自管.
            ttl_seconds: BrowserSession TTL · ClusterManager 超时主动 release.
            initial_cookies: Playwright cookies list[dict] ·
                只传给 `supports_cookies_at_create=True` 的 vendor.
            profile_lookup: `vendor_name → profile_id_or_None` callable ·
                Lane E BrowserOpen executor 注入 `lambda v: account.vendor_profile_refs.get(v)`.
                None 时全部 vendor 新建 profile.

        Returns:
            BrowserSession · Pool 已 track 进 ClusterManager · 调用方接管 cdp_ws.

        Raises:
            PoolExhausted: vendor_chain 全部不可借(error_code=7000).
            ValueError: vendor_chain 为空.
        """
        if not vendor_chain:
            raise ValueError("vendor_chain must not be empty")

        errors: list[str] = []
        for vendor_name in vendor_chain:
            vendor = self._registry.get(vendor_name)
            if vendor is None:
                errors.append(f"{vendor_name}: not registered")
                logger.warning("pool.borrow.vendor_missing", vendor=vendor_name)
                continue

            # 1. health check(cache 优先 · 没有再现调一次)
            health = self._cluster.get_health(vendor_name)
            if health is None:
                try:
                    health = vendor.health()
                except Exception as e:
                    errors.append(f"{vendor_name}: health probe failed · {e}")
                    logger.warning(
                        "pool.borrow.health_probe_failed",
                        vendor=vendor_name,
                        error=str(e),
                    )
                    continue
            if not health.healthy:
                errors.append(f"{vendor_name}: unhealthy · {health.last_error or 'unknown'}")
                logger.info(
                    "pool.borrow.skip_unhealthy",
                    vendor=vendor_name,
                    last_error=health.last_error,
                )
                continue

            # 2. quota check
            try:
                quota = vendor.quota()
            except Exception as e:
                errors.append(f"{vendor_name}: quota query failed · {e}")
                logger.warning(
                    "pool.borrow.quota_query_failed",
                    vendor=vendor_name,
                    error=str(e),
                )
                continue
            if quota.concurrent_in_use >= quota.concurrent_limit:
                errors.append(
                    f"{vendor_name}: at capacity {quota.concurrent_in_use}/{quota.concurrent_limit}"
                )
                logger.info(
                    "pool.borrow.skip_at_capacity",
                    vendor=vendor_name,
                    in_use=quota.concurrent_in_use,
                    limit=quota.concurrent_limit,
                )
                continue

            # 3. acquire vendor lock + provision
            lock = self._provision_locks.setdefault(vendor_name, threading.Lock())
            acquired = lock.acquire(timeout=_PROVISION_LOCK_TIMEOUT_S)
            if not acquired:
                errors.append(f"{vendor_name}: lock timeout > {_PROVISION_LOCK_TIMEOUT_S}s")
                logger.warning("pool.borrow.lock_timeout", vendor=vendor_name)
                continue
            try:
                profile_id = profile_lookup(vendor_name) if profile_lookup else None
                cookies_for_vendor = (
                    initial_cookies if vendor.supports_cookies_at_create else None
                )
                try:
                    session = vendor.provision(
                        profile_id=profile_id,
                        proxy=proxy,
                        fingerprint_hint=fingerprint_hint,
                        ttl_seconds=ttl_seconds,
                        initial_cookies=cookies_for_vendor,
                    )
                except Exception as e:
                    errors.append(f"{vendor_name}: provision failed · {e}")
                    logger.warning(
                        "pool.borrow.provision_failed",
                        vendor=vendor_name,
                        profile_id=profile_id,
                        error=str(e),
                    )
                    continue
            finally:
                lock.release()

            # 4. track + return
            self._cluster.track(session)
            logger.info(
                "pool.borrow.success",
                vendor=vendor_name,
                session_id=session.session_id,
                profile_id=session.profile_id,
                ttl_seconds=ttl_seconds,
                chain=vendor_chain,
            )
            return session

        # 5. all vendors failed → PoolExhausted
        logger.error(
            "pool.borrow.exhausted",
            vendor_chain=vendor_chain,
            errors=errors,
        )
        raise PoolExhausted(vendor_chain=vendor_chain, errors=errors)

    def release(self, session_id: str) -> None:
        """归还 session · 找 vendor · 调 vendor.release · untrack.

        失败容忍:vendor.release 抛错时 log warn 并仍 untrack(防 leak)·
        zombie 计数由 ClusterManager 处理(集成在 sweep_expired 路径)。
        """
        session = self._cluster.find(session_id)
        if session is None:
            logger.info("pool.release.unknown_session", session_id=session_id)
            return
        vendor = self._registry.get(session.vendor)
        if vendor is None:
            logger.warning(
                "pool.release.vendor_missing",
                session_id=session_id,
                vendor=session.vendor,
            )
            self._cluster.untrack(session_id)
            return
        try:
            vendor.release(session_id)
        except Exception as e:
            logger.warning(
                "pool.release.vendor_call_failed",
                session_id=session_id,
                vendor=session.vendor,
                error=str(e),
            )
        finally:
            self._cluster.untrack(session_id)
        logger.info("pool.release.done", session_id=session_id, vendor=session.vendor)

    # ── 查询 ────────────────────────────────────────────────

    def cluster_status(self) -> dict[str, VendorQuota]:
        """每 vendor 当前配额 snapshot · UI / CLI / metrics 用.

        skipped if quota() 抛错 · 不让一个坏 vendor 拖垮整个 status 读取.
        """
        result: dict[str, VendorQuota] = {}
        for name, vendor in self._registry.items():
            try:
                result[name] = vendor.quota()
            except Exception as e:
                logger.warning(
                    "pool.cluster_status.quota_failed",
                    vendor=name,
                    error=str(e),
                )
        return result

    def generate_session_id(self) -> str:
        """给 vendor 实装用 · Sourcery 内部 session_id 不编码 vendor 信息(P1-4)."""
        return uuid.uuid4().hex
