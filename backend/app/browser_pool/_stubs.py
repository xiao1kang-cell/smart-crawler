"""Type stubs for Sourcery cross-package references.

smart-crawler vendors browser_pool from Sourcery (M1.2 abstraction). Sourcery
references its own `resources.Proxy` for typing convenience in `provision()`.
smart-crawler has its own proxy system (`app.proxy_pool.ProxyPool`) and never
passes a Sourcery `Proxy` instance — callers use `ProxySpec` directly. This
stub keeps the type hint working without dragging `sourcery.resources` in.
"""
from __future__ import annotations

from typing import Any

# Forward-compatible Any alias. If we later vendor sourcery.resources.proxy
# verbatim, swap this for the real Pydantic model.
Proxy = Any
