"""ClusterManager · 跟踪所有 active sessions + TTL sweep + health probe.

对应 plan §3.3 + §13.1 锁的 sync 范式 · 用 `threading.Timer` 起 daemon thread ·
不用 asyncio task。

核心职责:
- **track/untrack/find**:Pool.borrow 后 track · release 时 untrack
- **TTL sweep**:周期(默认 30s)扫一遍 · expired session 主动 release · 防 vendor 端 zombie
- **health probe**:周期(默认 60s)调所有 vendor 的 `.health()` · 不健康标记 ·
  Pool.borrow 时跳过(失败 chain 的隐式 driver)
- **zombie metric**(P1-8 升级):TTL release 时 vendor 不响应 · 累计计数 ·
  CLI `sourcery vendors zombies` 查

设计权衡:
- 后台 daemon thread 默认**不自动启** · 测试 / 短脚本可以只用 sync API
  (`sweep_expired_once` / `health_probe_once`)避免 thread 启停噪音
- 真生产环境(M1.4 控制台 / M1.5 编排层)显式调 `start_background()` ·
  程序退出前 `stop_background()` 释放
- `clock` 注入:测试用 manual clock 推时间 · 避免 `time.sleep` 让测试 flaky(P2-2)
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from app.browser_pool.base import BrowserSession, VendorHealth

if TYPE_CHECKING:
    from app.browser_pool.base import BrowserVendor

logger = structlog.get_logger(__name__)


# ── 默认实参 ─────────────────────────────────────────────────────

_DEFAULT_TTL_SWEEP_INTERVAL_S = 30.0
_DEFAULT_HEALTH_PROBE_INTERVAL_S = 60.0


def _wall_clock_now() -> datetime:
    """默认 clock 实现 · `datetime.now()`."""
    return datetime.now()


# ── 主类 ──────────────────────────────────────────────────────────


class ClusterManager:
    """单进程 sync 集群管理 · 跟所有 active BrowserSession + 后台扫描 + health.

    Usage(测试 / 短脚本)::

        cluster = ClusterManager(registry={"tge": tge_vendor})
        cluster.track(session)
        # 手动 sweep
        released = cluster.sweep_expired_once()

    Usage(生产)::

        cluster = ClusterManager(registry=registry, ttl_sweep_interval_s=30.0)
        cluster.start_background()
        # ... 程序运行 ...
        cluster.stop_background()  # 退出前调
    """

    def __init__(
        self,
        registry: Mapping[str, BrowserVendor],
        ttl_sweep_interval_s: float = _DEFAULT_TTL_SWEEP_INTERVAL_S,
        health_probe_interval_s: float = _DEFAULT_HEALTH_PROBE_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_sweep_interval_s <= 0:
            raise ValueError(f"ttl_sweep_interval_s must be positive, got {ttl_sweep_interval_s}")
        if health_probe_interval_s <= 0:
            raise ValueError(f"health_probe_interval_s must be positive, got {health_probe_interval_s}")

        self._registry = registry
        self._ttl_interval = ttl_sweep_interval_s
        self._health_interval = health_probe_interval_s
        self._clock = clock or _wall_clock_now

        self._sessions: dict[str, BrowserSession] = {}
        self._health_cache: dict[str, VendorHealth] = {}
        self._zombies: dict[str, int] = {}  # vendor → zombie count(P1-8)
        self._lock = threading.RLock()

        # 后台 thread 状态 · 测试场景下不启
        self._ttl_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Session 追踪 ────────────────────────────────────────

    def track(self, session: BrowserSession) -> None:
        """记录一个 active session · Pool.borrow 后调."""
        with self._lock:
            self._sessions[session.session_id] = session
        logger.info(
            "cluster.session.track",
            session_id=session.session_id,
            vendor=session.vendor,
            expires_at=session.expires_at.isoformat(),
        )

    def untrack(self, session_id: str) -> None:
        """移除一个 session · Pool.release 后调 · 不存在静默忽略."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            logger.info(
                "cluster.session.untrack",
                session_id=session_id,
                vendor=session.vendor,
            )

    def find(self, session_id: str) -> BrowserSession | None:
        """查 session · 不存在返 None(Pool.release 用)."""
        with self._lock:
            return self._sessions.get(session_id)

    def active_sessions(self) -> list[BrowserSession]:
        """快照 · 当前所有 active session(CLI / UI 用)."""
        with self._lock:
            return list(self._sessions.values())

    def session_count_by_vendor(self) -> dict[str, int]:
        """按 vendor 分组的 active session 计数 · Pool.borrow 决策用."""
        with self._lock:
            counts: dict[str, int] = {}
            for session in self._sessions.values():
                counts[session.vendor] = counts.get(session.vendor, 0) + 1
            return counts

    # ── TTL sweep ───────────────────────────────────────────

    def sweep_expired_once(self) -> list[str]:
        """单次 sweep · 释放所有 expired session · 返回被处理的 session_id 列表.

        失败 release 不 retry(避免叠加 vendor 调用)· 仅 log warn 并累计 zombie 计数。
        测试用此方法 · 不用启 daemon thread。
        """
        now = self._clock()
        with self._lock:
            expired = [s for s in self._sessions.values() if s.expires_at < now]

        released: list[str] = []
        for session in expired:
            vendor = self._registry.get(session.vendor)
            if vendor is None:
                logger.warning(
                    "cluster.session.sweep.vendor_missing",
                    session_id=session.session_id,
                    vendor=session.vendor,
                )
                self.untrack(session.session_id)
                continue
            try:
                vendor.release(session.session_id)
                self.untrack(session.session_id)
                released.append(session.session_id)
                logger.info(
                    "cluster.session.expired",
                    session_id=session.session_id,
                    vendor=session.vendor,
                    age_seconds=(now - session.started_at).total_seconds(),
                )
            except Exception as e:
                # P1-8 · TTL release 失败 → 累计 zombie · 强标 untrack
                with self._lock:
                    self._zombies[session.vendor] = self._zombies.get(session.vendor, 0) + 1
                self.untrack(session.session_id)
                logger.warning(
                    "cluster.session.expired.release_failed",
                    session_id=session.session_id,
                    vendor=session.vendor,
                    error=str(e),
                    zombie_count=self._zombies[session.vendor],
                )
        return released

    def zombie_count(self, vendor: str | None = None) -> int:
        """累计 zombie 计数(P1-8)· vendor=None 时返所有 vendor 合计.

        CLI `sourcery vendors zombies` 调用此 · `sourcery_browser_vendor_zombies_total`
        metric 也读此值。
        """
        with self._lock:
            if vendor is None:
                return sum(self._zombies.values())
            return self._zombies.get(vendor, 0)

    def zombie_counts_by_vendor(self) -> dict[str, int]:
        """按 vendor 分组 zombie 计数 snapshot."""
        with self._lock:
            return dict(self._zombies)

    # ── Health probe ────────────────────────────────────────

    def health_probe_once(self) -> dict[str, VendorHealth]:
        """单次 probe 所有 vendor · 更新内部 cache · 返回 vendor → health.

        失败的 vendor 仍写入 cache(healthy=False)· borrow 时跳过。
        """
        results: dict[str, VendorHealth] = {}
        for name, vendor in self._registry.items():
            try:
                health = vendor.health()
            except Exception as e:
                # 上一次不健康 → consecutive_failures + 1 · 否则从 1 开始
                prior = self._health_cache.get(name)
                if prior is not None and not prior.healthy:
                    failures = prior.consecutive_failures + 1
                else:
                    failures = 1
                health = VendorHealth(
                    vendor=name,
                    healthy=False,
                    last_check_at=self._clock(),
                    last_error=str(e),
                    consecutive_failures=failures,
                )
                logger.warning(
                    "cluster.health_probe.failed",
                    vendor=name,
                    error=str(e),
                    consecutive_failures=health.consecutive_failures,
                )
            results[name] = health
        with self._lock:
            self._health_cache.update(results)
        return results

    def get_health(self, vendor: str) -> VendorHealth | None:
        """读 cached health · 未 probe 过返 None(Pool 应主动 probe)."""
        with self._lock:
            return self._health_cache.get(vendor)

    # ── 后台 daemon thread ──────────────────────────────────

    def start_background(self) -> None:
        """启 TTL sweep + health probe 两个 daemon thread · 幂等调用."""
        with self._lock:
            if self._ttl_thread is not None and self._ttl_thread.is_alive():
                return
            self._stop_event.clear()
            self._ttl_thread = threading.Thread(
                target=self._ttl_loop,
                name="cluster-ttl-sweep",
                daemon=True,
            )
            self._health_thread = threading.Thread(
                target=self._health_loop,
                name="cluster-health-probe",
                daemon=True,
            )
            self._ttl_thread.start()
            self._health_thread.start()
        logger.info(
            "cluster.background.started",
            ttl_interval_s=self._ttl_interval,
            health_interval_s=self._health_interval,
        )

    def stop_background(self, timeout_s: float = 5.0) -> None:
        """停后台 thread · 程序退出前调."""
        self._stop_event.set()
        with self._lock:
            ttl = self._ttl_thread
            health = self._health_thread
        if ttl is not None:
            ttl.join(timeout=timeout_s)
        if health is not None:
            health.join(timeout=timeout_s)
        with self._lock:
            self._ttl_thread = None
            self._health_thread = None
        logger.info("cluster.background.stopped")

    def _ttl_loop(self) -> None:
        """TTL sweep daemon loop · stop_event 通过 wait 触发."""
        while not self._stop_event.wait(timeout=self._ttl_interval):
            try:
                self.sweep_expired_once()
            except Exception as e:
                logger.exception("cluster.ttl_loop.iteration_failed", error=str(e))

    def _health_loop(self) -> None:
        """Health probe daemon loop."""
        while not self._stop_event.wait(timeout=self._health_interval):
            try:
                self.health_probe_once()
            except Exception as e:
                logger.exception("cluster.health_loop.iteration_failed", error=str(e))
