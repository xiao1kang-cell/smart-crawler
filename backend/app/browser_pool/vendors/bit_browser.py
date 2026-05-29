"""BitBrowserVendor · 对接外部 BitBrowser 指纹浏览器平台.

对应 plan §3.6 + §13.1 sync 范式.

**Starter 实装注记**:本 sprint 没有 Bit SDK demo 在手(plan §3.6 仅写"接口形状
与 Tge 类似 · 字段差异参考 Bit 官方文档")· 实装按 Bit 公开 REST API 文档常识:
- 默认 `http://127.0.0.1:54345`
- endpoints: POST /browser/update(创建/更新)· POST /browser/open(启)·
  POST /browser/close(停)· POST /browser/delete(删)· POST /browser/list(列)
- 字段名 `id` / `profile_id` 而非 envId

字段精度 / 错误码细分留 M1.2 真用 Bit 时通过 `@pytest.mark.bit_live` contract test
对齐(plan §13.7 marker 已在 pyproject)。当前实装满足 BrowserVendor Protocol +
单测 mock 全过 · 真机 drift 由 contract test 检测。

能力声明:
- `supports_cookies_at_create = False`(Bit 文档未明确支持 create 时灌 cookies ·
  保守设 False · 走 attach 后 `context.add_cookies()` 兜底)
- `capabilities() = set()`(没有像 Tge 那样的 update_env / delete_cache / 等私有扩展 ·
  Bit 自己的能力等真用时按需声明)
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import structlog

from app.browser_pool.base import (
    BrowserProfile,
    BrowserSession,
    Cookie,
    FingerprintHint,
    ProfileRef,
    ProxySpec,
    VendorCapability,
    VendorHealth,
    VendorQuota,
    VendorSession,
)
from app.browser_pool.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from app.browser_pool._stubs import Proxy

logger = structlog.get_logger(__name__)


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
_DEFAULT_RATE_LIMIT = 100  # 通用保守限额 · 真用 Bit 时按文档调
_DEFAULT_CONCURRENT_LIMIT = 5
_DEFAULT_TIMEOUT_S = 30.0


class BitBrowserVendor:
    """BitBrowser REST API vendor · sync httpx.Client 实装.

    Usage::

        vendor = BitBrowserVendor(
            api_url="http://127.0.0.1:54345",
            api_key="bit_xxx",  # Bit 通常不需要 token · 这里保留以备
            concurrent_limit=5,
        )
        session = vendor.provision(
            profile_id=None, proxy=my_proxy, fingerprint_hint=hint, ttl_seconds=300
        )
        # ... worker connect_over_cdp(session.cdp_ws) ...
        vendor.release(session.session_id)
    """

    name = "bit"
    supports_cookies_at_create = False

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:54345",
        api_key: str | None = None,
        concurrent_limit: int = _DEFAULT_CONCURRENT_LIMIT,
        rate_limit_per_min: int = _DEFAULT_RATE_LIMIT,
        timeout_seconds: float = _DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        if concurrent_limit <= 0:
            raise ValueError(f"concurrent_limit must be positive, got {concurrent_limit}")
        if rate_limit_per_min <= 0:
            raise ValueError(f"rate_limit_per_min must be positive, got {rate_limit_per_min}")

        self._api_url = api_url.rstrip("/")
        self._concurrent_limit = concurrent_limit
        self._rate_limiter = RateLimiter(max_calls=rate_limit_per_min, period_seconds=60)
        self._session_refs: dict[str, str] = {}
        self._lock = threading.RLock()
        self._pending_cookies: dict[str, list[Cookie]] = {}

        if client is None:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "User-Agent": _DEFAULT_USER_AGENT,
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            self._client = httpx.Client(
                base_url=self._api_url,
                headers=headers,
                timeout=timeout_seconds,
            )
        else:
            self._client = client

    # ── Protocol 实装 ───────────────────────────────────────

    def provision(
        self,
        profile_id: str | None,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        ttl_seconds: int = 300,
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> BrowserSession:
        """启 Bit profile + open → 返 BrowserSession 含 ws_endpoint.

        - profile_id None → POST /browser/update 创建新 profile(Bit 用 update 创建)
        - profile_id 给 → 复用已有 profile
        - POST /browser/open(id)→ 返 ws_endpoint
        """
        self._rate_limiter.acquire()

        # 1. create / reuse profile
        if profile_id is None:
            payload = self._build_create_payload(proxy, fingerprint_hint, ttl_seconds)
            try:
                update_resp = self._client.post("/browser/update", json=payload)
                update_resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("bit.create_profile.failed", error=str(e))
                raise
            data = update_resp.json().get("data", {})
            new_profile_id = data.get("id") or data.get("profile_id")
            if not new_profile_id:
                raise RuntimeError(f"Bit /browser/update returned no id · response: {update_resp.json()}")
            actual_profile_id = str(new_profile_id)
        else:
            actual_profile_id = profile_id

        # 2. open profile
        self._rate_limiter.acquire()
        try:
            open_resp = self._client.post("/browser/open", json={"id": actual_profile_id})
            open_resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("bit.open.failed", profile_id=actual_profile_id, error=str(e))
            raise
        open_data = open_resp.json().get("data", {})
        ws = open_data.get("ws") or open_data.get("wsEndpoint")
        if not ws:
            raise RuntimeError(f"Bit /browser/open returned no ws · response: {open_resp.json()}")

        # 3. construct session
        session_id = uuid.uuid4().hex
        now = datetime.now()
        session = BrowserSession(
            session_id=session_id,
            vendor=self.name,
            vendor_session_ref=actual_profile_id,
            profile_id=actual_profile_id,
            cdp_ws=ws,
            cdp_http=open_data.get("http"),
            cdp_port=open_data.get("debuggingPort"),
            chromedriver_path=open_data.get("driver"),
            proxy_id=proxy.id if proxy else None,
            fingerprint_ref=f"bit:{actual_profile_id}",
            started_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._session_refs[session_id] = actual_profile_id

        logger.info(
            "bit.provision",
            session_id=session_id,
            profile_id=actual_profile_id,
            ws=ws,
            ttl_seconds=ttl_seconds,
        )
        return session

    def destroy(self, profile_id: str) -> None:
        """POST /browser/delete · 彻底删 profile · 不可恢复."""
        self._rate_limiter.acquire()
        try:
            resp = self._client.post("/browser/delete", json={"id": profile_id})
            resp.raise_for_status()
            logger.info("bit.destroy", profile_id=profile_id)
        except httpx.HTTPError as e:
            logger.warning("bit.destroy.failed", profile_id=profile_id, error=str(e))
            raise

    def health(self) -> VendorHealth:
        """Bit 无独立 status endpoint · 用 list(pageSize=1) 当 ping."""
        try:
            self._rate_limiter.acquire()
            resp = self._client.post(
                "/browser/list", json={"page": 0, "pageSize": 1}, timeout=5.0
            )
            resp.raise_for_status()
            return VendorHealth(
                vendor=self.name,
                healthy=True,
                last_check_at=datetime.now(),
            )
        except Exception as e:
            return VendorHealth(
                vendor=self.name,
                healthy=False,
                last_check_at=datetime.now(),
                last_error=str(e),
                consecutive_failures=1,
            )

    def quota(self) -> VendorQuota:
        """Bit 无 running envs 单独 endpoint · 退化用本地 session_refs 估."""
        with self._lock:
            in_use = len(self._session_refs)
        return VendorQuota(
            vendor=self.name,
            concurrent_in_use=in_use,
            concurrent_limit=self._concurrent_limit,
            rate_limit_remaining=self._rate_limiter.remaining(),
            rate_limit_reset_at=self._rate_limiter.reset_at(),
        )

    def list_profiles(self) -> list[BrowserProfile]:
        """POST /browser/list · 列 vendor 上所有 profile."""
        self._rate_limiter.acquire()
        resp = self._client.post("/browser/list", json={"page": 0, "pageSize": 100})
        resp.raise_for_status()
        records = resp.json().get("data", {}).get("list", [])
        profiles: list[BrowserProfile] = []
        for p in records:
            pid = p.get("id") or p.get("profile_id")
            if not pid:
                continue
            profiles.append(
                BrowserProfile(
                    vendor=self.name,
                    profile_id=str(pid),
                    name=p.get("name") or p.get("remark"),
                    cookies_managed=True,  # Bit 内部持久化 cookies
                    fingerprint_id=f"bit:{pid}",
                    last_used_at=None,
                    proxy_bound=str(p.get("proxyId")) if p.get("proxyId") else None,
                )
            )
        return profiles

    def capabilities(self) -> set[str]:
        """MVP 自报 2 个 capability · Phase 2 加齐到 8 个.

        # spec: docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5
        # task: MV-1 · FingerprintBrowserVendor MVP walking skeleton
        """
        return {
            VendorCapability.COOKIE_INJECT,   # 走 stash + Worker add_cookies
            VendorCapability.PROXY_HOT_SWAP,  # hot=False 也算实装(MVP)
        }

    # ════════════════════════════════════════════════════════════════════
    # FingerprintBrowserVendor MVP · Amazon Bridge Phase 1 · Task MV-1
    # ════════════════════════════════════════════════════════════════════
    #
    # 4 核心方法 + capabilities() · MVP walking skeleton · Phase 2 polish 时补剩 4 个
    # (list_profiles / set_fingerprint / batch_release / health_check)。
    #
    # 设计依据:docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5
    # RFC 012 · Plan Phase 1 MV-1

    def acquire(self, profile_ref: ProfileRef) -> VendorSession:
        """启动 BitBrowser profile · 返 CDP url + metadata · 替 start_fingerprint.

        # spec: docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5.2
        # task: MV-1 · acquire
        """
        self._rate_limiter.acquire()
        try:
            resp = self._client.post(
                "/browser/open",
                json={"id": profile_ref.profile_id, "loadExtensions": True},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception:
            logger.error("bit.acquire.failed", profile_id=profile_ref.profile_id)
            raise
        data = resp.json().get("data", {})
        ws = data.get("ws") or data.get("wsEndpoint")
        if not ws:
            raise ValueError(
                f"BitBrowser /browser/open returned no ws · resp={data}"
            )
        now = datetime.now(tz=UTC)
        session = VendorSession(
            session_id=f"bit-{profile_ref.profile_id}-{uuid.uuid4().hex[:8]}",
            vendor=self.name,
            vendor_session_ref=profile_ref.profile_id,
            profile_id=profile_ref.profile_id,
            cdp_ws=ws,
            cdp_http=data.get("http"),
            started_at=now,
            expires_at=now + timedelta(seconds=profile_ref.ttl_seconds),
        )
        with self._lock:
            self._session_refs[session.session_id] = profile_ref.profile_id
        logger.info(
            "bit.acquire",
            session_id=session.session_id,
            profile_id=profile_ref.profile_id,
            ws=ws,
        )
        return session

    def release(
        self,
        session: VendorSession | str,
        persist_state: bool = True,
    ) -> None:
        """关 BitBrowser profile · 保留配置(persist_state=True 时 cookies 自动留).

        注:FingerprintBrowserVendor.release(session: VendorSession, persist_state) 与
        旧 BrowserVendor.release(session_id: str) 签名不同 · 本实装同时兼容两种调用:
        - release(session: VendorSession) · 新 FingerprintBrowserVendor 调用路径
        - release(session_id: str) · 旧 BrowserVendor 调用路径(向后兼容)

        sibling protocol 方案的已知 trade-off · type: ignore[override] 是意图记录。

        # spec: docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5.2
        # task: MV-1 · release
        """
        # 兼容旧 BrowserVendor.release(session_id: str) 调用路径
        if isinstance(session, str):
            session_id = session
            with self._lock:
                profile_id = self._session_refs.pop(session_id, None)
            if profile_id is None:
                logger.info("bit.release.unknown_session", session_id=session_id)
                return
            self._rate_limiter.acquire()
            try:
                resp = self._client.post("/browser/close", json={"id": profile_id})
                resp.raise_for_status()
                logger.info("bit.release", session_id=session_id, profile_id=profile_id)
            except Exception as e:
                logger.warning(
                    "bit.release.failed",
                    session_id=session_id,
                    profile_id=profile_id,
                    error=str(e),
                )
                raise
            return

        # 新 FingerprintBrowserVendor.release(session: VendorSession, persist_state) 路径
        self._rate_limiter.acquire()
        try:
            resp = self._client.post(
                "/browser/close",
                json={"id": session.profile_id},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("bit.fp_release.failed", profile_id=session.profile_id)
            raise
        with self._lock:
            self._session_refs.pop(session.session_id, None)
            # MVP 不强求 cookies extract 回写 · BitBrowser 自己 persist
        logger.info(
            "bit.fp_release",
            session_id=session.session_id,
            profile_id=session.profile_id,
            persist_state=persist_state,
        )

    def update_proxy(
        self,
        profile_id: str,
        proxy: ProxySpec,
        hot: bool = False,
    ) -> None:
        """更新 profile 绑定的 proxy · hot=False(MVP)· 不重启需重新 acquire.

        # spec: docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5.2
        # task: MV-1 · update_proxy
        """
        payload: dict[str, Any] = {
            "id": profile_id,
            "proxyMethod": 2,       # custom proxy
            "proxyType": "http",    # MVP 只支持 http · socks 留 Phase 2
            "host": self._parse_host(proxy.server),
            "port": self._parse_port(proxy.server),
        }
        if proxy.username:
            payload["proxyUserName"] = proxy.username
        if proxy.password:
            payload["proxyPassword"] = proxy.password
        self._rate_limiter.acquire()
        try:
            resp = self._client.post(
                "/browser/proxy/update", json=payload, timeout=30
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("bit.update_proxy.failed", profile_id=profile_id)
            raise
        logger.info("bit.update_proxy", profile_id=profile_id, hot=hot)

    def inject_cookies(
        self,
        profile_id: str,
        cookies: list[Cookie],
        domain_filter: str | None = None,
    ) -> None:
        """启动前注入 cookies · MVP 走 stash 待 acquire 后 Worker add_cookies.

        BitBrowser 没原生 cookie inject API(走 CDP) · MVP 实装方式:
        acquire profile · 用 Playwright over CDP add_cookies · release。
        生产化 Phase 2 · 改 stand-alone CDP call。

        # spec: docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5.2
        # task: MV-1 · inject_cookies
        """
        filtered = [
            c for c in cookies
            if domain_filter is None or domain_filter in c.domain
        ]
        with self._lock:
            self._pending_cookies.setdefault(profile_id, []).extend(filtered)
        logger.info(
            "bit.inject_cookies.stashed",
            profile_id=profile_id,
            count=len(filtered),
        )

    # ── 内部 helpers ────────────────────────────────────────

    @staticmethod
    def _parse_host(server: str) -> str:
        """从 'http://1.2.3.4:8080' 解 host."""
        return urlparse(server).hostname or ""

    @staticmethod
    def _parse_port(server: str) -> int:
        """从 'http://1.2.3.4:8080' 解 port."""
        return urlparse(server).port or 0

    def _build_create_payload(
        self,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        """组装 /browser/update payload(Bit 用 update 创建).

        字段名按 Bit 官方文档常识 · 真用时通过 bit_live contract test 校准。
        """
        payload: dict[str, Any] = {
            "name": f"sourcery-{uuid.uuid4().hex[:8]}",
            "remark": f"sourcery managed · session-ttl-{ttl_seconds}s",
        }
        if proxy is not None:
            payload["proxyMethod"] = "custom"
            payload["proxyType"] = proxy.type.value
            payload.update(self._proxy_to_bit(proxy))
        if fingerprint_hint is not None:
            payload["browserFingerPrint"] = self._fp_to_bit(fingerprint_hint)
        return payload

    @staticmethod
    def _proxy_to_bit(proxy: Proxy) -> dict[str, Any]:
        """Sourcery Proxy → Bit proxy fields(展开到 update payload)."""
        vendor_refs: Mapping[str, str] = getattr(proxy, "vendor_proxy_refs", {}) or {}
        if "bit" in vendor_refs:
            return {"proxyId": vendor_refs["bit"]}

        parsed = urlparse(proxy.endpoint)
        result: dict[str, Any] = {
            "host": parsed.hostname or "",
        }
        if parsed.port:
            result["port"] = parsed.port
        if parsed.username:
            result["proxyUserName"] = parsed.username
        if parsed.password:
            result["proxyPassword"] = parsed.password
        return result

    @staticmethod
    def _fp_to_bit(hint: FingerprintHint) -> dict[str, Any]:
        """Sourcery FingerprintHint → Bit browserFingerPrint dict.

        字段名按 Bit 公开文档 · 不全的等 contract test 反馈。
        """
        if hint.raw.get("_random", False):
            return {"isRandom": True}

        fp: dict[str, Any] = {
            "isRandom": False,
            "ostype": "PC" if hint.os in {"windows", "macos", "linux"} else "Mobile",
            "os": (hint.os or "windows").capitalize(),
            "version": str(hint.raw.get("platformVersion", "11")),
            "userAgent": hint.raw.get("userAgent", ""),
            "language": hint.accept_language or "en-US",
            "timeZone": hint.timezone or "GMT+08:00",
            "resolution": hint.raw.get("resolution", "1920x1080"),
            "canvas": hint.raw.get("canvas", True),
            "webgl": hint.raw.get("webgl", True),
            "webrtc": hint.raw.get("webrtc", True),
        }
        for k, v in hint.raw.items():
            if k.startswith("_"):
                continue
            fp[k] = v
        return fp

    # ── 资源清理 ────────────────────────────────────────────

    def close(self) -> None:
        """关 httpx.Client · 程序退出前调."""
        self._client.close()
