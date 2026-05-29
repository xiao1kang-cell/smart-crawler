"""browser_pool · M1.2 · BrowserVendor 抽象 + 集群生命周期.

PRD §7.4.2 F-FP-01~03 + §F-AD · 反爬资源池外部浏览器平台子集。

设计要点(对照 docs/plan/roadmap/02-m1.2-browser-vendor.md §13 锁定决策):
- **sync abstraction**(D1 决策)· 不用 asyncio · 对齐现 sourcery/worker/ 范式
- `BrowserVendor` Protocol 统一外部浏览器平台(LocalPlaywright / Tge / Bit / ...)
- `FingerprintHint` 给 vendor 偏好提示 · Sourcery 不自管指纹库 · vendor 内置数千套
- `BrowserSession.vendor_session_ref` 字段化(P1-4)· 不字符串解析 vendor ref
- `BrowserVendor.capabilities()`(P1-5)· vendor 私有 extension 走 capability 声明
- `supports_cookies_at_create` flag(P1-1)· Tge True · Bit/Local False
"""

from app.browser_pool.base import (
    BrowserProfile,
    BrowserSession,
    BrowserVendor,
    Cookie,
    FingerprintBrowserVendor,
    FingerprintHint,
    PoolExhausted,
    ProfileQuery,
    ProfileRef,
    ProfileState,
    ProfileSummary,
    ProxySpec,
    VendorCapability,
    VendorHealth,
    VendorQuota,
    VendorSession,
)

__all__ = [
    "BrowserProfile",
    "BrowserSession",
    "BrowserVendor",
    "Cookie",
    "FingerprintBrowserVendor",
    "FingerprintHint",
    "PoolExhausted",
    "ProfileQuery",
    "ProfileRef",
    "ProfileState",
    "ProfileSummary",
    "ProxySpec",
    "VendorCapability",
    "VendorHealth",
    "VendorQuota",
    "VendorSession",
]
