"""Smoke test: vendored browser_pool from VOC-Shulex/Sourcery loads in smart-crawler.

Runs inside the smart-crawler container. Validates the abstraction is wired
without actually launching Playwright (which would need browser binaries).

Run:
    docker exec smart-crawler python -m scripts.demo_browser_pool
"""
from __future__ import annotations

import sys


def main() -> int:
    # 1) Import surface
    from app.browser_pool import (
        BrowserVendor,
        BrowserProfile,
        BrowserSession,
        FingerprintBrowserVendor,
        FingerprintHint,
        ProxySpec,
        VendorCapability,
        VendorHealth,
        VendorQuota,
    )
    print("[1/4] browser_pool import surface — OK")

    # 2) Vendor implementations resolve
    from app.browser_pool.vendors.local_playwright import LocalPlaywrightVendor
    from app.browser_pool.vendors.tge_browser import TgeBrowserVendor
    from app.browser_pool.vendors.bit_browser import BitBrowserVendor
    from app.browser_pool.vendors.local_chrome import LocalChromeVendor
    print("[2/4] 4 vendors importable — LocalPlaywright / Tge / Bit / LocalChrome")

    # 3) Pydantic models instantiate
    spec = ProxySpec(
        server="http://proxy.example:8080",
        username="u",
        password="p",
    )
    hint = FingerprintHint(
        os="windows",
        browser="chrome",
        accept_language="en-US,en;q=0.9",
        timezone="America/Los_Angeles",
    )
    print(
        f"[3/4] ProxySpec + FingerprintHint instantiate — "
        f"proxy={spec.server} fp={hint.os}/{hint.browser}"
    )

    # 4) Vendor capabilities visible
    caps = list(VendorCapability)
    print(f"[4/4] {len(caps)} capability flags: {[c.value for c in caps[:6]]}...")

    print()
    print("✅ browser_pool vendored from Sourcery — ready for use in smart-crawler")
    print(
        "   Concrete callers next: TikTok TGE adapter (influencers/tiktok.py M6 unblock),"
    )
    print(
        "   vidaxl_us Demandware bypass via LocalPlaywrightVendor, influencer cookie refresh."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
