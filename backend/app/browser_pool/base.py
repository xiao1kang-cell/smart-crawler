"""BrowserVendor Protocol + 数据模型 · M1.2 sprint Lane A 第一步.

对应 plan §3.1 + §13 锁定决策:
- 全部 sync · 不用 asyncio(D1 决策 · 对齐现 worker 范式)
- Pydantic v2 BaseModel · 对齐现 sourcery/resources/ 范式(不用 dataclass)
- `BrowserSession.vendor_session_ref` 字段化(13.5 · P1-4)· 不字符串解析
- `BrowserVendor.capabilities() -> set[str]`(13.6 · P1-5)· vendor 私有能力声明
- `supports_cookies_at_create` flag(13.2 · P1-1)· 加 `initial_cookies` 参数

┌────────────────────────────────────────────────────────────────┐
│              Vendor Pool 抽象关系图                            │
│                                                                 │
│  Worker(BrowserOpen Executor · sync)                          │
│        │ borrow                                                │
│        ▼                                                       │
│  BrowserPool ──┐                                              │
│        │       │ failover chain · 按 vendor_chain 顺序        │
│        ▼       │                                              │
│  BrowserVendor Protocol(sync 方法 6 个)                      │
│   ├─ LocalPlaywrightVendor(包 sync_playwright · backward)    │
│   ├─ TgeBrowserVendor(httpx.Client · REST API · envId)       │
│   └─ BitBrowserVendor(httpx.Client · REST API)               │
│        │                                                       │
│        ▼ provision                                             │
│  BrowserSession(session_id + vendor_session_ref + cdp_*)      │
│        │                                                       │
│        ▼ Worker connect_over_cdp                              │
│  Playwright Page · ctx[session_var] = (session, browser, page)│
└────────────────────────────────────────────────────────────────┘

不在本模块:
- `BrowserPool` · 见 pool.py
- `ClusterManager` · 见 cluster.py
- `RateLimiter` · 见 rate_limiter.py
- vendor 实装 · 见 vendors/*.py
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.browser_pool._stubs import Proxy


# ── Vendor 偏好提示 ───────────────────────────────────────────────


class FingerprintHint(BaseModel):
    """Sourcery 不自管指纹库 · 仅给 vendor 一个偏好提示.

    Vendor(Tge / Bit / ...)内部各自维护数千套指纹库 + 主动应对 bot.sannysoft.com /
    pixelscan 等检测站点的对抗。Sourcery 不重做轮子 · 只声明"我希望大致这样"。

    具体字段映射各 vendor 自己负责(见 vendors/tge_browser.py `_fp_to_tge`)。
    `raw: dict` 透传 vendor-specific 字段 · 不支持的 vendor 自行忽略不报错。

    P2-1(deferred 到 M2):`raw: dict` 长期会演变成 dict-of-strings 类型不安全 ·
    M2 引入 Pydantic discriminated union(TgeFingerprintHint / BitFingerprintHint 子类)。
    MVP 接受 `raw` 透传。
    """

    model_config = ConfigDict(extra="forbid")

    os: Literal["windows", "macos", "linux", "android", "ios"] | None = None
    browser: Literal["chrome", "edge", "firefox", "safari"] | None = None
    accept_language: str | None = None  # 如 "en-US,en;q=0.9"
    timezone: str | None = None  # 如 "Asia/Shanghai"
    screen_min_width: int | None = None

    # Vendor-specific 字段透传 · 不在主结构上声明的字段放这里
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Vendor 启好的浏览器实例句柄 ───────────────────────────────────


class BrowserSession(BaseModel):
    """Vendor 启好 · Worker 准备接管的浏览器实例句柄.

    Worker 拿到后 ``playwright.sync_api.sync_playwright().start().chromium.connect_over_cdp(cdp_ws)``
    即可接管 · 不再自行启动 Chromium 进程。

    `session_id` 是 Sourcery 内部 uuid · 跟 vendor 解耦(P1-4 决策 · plan §13.5)。
    `vendor_session_ref` 持 vendor 内部 ref(Tge=envId · Bit=uuid · Local=空串)·
    `release(session_id)` 时 Pool 从 ClusterManager 查 session 拿 ref · 不字符串解析。
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    session_id: str  # Sourcery 内部 uuid · 不再编码 vendor 信息
    vendor: str  # "local_playwright" / "tge" / "bit"
    vendor_session_ref: str  # vendor 内部 ref · Tge=envId(str)/ Bit=uuid / Local=""(可空)
    profile_id: str  # vendor 内部 profile/env id

    # CDP 接管端点(三选一支持:Playwright 用 ws · DrissionPage 用 port · Selenium 用 driver)
    cdp_ws: str  # 如 "ws://127.0.0.1:50326/devtools/browser/abc..."
    cdp_http: str | None = None  # 如 "http://127.0.0.1:50326/json/version"
    cdp_port: int | None = None  # 如 50326(给 DrissionPage 用)
    chromedriver_path: str | None = None  # ChromeDriver 路径 · 给 Selenium 兼容

    # 关联到 Sourcery 资源
    proxy_id: str | None = None  # Sourcery Proxy 实体 id
    fingerprint_ref: str | None = None  # vendor 内部 fp id · Sourcery 不解析

    started_at: datetime
    expires_at: datetime  # TTL · idle 超时 ClusterManager 主动 release


