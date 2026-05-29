"""browser_pool.vendors · 各 vendor 实装(LocalPlaywright / Tge / Bit / ...).

注:这些 vendor 类只通过 `BrowserVendor` Protocol 暴露给 Pool · 实际类型不导出
为公共 API · 避免上层代码硬依赖具体实装。compose root(M1.5 编排层 / CLI 启动期)
按需 import 注册。
"""

from app.browser_pool.vendors.bit_browser import BitBrowserVendor
from app.browser_pool.vendors.local_chrome import LocalChromeVendor
from app.browser_pool.vendors.local_playwright import (
    LocalPlaywrightVendor,
    PlaywrightNotInstalled,
)
from app.browser_pool.vendors.tge_browser import TgeBrowserVendor

__all__ = [
    "BitBrowserVendor",
    "LocalChromeVendor",
    "LocalPlaywrightVendor",
    "PlaywrightNotInstalled",
    "TgeBrowserVendor",
]
