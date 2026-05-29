"""TgeBrowserVendor · 对接外部 TgeBrowser 指纹浏览器平台.

对应 plan §3.5 + §3.5.1~§3.5.4 + §13.1 锁的 sync 范式(httpx.Client · 非 async).

实装基础(plan §3.5):
- `httpx.Client` REST + Bearer token + 必带 User-Agent(SDK demo 实测必须)
- 13 个 endpoints:create/update/start/stop/delete/delete_cache/list/list_open/
  list_groups/list_proxies/sort/custom_sort + 隐式 health
- envId 是 vendor 内部 ref(create_env 返回 `data.envId`)
- start_env 返回 `data.ws` 是 CDP ws_endpoint · 直接 `connect_over_cdp(ws)` 接管
- RateLimiter 100/60s(Tge 文档限额)

§3.5.1 · Proxy 双轨:
- `Proxy.endpoint` URL parse 出 inline {protocol/host/port/username/password}
- 或调用方在 Proxy 实体上注入 `vendor_proxy_refs={"tge": "92852"}` 走 proxyId(Lane F)
- Lane B 阶段没有 vendor_proxy_refs 字段 · 走 inline 路径

§3.5.2 · Fingerprint 字段映射:
- FingerprintHint → Tge fp dict · 含 *BaseIp 智能默认(proxy IP 推 tz / lang / lat-lng)
- hint.raw 透传 vendor-specific 字段

§3.5.3 · Cookies at create(P1-1):
- `supports_cookies_at_create = True`
- `provision(initial_cookies=...)` 时 · Pool 把 cookies 传过来 · 转 JSON 字符串塞进 create_env 的 Cookie 字段
- 不传时 · Pool 内部填 None · Tge 自行不灌

§3.5.4 · Vendor extensions(P1-5 capabilities 声明):
- update_env / delete_env_cache / list_running / list_groups / list_proxies /
  window_sort / window_sort_custom 共 7 个 Tge 私有能力 · 走 capabilities() 声明 ·
  CLI dispatch 用
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import httpx
import structlog

from app.browser_pool.base import (
    BrowserProfile,
    BrowserSession,
    FingerprintHint,
    VendorHealth,
    VendorQuota,
)
from app.browser_pool.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from app.browser_pool._stubs import Proxy

logger = structlog.get_logger(__name__)


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
_DEFAULT_RATE_LIMIT = 100  # calls per minute(Tge 文档)
_DEFAULT_CONCURRENT_LIMIT = 5
_DEFAULT_TIMEOUT_S = 30.0


class TgeBrowserVendor:
    """TgeBrowser REST API vendor 实装.

    Usage::

        vendor = TgeBrowserVendor(
            api_url="http://127.0.0.1:50326",
            api_key="asp_xxx...",
            concurrent_limit=5,
        )
        session = vendor.provision(
            profile_id=None,        # 新建 env
            proxy=my_proxy,
            fingerprint_hint=FingerprintHint(os="windows", timezone="Asia/Shanghai"),
            ttl_seconds=300,
            initial_cookies=[{"name": "session", "value": "abc", "domain": ".amazon.com"}],
        )
        # ... worker connect_over_cdp(session.cdp_ws) ...
        vendor.release(session.session_id)
    """

    name = "tge"
    supports_cookies_at_create = True

    def __init__(
        self,
        api_url: str,
        api_key: str,
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
        # session_id → vendor_session_ref(envId)· 释放时查
        self._session_refs: dict[str, str] = {}
        self._lock = threading.RLock()

        # 客户端注入(测试可传 mock)
        if client is None:
            self._client = httpx.Client(
                base_url=self._api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": _DEFAULT_USER_AGENT,
                },
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
        """启 Tge env + start → 返 BrowserSession 含 ws_endpoint.

        - profile_id None → POST /api/browser/create 新建 env(返回 envId)
        - profile_id 给 → 复用已有 envId · 跳过 create
        - 然后 POST /api/browser/start(envId)→ 返 ws_endpoint
        - cookies(若给)塞 create_env.Cookie 字段(JSON 字符串)
        """
        self._rate_limiter.acquire()

        # 1. create / reuse env
        if profile_id is None:
            payload = self._build_create_payload(proxy, fingerprint_hint, initial_cookies, ttl_seconds)
            try:
                create_resp = self._client.post("/api/browser/create", json=payload)
                create_resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("tge.create_env.failed", error=str(e), payload_keys=list(payload.keys()))
                raise
            data = create_resp.json().get("data", {})
            env_id = data.get("envId")
            if env_id is None:
                raise RuntimeError(f"Tge create_env returned no envId · response: {create_resp.json()}")
            env_id_str = str(env_id)
        else:
            env_id_str = profile_id

        # 2. start env
        self._rate_limiter.acquire()
        start_payload: dict[str, Any] = {"envId": int(env_id_str) if env_id_str.isdigit() else env_id_str}
        if fingerprint_hint is not None and fingerprint_hint.raw.get("_headless"):
            start_payload["args"] = ["--headless"]
        try:
            start_resp = self._client.post("/api/browser/start", json=start_payload)
            start_resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("tge.start_env.failed", env_id=env_id_str, error=str(e))
            raise
        start_data = start_resp.json().get("data", {})
        ws = start_data.get("ws")
        if not ws:
            raise RuntimeError(f"Tge start_env returned no ws · response: {start_resp.json()}")

        # 3. construct session
        session_id = uuid.uuid4().hex
        now = datetime.now()
        session = BrowserSession(
            session_id=session_id,
            vendor=self.name,
            vendor_session_ref=env_id_str,
            profile_id=env_id_str,
            cdp_ws=ws,
            cdp_http=start_data.get("http"),
            cdp_port=start_data.get("port"),
            chromedriver_path=start_data.get("driver"),
            proxy_id=proxy.id if proxy else None,
            fingerprint_ref=f"tge:{env_id_str}",
            started_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._session_refs[session_id] = env_id_str

        logger.info(
            "tge.provision",
            session_id=session_id,
            env_id=env_id_str,
            ws=ws,
            ttl_seconds=ttl_seconds,
            had_initial_cookies=initial_cookies is not None,
        )
        return session

    def release(self, session_id: str) -> None:
        """POST /api/browser/stop(envId)· 保留 env 不删."""
        with self._lock:
            env_id = self._session_refs.pop(session_id, None)
        if env_id is None:
            logger.info("tge.release.unknown_session", session_id=session_id)
            return
        try:
            self._rate_limiter.acquire()
            resp = self._client.post(
                "/api/browser/stop",
                json={"envId": int(env_id) if env_id.isdigit() else env_id},
            )
            resp.raise_for_status()
            logger.info("tge.release", session_id=session_id, env_id=env_id)
        except httpx.HTTPError as e:
            logger.warning("tge.release.failed", session_id=session_id, env_id=env_id, error=str(e))
            raise

    def destroy(self, profile_id: str) -> None:
        """POST /api/browser/delete · 彻底删 env · 不可恢复."""
        self._rate_limiter.acquire()
        try:
            resp = self._client.post(
                "/api/browser/delete",
                json={"envId": int(profile_id) if profile_id.isdigit() else profile_id},
            )
            resp.raise_for_status()
            logger.info("tge.destroy", profile_id=profile_id)
        except httpx.HTTPError as e:
            logger.warning("tge.destroy.failed", profile_id=profile_id, error=str(e))
            raise

    def health(self) -> VendorHealth:
        """Tge 无独立 /api/status · 用 list_envs(pageSize=1) 当 ping."""
        try:
            self._rate_limiter.acquire()
            resp = self._client.get("/api/browser/list?current=1&pageSize=1", timeout=5.0)
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
        """数 /api/browser/open/list 当前 running envs · rate_limit_remaining 从内部 limiter.

        真机 contract(Tge desktop 0.50326)· `{"success": true, "data": [...]}` ·
        `data` 直接是 list。旧 mock 假设 `data.records` envelope · 真机不匹配 · 两种格式都兼容。
        """
        in_use = 0
        try:
            self._rate_limiter.acquire()
            resp = self._client.get("/api/browser/open/list?current=1&pageSize=100")
            resp.raise_for_status()
            in_use = len(_parse_tge_records(resp.json()))
        except Exception as e:
            logger.warning("tge.quota.in_use_query_failed", error=str(e))
            # 退化:用本地 session_refs 估计
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
        """GET /api/browser/list · 全量 envs(含已停)· CLI / 控制台用.

        真机 contract(Tge desktop 0.50326)· `data` 是 `{"total":N, "current":1, "pageSize":100, "list":[...]}` ·
        `list` 字段而非 `records`。旧 mock 假设 `records` · 走 `_parse_tge_records` 兼容两种。
        """
        self._rate_limiter.acquire()
        resp = self._client.get("/api/browser/list?current=1&pageSize=100")
        resp.raise_for_status()
        records = _parse_tge_records(resp.json())
        profiles: list[BrowserProfile] = []
        for p in records:
            env_id = p.get("envId")
            if env_id is None:
                continue
            profiles.append(
                BrowserProfile(
                    vendor=self.name,
                    profile_id=str(env_id),
                    name=p.get("browserName"),
                    cookies_managed=True,
                    fingerprint_id=f"tge:{env_id}",
                    last_used_at=None,  # Tge 未返回 · 留空
                    proxy_bound=str(p.get("proxyId")) if p.get("proxyId") else None,
                )
            )
        return profiles

    def capabilities(self) -> set[str]:
        """Tge 私有能力声明 · CLI dispatch 用 · 见 plan §13.6."""
        return {
            "cookies_at_create",
            "update_env",
            "delete_cache",
            "list_running",
            "list_groups",
            "list_proxies",
            "window_sort",
        }

    # ── Tge 私有 extensions(P1-5 capabilities) ───────────

    def update_env(
        self,
        env_id: str | int,
        *,
        proxy: dict[str, Any] | None = None,
        fingerprint: dict[str, Any] | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/browser/update · 改 env 配置而不销毁."""
        self._rate_limiter.acquire()
        payload: dict[str, Any] = {"envId": int(env_id) if str(env_id).isdigit() else env_id}
        if proxy is not None:
            payload["proxy"] = proxy
        if fingerprint is not None:
            payload["fingerprint"] = fingerprint
        if remark is not None:
            payload["remark"] = remark
        resp = self._client.post("/api/browser/update", json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def delete_cache(self, env_id: str | int) -> None:
        """POST /api/browser/cache/delete · 清缓存但保留 env."""
        self._rate_limiter.acquire()
        resp = self._client.post(
            "/api/browser/cache/delete",
            json={"envId": int(env_id) if str(env_id).isdigit() else env_id},
        )
        resp.raise_for_status()

    def list_running(self) -> list[dict[str, Any]]:
        """GET /api/browser/open/list · 当前 running envs · `_parse_tge_records` 兼容三种 envelope."""
        self._rate_limiter.acquire()
        resp = self._client.get("/api/browser/open/list?current=1&pageSize=100")
        resp.raise_for_status()
        return _parse_tge_records(resp.json())

    def list_groups(self) -> list[dict[str, Any]]:
        """GET /api/groups/list · Tge 内部分组 · `_parse_tge_records` 兼容三种 envelope."""
        self._rate_limiter.acquire()
        resp = self._client.get("/api/groups/list?current=1&pageSize=100")
        resp.raise_for_status()
        return _parse_tge_records(resp.json())

    def list_proxies(self) -> list[dict[str, Any]]:
        """GET /api/proxies/list · Tge 内置代理库 · `_parse_tge_records` 兼容三种 envelope."""
        self._rate_limiter.acquire()
        resp = self._client.get("/api/proxies/list?current=1&pageSize=100")
        resp.raise_for_status()
        return _parse_tge_records(resp.json())

    def window_sort(
        self, env_ids: list[int], layout: Literal["grid", "box"] = "grid"
    ) -> dict[str, Any]:
        """POST /api/windowbounds/sort 或 sort/custom · 多窗口布局."""
        self._rate_limiter.acquire()
        if layout == "box":
            payload: dict[str, Any] = {
                "type": "box",
                "startX": 0,
                "startY": 0,
                "width": 500,
                "height": 400,
                "col": 3,
                "spaceX": 50,
                "spaceY": 50,
                "offsetX": 50,
                "offsetY": 50,
                "envIds": env_ids,
            }
            resp = self._client.post("/api/windowbounds/sort/custom", json=payload)
        else:
            resp = self._client.post("/api/windowbounds/sort", json={"envIds": env_ids})
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # ── 内部 helpers ────────────────────────────────────────

    def _build_create_payload(
        self,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        initial_cookies: list[dict[str, Any]] | None,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        """组装 /api/browser/create payload."""
        payload: dict[str, Any] = {
            "browserName": f"sourcery-{uuid.uuid4().hex[:8]}",
            "remark": f"sourcery managed · session-ttl-{ttl_seconds}s",
            "Cookie": json.dumps(initial_cookies) if initial_cookies else "",
            "startInfo": {
                "startPage": {"mode": "custom", "value": []},
                "otherConfig": {
                    "openConfigPage": False,
                    "checkPage": False,
                    "extensionTab": False,
                },
            },
        }
        if proxy is not None:
            payload["proxy"] = self._proxy_to_tge(proxy)
        else:
            # 真机 contract(0.50326)· `proxy` 字段必填 · 缺则 400 "Either proxyId or proxy info must be provided"
            # `protocol: "noproxy"` 会被 "proxy 字段验证失败" 拒 · vendor 期望 demo-style 完整 proxy 对象
            # 默认走 demo 风格 stub(socks5 + 127.0.0.1:7890)· 跑不通代理但能过 vendor 字段校验。
            # ResourcePool 走通时调用方会传 real Proxy · 这条 fallback 仅给 CLI test / contract test 用。
            payload["proxy"] = {"protocol": "socks5", "host": "127.0.0.1", "port": 7890}
        if fingerprint_hint is not None:
            payload["fingerprint"] = self._fp_to_tge(fingerprint_hint)
        else:
            # 真机 contract(0.50326)· `fingerprint.os` 必填 · `randomFingerprint=True` 单字段会被 400 拒
            payload["fingerprint"] = {"randomFingerprint": True, "os": "Windows"}
        return payload

    @staticmethod
    def _proxy_to_tge(proxy: Proxy) -> dict[str, Any]:
        """Sourcery Proxy → Tge proxy dict.

        Plan §13.3 锁定 Account/Proxy 用 `vendor_proxy_refs: dict[str, str]` 多 vendor map ·
        但 Lane F 才加该字段。Lane C 阶段:
        - 检查 `getattr(proxy, "vendor_proxy_refs", {})` 取 "tge" key(向前兼容 Lane F)
        - fallback 走 inline · 从 endpoint URL parse
        """
        vendor_refs: Mapping[str, str] = getattr(proxy, "vendor_proxy_refs", {}) or {}
        if "tge" in vendor_refs:
            return {"proxyId": int(vendor_refs["tge"]) if vendor_refs["tge"].isdigit() else vendor_refs["tge"]}

        parsed = urlparse(proxy.endpoint)
        scheme = parsed.scheme or proxy.type.value
        result: dict[str, Any] = {
            "protocol": scheme,
            "host": parsed.hostname or "",
        }
        if parsed.port:
            result["port"] = parsed.port
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        result["ipChecker"] = "https://ipinfo.io"
        return result

    @staticmethod
    def _fp_to_tge(hint: FingerprintHint) -> dict[str, Any]:
        """Sourcery FingerprintHint → Tge fp dict · 30+ 字段映射(plan §3.5.2).

        默认开 `*BaseIp` 智能字段(让 Tge 按 proxy IP 推 timezone / language / lat-lng)·
        hint 显式指定时关。
        """
        if hint.raw.get("_random", False):
            return {"randomFingerprint": True}

        fp: dict[str, Any] = {
            "randomFingerprint": False,
            "os": (hint.os or "windows").capitalize(),
            "userAgent": hint.raw.get("userAgent", ""),
            "language": hint.accept_language or "en-US",
            "languageBaseIp": "language" not in hint.raw,  # 显式给值时关 baseIp
            "uiLanguage": hint.accept_language or "en-US",
            "uiLanguageBaseIp": "uiLanguage" not in hint.raw,
            "timezone": hint.timezone or "",
            "timezoneBaseIp": "timezone" not in hint.raw and hint.timezone is None,
            "geolocationBaseIp": True,
            "resolution": hint.raw.get("resolution", "1920x1080"),
            "ram": hint.raw.get("ram", 8),
            "cpu": hint.raw.get("cpu", 4),
            "canvas": hint.raw.get("canvas", True),
            "speechVoices": hint.raw.get("speechVoices", True),
            "clientRects": hint.raw.get("clientRects", True),
            "hardwareAcceleration": hint.raw.get("hardwareAcceleration", True),
            "kernel": hint.raw.get("kernel", "139"),
            "platformVersion": hint.raw.get("platformVersion", 11),
        }
        # 透传 hint.raw 里的所有非保留字段
        for k, v in hint.raw.items():
            if k.startswith("_"):  # 内部 marker · 不进 Tge payload
                continue
            fp[k] = v
        return fp

    # ── 资源清理 ────────────────────────────────────────────

    def close(self) -> None:
        """关 httpx.Client · 程序退出前调."""
        self._client.close()


# ── Module-level helpers ─────────────────────────────────────────


def _parse_tge_records(body: Any) -> list[dict[str, Any]]:
    """从 Tge response body 取 records list · 兼容三种 envelope.

    真机 contract(Tge desktop 0.50326)观察到的三种格式:

    1. ``{"success": true, "data": [...]}`` · ``data`` 直接是 list(``/api/browser/open/list``)
    2. ``{"success": true, "data": {"list": [...], "total": N, "current": 1, ...}}`` ·
       ``data.list``(``/api/browser/list``)
    3. ``{"success": true, "data": {"records": [...]}}`` · ``data.records``
       (旧 mock 假设 · 历史兼容保留)

    任何一种解析失败时返 ``[]`` · 让调用方走"假设无 record"路径而不是抛异常。
    Drift lock test: ``tests/browser_pool/test_tge_envelope.py``。
    """
    data = body.get("data") if isinstance(body, dict) else None
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        records = data.get("list") or data.get("records") or []
        return records if isinstance(records, list) else []
    return []