# ── Vendor 健康 / 配额 ────────────────────────────────────────────


class VendorHealth(BaseModel):
    """Vendor 健康状态 · ClusterManager 周期 probe 后更新."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    healthy: bool
    last_check_at: datetime
    last_error: str | None = None
    consecutive_failures: int = 0


class VendorQuota(BaseModel):
    """Vendor 配额 · borrow 时检查 · UI 显示余额."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    concurrent_in_use: int  # 当前借出未释放数
    concurrent_limit: int  # vendor 配置上限
    rate_limit_remaining: int  # 当前分钟剩余调用数
    rate_limit_reset_at: datetime


# ── Vendor 上的 profile(账号槽位)──────────────────────────────────


class BrowserProfile(BaseModel):
    """Vendor 上的一个 profile · `list_profiles` 返回 · CLI / 控制台用.

    profile 是 vendor 内部"持久化的浏览器配置 + 可能的登录态" · 类似 Chrome user-data-dir。
    Tge 称 env · Bit 称 profile · LocalPlaywright 直接对应 Chromium 进程不持久化。
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str
    profile_id: str
    name: str | None = None  # vendor 上的别名
    cookies_managed: bool  # vendor 是否管 cookies(Tge True · Bit some · Local False)
    fingerprint_id: str | None = None  # vendor 内部 fp id · Sourcery 不解析
    last_used_at: datetime | None = None
    proxy_bound: str | None = None  # vendor 内部 proxy ref


# ── Exception ──────────────────────────────────────────────────────


class PoolExhausted(Exception):
    """Failover chain 全部 vendor 都不可借时抛出.

    `error_code=7000`(POOL_EXHAUSTED · 对齐 master roadmap §6.2 9 段错误码)。
    """

    def __init__(
        self,
        *,
        vendor_chain: list[str],
        errors: list[str],
        error_code: int = 7000,
    ) -> None:
        self.vendor_chain = vendor_chain
        self.errors = errors
        self.error_code = error_code
        super().__init__(
            f"BrowserPool exhausted · chain={vendor_chain} · "
            f"errors={errors}"
        )


# ── Vendor Protocol(本 sprint 核心抽象)─────────────────────────


@runtime_checkable
class BrowserVendor(Protocol):
    """所有外部浏览器平台都实装这个 Protocol.

    全部方法 sync(D1 决策 · plan §13.1)· 不用 async / await。

    实装时:
    - 实例上声明 `name: str` 类属性(如 `name = "tge"`)
    - 声明 `supports_cookies_at_create: bool` 类属性(plan §13.2 · P1-1)
    - 实装 6 个 sync 方法 + `capabilities()`(P1-5)

    `provision(initial_cookies=...)` 仅 `supports_cookies_at_create=True` 时使用 ·
    其余 vendor 静默忽略 · 调用方在 attach 后走 `context.add_cookies()` 兜底。
    """

    name: str
    """Vendor 名称 · 'local_playwright' / 'tge' / 'bit' / ..."""

    supports_cookies_at_create: bool
    """是否支持在 provision 时直接灌 cookies(Tge True · Bit/Local False)."""

    def provision(
        self,
        profile_id: str | None,
        proxy: Proxy | None,
        fingerprint_hint: FingerprintHint | None,
        ttl_seconds: int = 300,
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> BrowserSession:
        """启动一个浏览器实例 · 返 CDP endpoint.

        Args:
            profile_id: 复用已有 profile · None 时新建.
            proxy: Sourcery Proxy 实体 · None 时 vendor 用自己的默认 / 不走代理.
            fingerprint_hint: 偏好提示 · None 时让 vendor 完全自选.
            ttl_seconds: BrowserSession TTL · ClusterManager 超时主动 release.
            initial_cookies: Playwright cookies 格式 list[dict] ·
                仅 `supports_cookies_at_create=True` 时用 · 否则 vendor 忽略.

        Returns:
            BrowserSession · session_id 唯一 · cdp_ws 可用 connect_over_cdp 接管.

        Raises:
            httpx.HTTPError: vendor API 不可达 · 由 Pool 升级为 1xxx VENDOR_UNREACHABLE.
            ValueError: 配置错(如 vendor 不存在的 profile_id).
        """
        ...

    def release(self, session_id: str) -> None:
        """停实例 · 保留 profile · 下次可复用.

        Args:
            session_id: BrowserSession.session_id · Pool 从 ClusterManager
                查 vendor_session_ref 后调 vendor API.
        """
        ...

    def destroy(self, profile_id: str) -> None:
        """彻底删 profile · 不可恢复 · 只在用户主动调 CLI 时执行."""
        ...

    def health(self) -> VendorHealth:
        """健康检查 · ClusterManager 定期调 · borrow 时也走一遍."""
        ...

    def quota(self) -> VendorQuota:
        """配额查询 · borrow 决策用 · UI 显示余额用."""
        ...

    def list_profiles(self) -> list[BrowserProfile]:
        """列出 vendor 上已有 profile · CLI `vendors profiles <name>` 调."""
        ...

    def capabilities(self) -> set[str]:
        """返回该 vendor 支持的可选能力集合.

        基础能力(`provision / release / destroy / health / quota / list_profiles`)
        所有 vendor 默认有 · 不进 capabilities 集合。

        可选能力示例:
        - `"cookies_at_create"`: provision 时可灌 cookies
        - `"update_env"`: 改 profile 配置而不销毁(Tge 私有)
        - `"delete_cache"`: 清缓存但保留 profile(Tge 私有)
        - `"list_running"`: 列出当前 running envs(Tge 私有)
        - `"list_groups"`: 列出 vendor 内部分组(Tge 私有)
        - `"list_proxies"`: 列出 vendor 内置代理库(Tge 私有)
        - `"window_sort"`: 多窗口布局(Tge 私有)

        CLI `sourcery vendors <name> <subcommand>` 用 capabilities 决定
        subcommand 是否合法 · 不在集合内的 subcommand 返 5xxx
        `CAPABILITY_NOT_SUPPORTED`(对齐 master roadmap §6.2)。
        """
        ...


# ════════════════════════════════════════════════════════════════════
# FingerprintBrowserVendor · 子协议 · Amazon Bridge Lane V 加
# ════════════════════════════════════════════════════════════════════
#
# 设计依据:docs/superpowers/specs/2026-05-22-amazon-crawler-bridge-design.md §5
# RFC:docs/RFC/012-fingerprint-vendor-capability-matrix.md(Task P14 落)
#
# 目标:在 BrowserVendor 基础协议之上 · 加一层指纹站子契约 · 8 方法 +
# 8 Capability 枚举值 · 让 BitBrowser / Tge / ix / AdsPower / Hubstudio
# 都按同一套合同接入。


class VendorCapability(StrEnum):
    """指纹浏览器 vendor 可声明的可选能力.

    跟现 BrowserVendor.capabilities() set[str] 签名兼容(StrEnum members ARE strings)。
    Recipe 通过 vendor_capabilities_required 声明依赖 · Pool 自动过滤候选 vendor。

    设计依据:spec §5.2 + RFC 012
    """

    COOKIE_INJECT = "cookie_inject"
    COOKIE_EXTRACT = "cookie_extract"
    PROXY_HOT_SWAP = "proxy_hot_swap"
    FINGERPRINT_OVERRIDE = "fingerprint_override"
    PROFILE_LIST = "profile_list"
    HEALTH_CHECK = "health_check"
    BATCH_RELEASE = "batch_release"
    TAB_TAKEOVER = "tab_takeover"


class ProfileState(StrEnum):
    """`FingerprintBrowserVendor.health_check` 返回."""

    ALIVE = "alive"
    RUNNING = "running"
    ZOMBIE = "zombie"
    BANNED = "banned"
    LOGGED_OUT = "logged_out"


class Cookie(BaseModel):
    """跨 vendor 通用 cookie 类型 · 跟 Playwright add_cookies 字段对齐."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    http_only: bool = False
    secure: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = None


