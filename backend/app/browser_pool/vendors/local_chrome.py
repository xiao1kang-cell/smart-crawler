"""LocalChromeVendor · Sourcery 第 3 vendor · 接用户已启动 Chrome via CDP.

Use case:
- 单人 / 小项目场景 · 不需要超高并发 / 多账号轮换
- 用户已有 Chrome + 真 cookies + 真历史 · 绕反爬免费
- 不需要 BitBrowser / TgeBrowser 商业指纹浏览器

User 启动 Chrome(给路径示例 Windows):
    chrome.exe --remote-debugging-port=9222 \\
      --user-data-dir=C:/sourcery-chrome-data

(独立 user-data-dir · 防止跟用户主 Chrome 冲突 · 第一次启动空 profile ·
用户手动登录 / 浏览一下养 cookies · 后续 Sourcery 复用)

Sourcery 配:
    export SOURCERY_CHROME_CDP_URL=http://localhost:9222
    或 Recipe 加:
        resource_policy:
          browser_vendor_chain: [local_chrome]

跑:
    uv run python -m sourcery run examples/amazon_product_reviews_recent.yaml \\
        --input asin=B0DSWGSDQJ --input tld=com

设计依据:
- amazon_crawler shuler/util/local_browser.py(422 LOC)pattern · DROP 大头(Selenium/ws/Drission 接管)
  · 走 Playwright connect_over_cdp 干净版
- 跟现 LocalPlaywrightVendor 同范式 · 实装相同 7 method
- 不实装 FingerprintBrowserVendor 子协议(无 profile 体系 · 跟 LocalPlaywright 一致)
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from app.browser_pool.base import (
    BrowserProfile,
    BrowserSession,
    FingerprintHint,
    PoolExhausted,
    VendorHealth,
    VendorQuota,
)

if TYPE_CHECKING:
    from app.browser_pool._stubs import Proxy

logger = structlog.get_logger(__name__)

_FAR_FUTURE_RESET = timedelta(days=365)


class LocalChromeVendor:
    """Connect-over-CDP vendor · 接用户预启动的 Chrome.

    跟 LocalPlaywrightVendor 区别:
    - LocalPlaywrightVendor:Sourcery 启动 Chromium · 走 Playwright launch
    - LocalChromeVendor:用户启动 Chrome(--remote-debugging-port) · Sourcery
      connect_over_cdp 接管 · 用户 Chrome 在 Sourcery 不跑时仍能用

    Release 语义:关 page · 留 context · 留用户 Chrome(不杀用户进程)。
    Destroy 是 no-op · LocalChrome 不管 profile 持久化。
    """

    name = "local_chrome"
    supports_cookies_at_create = False  # cookies 由用户 Chrome 自带 · provision 不灌

    def __init__(
        self,
        cdp_url: str | None = None,
        concurrent_limit: int = 10,
    ) -> None:
        self.cdp_url = cdp_url or os.environ.get(
            "SOURCERY_CHROME_CDP_URL", "http://localhost:9222"
        )
        self._concurrent_limit = concurrent_limit
        self._lock = threading.RLock()
        # session_id → (browser, context, page)
        self._sessions: dict[str, tuple[Any, Any, Any]] = {}
        self._playwright: Any | None = None

    # ── Protocol 实装 ───────────────────────────────────────

    def provision(
        self,
        profile_id: str | None,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        ttl_seconds: int = 300,
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> BrowserSession:
        """connect_over_cdp 到用户 Chrome · 创建新 page.

        - `initial_cookies` 忽略(`supports_cookies_at_create=False` · Pool 不会传给我们)
        - `proxy` 忽略(用户 Chrome 自己的网络 · Sourcery 不插手)
        - `fingerprint_hint` 忽略(用户 Chrome 真指纹 · 无需 hint)
        """
        if initial_cookies is not None:
            logger.warning(
                "local_chrome.initial_cookies_ignored",
                reason="supports_cookies_at_create=False · should be filtered by BrowserPool",
            )
        if proxy is not None:
            logger.debug(
                "local_chrome.proxy_ignored",
                note="LocalChromeVendor 不插入代理 · 用户 Chrome 自带网络",
            )
        if fingerprint_hint is not None:
            logger.debug(
                "local_chrome.fp_hint_ignored",
                note="LocalChromeVendor 用用户真指纹 · fp_hint 无效",
            )

        pw = self._ensure_playwright()

        try:
            browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
        except Exception as exc:
            raise PoolExhausted(
                vendor_chain=[self.name],
                errors=[
                    f"connect_over_cdp({self.cdp_url}) 失败 · 用户 Chrome 未启动或没加 "
                    f"--remote-debugging-port?具体错:{exc}"
                ],
                error_code=1000,
            ) from exc

        # 取已有 context(用户 Chrome 第一个 context 即 default)· 或新建
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        session_id = uuid.uuid4().hex
        now = datetime.now()
        session = BrowserSession(
            session_id=session_id,
            vendor=self.name,
            vendor_session_ref="",  # LocalChrome 没 vendor 内部 ref
            profile_id=profile_id or "default",
            cdp_ws=self.cdp_url,  # Worker reconnect 用同一个 URL
            cdp_http=self.cdp_url,
            cdp_port=None,
            chromedriver_path=None,
            proxy_id=None,
            fingerprint_ref=None,
            started_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

        with self._lock:
            self._sessions[session_id] = (browser, ctx, page)

        logger.info(
            "local_chrome.provision",
            session_id=session_id,
            cdp_url=self.cdp_url,
        )
        return session

    def release(self, session_id: str) -> None:
        """关 page · 留 context · 留用户 Chrome.

        重要:不关 browser · 不关 context · 用户 Chrome 继续跑。
        """
        with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            logger.info("local_chrome.release.unknown_session", session_id=session_id)
            return
        _browser, _ctx, page = entry
        try:
            page.close()
            logger.info("local_chrome.release", session_id=session_id)
        except Exception as exc:
            logger.warning(
                "local_chrome.release.page_close_failed",
                session_id=session_id,
                error=str(exc),
            )

    def destroy(self, profile_id: str) -> None:
        """LocalChromeVendor 不管 profile 持久化 · destroy 是 no-op."""
        logger.debug(
            "local_chrome.destroy.noop",
            profile_id=profile_id,
            note="LocalChromeVendor 不持有 profile · destroy 无操作",
        )

    def health(self) -> VendorHealth:
        """检查 CDP endpoint /json/version · 3s timeout · 不卡."""
        import httpx

        now = datetime.now()
        try:
            resp = httpx.get(f"{self.cdp_url}/json/version", timeout=3.0)
            resp.raise_for_status()
            return VendorHealth(
                vendor=self.name,
                healthy=True,
                last_check_at=now,
            )
        except Exception as exc:
            return VendorHealth(
                vendor=self.name,
                healthy=False,
                last_check_at=now,
                last_error=f"CDP unreachable at {self.cdp_url}: {exc}",
                consecutive_failures=1,
            )

    def quota(self) -> VendorQuota:
        """无 vendor quota · 仅 concurrent_limit 约束."""
        with self._lock:
            in_use = len(self._sessions)
        now = datetime.now()
        return VendorQuota(
            vendor=self.name,
            concurrent_in_use=in_use,
            concurrent_limit=self._concurrent_limit,
            rate_limit_remaining=999999,  # 用户自己 Chrome · 无限速
            rate_limit_reset_at=now + _FAR_FUTURE_RESET,
        )

    def list_profiles(self) -> list[BrowserProfile]:
        """LocalChrome 不管 profile · 返单 default."""
        return [
            BrowserProfile(
                vendor=self.name,
                profile_id="default",
                name="User's Chrome",
                cookies_managed=False,
            )
        ]

    def capabilities(self) -> set[str]:
        """返本 vendor 私有能力.

        `connect_over_cdp` + `real_user_cookies` + `real_user_fingerprint` 三者
        合起来区分 LocalChromeVendor 跟 LocalPlaywrightVendor(后者 capabilities 为空)。
        """
        return {
            "connect_over_cdp",
            "real_user_cookies",  # 跟 LocalPlaywright 区分关键能力
            "real_user_fingerprint",
        }

    # ── Lane E executor helper ─────────────────────────────

    def get_page(self, session_id: str) -> Any:
        """取出 vendor 内部 Page 实例 · Lane E executor 复用已建 page 时调.

        Raises:
            KeyError: session_id 未 provision 过(或已 release)
        """
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise KeyError(f"local_chrome: session_id {session_id!r} not found")
        return entry[2]  # (browser, ctx, page) → page

    # ── 程序退出钩子 ────────────────────────────────────────

    def close_all(self) -> None:
        """关所有 active page + Playwright runtime · 程序退出前调 · 幂等.

        注意:不关用户 Chrome 浏览器本身 · 仅关 Playwright 侧资源。
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for _browser, _ctx, page in sessions:
            try:
                page.close()
            except Exception as exc:
                logger.warning("local_chrome.close_all.page_close_failed", error=str(exc))
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.warning(
                    "local_chrome.close_all.runtime_stop_failed", error=str(exc)
                )
            self._playwright = None
        logger.info("local_chrome.close_all.done", closed_count=len(sessions))

    # ── 内部 ────────────────────────────────────────────────

    def _ensure_playwright(self) -> Any:
        """懒启 Playwright runtime · 第一次 provision 时触发."""
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright 未安装 · 运行 'uv sync --extra browser' 后再用 LocalChromeVendor"
            ) from exc
        self._playwright = sync_playwright().start()
        logger.info("local_chrome.playwright_runtime.started")
        return self._playwright
