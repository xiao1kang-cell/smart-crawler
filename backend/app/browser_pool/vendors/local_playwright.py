"""LocalPlaywrightVendor · backward compat 路径 + 测试 / 演示场景默认 vendor.

对应 plan §3.4 + §13.1 sync 范式.

**实装路径(eng-review 后修正 · plan §3.4 原假设错误)**:
Plan §3.4 写"用 `chromium.ws_endpoint` 暴露 ws"是 async-only API · sync_playwright
**没有** `launch_server` / `BrowserServer` / `Browser.ws_endpoint`。修正为:
- `chromium.launch()` 启真 Browser 实例 · vendor 内部 `_browsers` dict 持有
- `BrowserSession.cdp_ws` 用 sentinel `local://<session_id>` · Lane E executor 改造时
  识别 `local://` 前缀走 vendor 内部 Browser(不走 `connect_over_cdp`)
- `get_browser(session_id)` API 给 Lane E executor 拿现成 Browser instance

这等价于 plan §3.4 的"包装 BrowserSessionManager" · 但更纯粹(vendor 直接持 Browser ·
不依赖 BrowserSessionManager)· 等 Lane E 改造完成后 M0 BrowserSessionManager 可删。

不支持的能力:
- `supports_cookies_at_create = False`(local launch 时不能灌 cookies · 走 attach 后
  context.add_cookies 兜底)
- `capabilities() = set()`(没有 Tge 那种私有扩展)
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

try:
    from playwright_stealth import Stealth

    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

# SOURCERY_STEALTH=1 / true / yes 启用基础 anti-bot 绕过(navigator.webdriver 等)
# 不启用时行为与原版完全一致
_STEALTH_ENABLED = os.environ.get("SOURCERY_STEALTH", "").lower() in ("1", "true", "yes")

from app.browser_pool.base import (
    BrowserProfile,
    BrowserSession,
    FingerprintHint,
    VendorHealth,
    VendorQuota,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright

    from app.browser_pool._stubs import Proxy

logger = structlog.get_logger(__name__)


_FAR_FUTURE_RESET = timedelta(days=365)


class PlaywrightNotInstalled(Exception):
    """Playwright extra 没装 · provision 时抛 · ErrorCode 3xxx RESOURCE_NOT_FOUND."""

    def __init__(self) -> None:
        super().__init__(
            "Playwright 未安装 · 运行 'uv sync --extra browser' 后再用 LocalPlaywrightVendor"
        )


class LocalPlaywrightVendor:
    """本地 Chromium · 通过 Playwright sync API 启 Browser · vendor 内部持实例.

    cdp_ws 用 sentinel `local://<session_id>` · BrowserOpen executor 改造后
    识别此前缀 · 调 `get_browser(session_id)` 取现成 Browser · 不走 connect_over_cdp。

    Usage(Lane E executor 改造后)::

        session = vendor.provision(...)
        if session.cdp_ws.startswith("local://"):
            browser = vendor.get_browser(session.session_id)
        else:
            browser = playwright.chromium.connect_over_cdp(session.cdp_ws)
        # ... browser.new_context() / new_page() / goto ...
    """

    name = "local_playwright"
    supports_cookies_at_create = False

    def __init__(self, *, concurrent_limit: int = 3, headless: bool = True) -> None:
        if concurrent_limit <= 0:
            raise ValueError(f"concurrent_limit must be positive, got {concurrent_limit}")
        self._concurrent_limit = concurrent_limit
        self._headless = headless
        self._lock = threading.RLock()
        # session_id → (Browser instance · launched_at)
        self._browsers: dict[str, tuple[Browser, datetime]] = {}
        self._playwright: Playwright | None = None

    # ── Protocol 实装 ───────────────────────────────────────

    def provision(
        self,
        profile_id: str | None,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        ttl_seconds: int = 300,
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> BrowserSession:
        """启一个 Chromium Browser · vendor 内部持有 · cdp_ws sentinel `local://<id>`.

        - `profile_id` 忽略(Local 不维护持久 profile · session_id 即 profile_id)
        - `initial_cookies` 忽略(`supports_cookies_at_create=False` · Pool 不会传给我们)
        - `fingerprint_hint` 仅 proxy 起作用 · 其他字段 launch 不接 · 仅 logger.debug 记录
        """
        if initial_cookies is not None:
            logger.warning(
                "local_playwright.initial_cookies_ignored",
                reason="supports_cookies_at_create=False · should be filtered by BrowserPool",
            )

        self._ensure_playwright()
        assert self._playwright is not None  # narrowing for mypy

        launch_opts = self._build_launch_opts(proxy, fingerprint_hint)
        try:
            browser = self._playwright.chromium.launch(**launch_opts)
        except Exception as e:
            logger.error("local_playwright.launch.failed", error=str(e), opts=launch_opts)
            raise

        session_id = uuid.uuid4().hex
        now = datetime.now()
        session = BrowserSession(
            session_id=session_id,
            vendor=self.name,
            vendor_session_ref="",  # Local 没 vendor 内部 ref
            profile_id=session_id,  # Local 用 session_id 当 profile_id
            cdp_ws=f"local://{session_id}",  # sentinel · Lane E executor 识别
            cdp_http=None,
            cdp_port=None,
            chromedriver_path=None,
            proxy_id=proxy.id if proxy is not None else None,
            fingerprint_ref=None,
            started_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

        with self._lock:
            self._browsers[session_id] = (browser, now)

        logger.info(
            "local_playwright.provision",
            session_id=session_id,
            headless=self._headless,
            has_proxy=proxy is not None,
        )
        return session

    def release(self, session_id: str) -> None:
        """关 Browser 实例."""
        with self._lock:
            entry = self._browsers.pop(session_id, None)
        if entry is None:
            logger.info("local_playwright.release.unknown_session", session_id=session_id)
            return
        browser, started_at = entry
        duration_s = (datetime.now() - started_at).total_seconds()
        try:
            browser.close()
            logger.info(
                "local_playwright.release",
                session_id=session_id,
                duration_seconds=duration_s,
            )
        except Exception as e:
            logger.warning(
                "local_playwright.release.close_failed",
                session_id=session_id,
                error=str(e),
            )

    def destroy(self, profile_id: str) -> None:
        """Local 无持久 profile · destroy 等价 release."""
        self.release(profile_id)

    def health(self) -> VendorHealth:
        """Local 永远 healthy · Playwright 没装直到 provision 才 fail."""
        return VendorHealth(
            vendor=self.name,
            healthy=True,
            last_check_at=datetime.now(),
        )

    def quota(self) -> VendorQuota:
        """concurrent_in_use 跟踪 active browser 数 · rate_limit 不限."""
        with self._lock:
            in_use = len(self._browsers)
        now = datetime.now()
        return VendorQuota(
            vendor=self.name,
            concurrent_in_use=in_use,
            concurrent_limit=self._concurrent_limit,
            rate_limit_remaining=99999,
            rate_limit_reset_at=now + _FAR_FUTURE_RESET,
        )

    def list_profiles(self) -> list[BrowserProfile]:
        """Local 不维护持久 profile · 返空."""
        return []

    def capabilities(self) -> set[str]:
        """Local 无私有扩展能力."""
        return set()

    # ── Lane E executor 用的 helper ─────────────────────────

    def get_browser(self, session_id: str) -> Browser:
        """取出 vendor 内部 Browser 实例 · Lane E executor 在 cdp_ws.startswith("local://")
        时调此方法 · 跳过 connect_over_cdp.

        Raises:
            KeyError: session_id 未 provision 过(或已 release)
        """
        with self._lock:
            entry = self._browsers.get(session_id)
        if entry is None:
            raise KeyError(f"local_playwright: session_id {session_id!r} not found")
        return entry[0]

    # ── 内部 ────────────────────────────────────────────────

    def _ensure_playwright(self) -> None:
        """懒启 Playwright runtime · 第一次 provision 时触发.

        若 SOURCERY_STEALTH=1 且 playwright-stealth 已装 · 通过
        Stealth.hook_playwright_context 将 stealth 脚本自动注入所有后续
        new_context() / new_page() 调用 · 不改动 Browser / Page 创建代码。
        """
        if self._playwright is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise PlaywrightNotInstalled() from e
        self._playwright = sync_playwright().start()
        logger.info("local_playwright.runtime.started", headless=self._headless)

        if _STEALTH_ENABLED and _HAS_STEALTH:
            # hook_playwright_context 拦截所有 Browser.new_context / new_page ·
            # 在每个新页面注入 stealth 脚本(navigator.webdriver 等 fingerprint 清理)
            Stealth().hook_playwright_context(self._playwright)
            logger.info("local_playwright.stealth.applied", method="hook_playwright_context")
        else:
            reason = "SOURCERY_STEALTH not set" if not _STEALTH_ENABLED else "playwright-stealth not installed"
            logger.debug("local_playwright.stealth.skipped", reason=reason)

    def _build_launch_opts(
        self, proxy: Proxy | None, fingerprint_hint: FingerprintHint | None
    ) -> dict[str, Any]:
        """把 Sourcery proxy + fp_hint 翻译成 Playwright `chromium.launch()` kwargs.

        - proxy → Playwright `proxy={"server": ..., "username": ..., "password": ...}`
        - fp_hint 大部分字段 launch 不支持(context-level)· 仅 logger.debug 记录
        """
        opts: dict[str, Any] = {"headless": self._headless}
        if proxy is not None:
            opts["proxy"] = self._sourcery_proxy_to_playwright(proxy)
        if fingerprint_hint is not None:
            ignored = {
                k: v
                for k, v in fingerprint_hint.model_dump(exclude={"raw"}).items()
                if v is not None
            }
            if ignored or fingerprint_hint.raw:
                logger.debug(
                    "local_playwright.fp_hint.partially_ignored",
                    note="chromium.launch doesn't support context-level fingerprint · "
                    "use tge/bit for real fingerprint control",
                    fp_fields_ignored=list(ignored.keys()),
                    raw_keys=list(fingerprint_hint.raw.keys()),
                )
        return opts

    @staticmethod
    def _sourcery_proxy_to_playwright(proxy: Proxy) -> dict[str, str]:
        """Sourcery `Proxy.endpoint`(完整 URL · 含可选 auth)→ Playwright dict.

        Playwright proxy 形态:
            {"server": "http://host:port" or "socks5://host:port",
             "username": <optional>, "password": <optional>}
        Sourcery `endpoint` 可能是:
            "http://host:port"
            "http://user:pass@host:port"
            "socks5://host:port"
        URL parsing 分离 auth · server 字段不带 auth。
        """
        from urllib.parse import urlparse

        parsed = urlparse(proxy.endpoint)
        scheme = parsed.scheme or proxy.type.value
        host = parsed.hostname or ""
        port = parsed.port
        server = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
        result: dict[str, str] = {"server": server}
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result

    # ── 程序退出钩子 ────────────────────────────────────────

    def close_all(self) -> None:
        """关所有 active Browser + Playwright runtime · 程序退出前调 · 幂等."""
        with self._lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()
        for browser, _started_at in browsers:
            try:
                browser.close()
            except Exception as e:
                logger.warning("local_playwright.close_all.browser_close_failed", error=str(e))
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as e:
                logger.warning("local_playwright.close_all.runtime_stop_failed", error=str(e))
            self._playwright = None
        logger.info("local_playwright.close_all.done", closed_count=len(browsers))