class ProxySpec(BaseModel):
    """`FingerprintBrowserVendor.update_proxy` 输入 · 跟 sourcery.resources.Proxy 解耦."""

    model_config = ConfigDict(extra="forbid")

    server: str
    username: str | None = None
    password: str | None = None


class ProfileRef(BaseModel):
    """`FingerprintBrowserVendor.acquire` 输入 · 比 BrowserVendor.provision 更轻量."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    ttl_seconds: int = 300


class VendorSession(BrowserSession):
    """`FingerprintBrowserVendor.acquire` 输出 · 复用 BrowserSession · 子类纯 marker."""

    # BrowserSession 已涵盖所有字段 · 子类空体 · 仅为类型注释清晰


class ProfileQuery(BaseModel):
    """`FingerprintBrowserVendor.list_profiles` 输入."""

    model_config = ConfigDict(extra="forbid")

    state: ProfileState | None = None
    limit: int = 100


class ProfileSummary(BaseModel):
    """`FingerprintBrowserVendor.list_profiles` 输出 item."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    state: ProfileState
    last_used_at: datetime | None = None
    proxy_bound: str | None = None


@runtime_checkable
class FingerprintBrowserVendor(Protocol):
    """指纹浏览器**同级协议**(sibling Protocol · 不继承 BrowserVendor).

    设计演化注:spec §5.1 原写"BrowserVendor 子协议(严格超集)"· 实施时发现
    几个方法跟 BrowserVendor 同名但签名不兼容(release / list_profiles)·
    继承会触发 LSP override 冲突 · 需要 `# type: ignore[override]` 工作 around。

    干净的解:改成 sibling Protocol · 真 vendor(BitBrowserVendor / TgeBrowserVendor)
    同时实装 BrowserVendor + FingerprintBrowserVendor 两个 protocol(Python 允许 ·
    multiple Protocol fulfillment)· 完全没 override 冲突。

    Pool 借时按 capabilities_required 过滤:
      - 单纯 BrowserVendor 能力 → 任何 vendor
      - FingerprintBrowserVendor 能力 → 只看实装本协议的 vendor

    任何指纹站需要的能力都长这样 · BitBrowser / Tge / ix / AdsPower / Hubstudio
    实装 5/8/10 个 method 就能接进来 · 不接的能力通过 capabilities() 自报。

    LocalPlaywrightVendor 故意不实装本协议(没 profile 体系)。

    设计依据:spec §5.1(注:本 commit 修了继承关系)+ RFC 012
    """

    name: str
    """Vendor 名称 · 'bit' / 'tge' / 'adspower' / ..."""

    def acquire(self, profile_ref: ProfileRef) -> VendorSession:
        """启动 profile · 返 CDP url + profile metadata · 替 start_fingerprint."""
        ...

    def release(self, session: VendorSession, persist_state: bool = True) -> None:
        """释放 session · persist_state=True 触发 cookies 回写."""
        ...

    def inject_cookies(
        self,
        profile_id: str,
        cookies: list[Cookie],
        domain_filter: str | None = None,
    ) -> None:
        """启动前注入 cookies(VendorCapability.COOKIE_INJECT)."""
        ...

    def extract_cookies(
        self,
        profile_id: str,
        domain_filter: str | None = None,
    ) -> list[Cookie]:
        """运行后回写 cookies(VendorCapability.COOKIE_EXTRACT)."""
        ...

    def update_proxy(
        self,
        profile_id: str,
        proxy: ProxySpec,
        hot: bool = False,
    ) -> None:
        """更新 profile 绑定的 proxy · hot=True 不重启切换(VendorCapability.PROXY_HOT_SWAP)."""
        ...

    def set_fingerprint(self, profile_id: str, fingerprint: Any) -> None:
        """运行时改指纹字段(VendorCapability.FINGERPRINT_OVERRIDE)·
        fingerprint 类型 vendor-specific · 不约束。
        """
        ...

    def list_profiles(self, query: ProfileQuery) -> list[ProfileSummary]:
        """列出符合条件的 profile(VendorCapability.PROFILE_LIST)."""
        ...

    def health_check(self, profile_id: str) -> ProfileState:
        """健康检测(VendorCapability.HEALTH_CHECK)."""
        ...

    def capabilities(self) -> set[str]:
        """返回该 vendor 实装的 VendorCapability 集合.

        Pool 按 Recipe 的 `vendor_capabilities_required` 过滤候选 vendor 时调。
        StrEnum members ARE strings · 跟 BrowserVendor.capabilities() set[str] 兼容。
        """
        ...
