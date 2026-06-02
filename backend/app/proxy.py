"""代理池 —— 规格 §8.2（C-010 代理 IP 轮换）。

代理来源（优先级从高到低）：
  1. 环境变量 RESIDENTIAL_PROXY / DATACENTER_PROXY（单个代理 URL）
  2. PROXIES_FILE 指向的私有文件，未设置时读取 backend/proxies.txt 模板
  3. 无代理 → 直连

对接 static-ip-manager：该项目管理 AT&T 静态块 108.95.61.128/26
（60 个美国静态 IP）。在某台持有这些 IP 的美国机器上起一个轻量代理
（3proxy / squid），把出口 URL 逐行写入 proxies.txt 的 [residential] 段，
本模块即可轮换使用 —— 详见 docs/风控策略评估.md。
"""
from __future__ import annotations

import itertools
import os
import threading
from pathlib import Path

_PROXY_FILE = Path(os.environ.get(
    "PROXIES_FILE",
    str(Path(__file__).resolve().parent.parent / "proxies.txt"),
))
_lock = threading.Lock()
_pools: dict[str, "itertools.cycle"] = {}
_loaded = False


def _load_file() -> dict[str, list[str]]:
    """解析 proxies.txt，按 [residential] / [datacenter] 分段。"""
    pools: dict[str, list[str]] = {"residential": [], "datacenter": []}
    if not _PROXY_FILE.exists():
        return pools
    current = "datacenter"
    for line in _PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            pools.setdefault(current, [])
            continue
        pools.setdefault(current, []).append(line)
    return pools


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        file_pools = _load_file()
        for tier in ("residential", "datacenter"):
            urls = list(file_pools.get(tier, []))
            env = os.environ.get(f"{tier.upper()}_PROXY")
            if env and env not in urls:
                urls.insert(0, env)
            if urls:
                _pools[tier] = itertools.cycle(urls)
        _loaded = True


def get_proxy(tier: str, site: str | None = None) -> str | None:
    """按 tier 取一个代理 URL。委托给新版 proxy_pool（含失败追踪 + 粘性会话）。"""
    if tier in (None, "none", ""):
        return None
    # 优先用新版 proxy_pool（带健康检查）；旧版作为 fallback
    try:
        from . import proxy_pool
        url = proxy_pool.get_proxy(tier, site=site)
        if url is not None:
            return url
    except Exception:
        pass
    # Fallback: 旧版简单轮换
    _ensure_loaded()
    pool = _pools.get(tier)
    if pool is None:
        return None
    with _lock:
        return next(pool)


def pool_status() -> dict:
    """代理池状态（用于看板 / 风控监控）。"""
    file_pools = _load_file()
    out = {}
    for tier in ("residential", "datacenter"):
        n = len(file_pools.get(tier, []))
        if os.environ.get(f"{tier.upper()}_PROXY"):
            n += 1
        out[tier] = n
    return out
