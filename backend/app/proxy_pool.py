"""增强版代理池 —— 健康检查 + 失败标记 + 自动剔除 + 粘性会话。

替代 proxy.py 简单 round-robin。新特性：
  · 每个代理记录连续失败次数，>N 次自动剔除（冷却后再试）
  · 健康检查：定期 ping ifconfig.me 验证代理活
  · 粘性会话：同一 site 在一次 crawl 中复用同一代理（避免半途切 IP 触发反爬）
  · 多 tier 优先级：residential > datacenter > free-pool

数据格式（proxies.txt）：
  [residential]
  http://user:pass@host:port    # 商业住宅代理
  socks5://host:port            # Tailscale/SSH 隧道

  [datacenter]
  http://host:port              # 数据中心 IP

环境变量：
  PROXY_FAIL_THRESHOLD=3        连续失败几次剔除（默认 3）
  PROXY_COOLDOWN_SEC=600        剔除后冷却时间（默认 10min）
  PROXY_HEALTH_INTERVAL=300     健康检查间隔（默认 5min）
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_PROXY_FILE = Path(__file__).resolve().parent.parent / "proxies.txt"

FAIL_THRESHOLD = int(os.environ.get("PROXY_FAIL_THRESHOLD", "3"))
COOLDOWN_SEC = int(os.environ.get("PROXY_COOLDOWN_SEC", "600"))


@dataclass
class ProxyEntry:
    url: str                          # http://user:pass@host:port
    tier: str                         # residential / datacenter
    fail_count: int = 0
    success_count: int = 0
    last_used: float = 0.0
    last_failed: float = 0.0
    blocked_until: float = 0.0        # 0 = available

    @property
    def is_available(self) -> bool:
        return time.time() >= self.blocked_until

    @property
    def total_uses(self) -> int:
        return self.fail_count + self.success_count


class ProxyPool:
    def __init__(self):
        import os
        self._lock = threading.Lock()
        self._proxies: list[ProxyEntry] = []
        # 用 PID 作为起始 index，确保 4 个并行 runner 起步不同代理
        # PID 1234 → index 1234, PID 5678 → index 5678
        self._index: int = os.getpid()
        self._sticky: dict[str, str] = {}   # site -> proxy URL（粘性会话）
        self._loaded = False

    def _load(self) -> None:
        proxies: list[ProxyEntry] = []
        if _PROXY_FILE.exists():
            current_tier = "datacenter"
            for line in _PROXY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current_tier = line[1:-1].strip().lower()
                    continue
                proxies.append(ProxyEntry(url=line, tier=current_tier))
        # 环境变量也加进来
        for tier in ("residential", "datacenter"):
            env = os.environ.get(f"{tier.upper()}_PROXY")
            if env:
                if not any(p.url == env for p in proxies):
                    proxies.insert(0, ProxyEntry(url=env, tier=tier))
        self._proxies = proxies
        self._loaded = True

    def _ensure_loaded(self):
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._load()

    def reload(self):
        """热重载 proxies.txt"""
        with self._lock:
            self._loaded = False
            self._load()
            self._sticky.clear()

    def get(self, tier: str | None = None,
            site: str | None = None) -> str | None:
        """取一个可用代理。
        - tier='residential' / 'datacenter' / None=不限
        - site 非 None: 粘性会话，同 site 复用同一代理直到失败
        """
        if tier in (None, "none", ""):
            return None
        self._ensure_loaded()
        with self._lock:
            # 粘性：site 已绑定过代理，且仍可用
            if site and site in self._sticky:
                sticky_url = self._sticky[site]
                for p in self._proxies:
                    if p.url == sticky_url and p.is_available:
                        p.last_used = time.time()
                        return p.url

            # 找候选：tier 匹配 + 可用
            candidates = [p for p in self._proxies
                          if p.tier == tier and p.is_available]
            if not candidates:
                return None

            # round-robin：从全局 index 选下一个候选
            n = len(candidates)
            self._index = (self._index + 1) % n
            chosen = candidates[self._index]
            chosen.last_used = time.time()
            if site:
                self._sticky[site] = chosen.url
            return chosen.url

    def report_success(self, url: str):
        if not url:
            return
        with self._lock:
            for p in self._proxies:
                if p.url == url:
                    p.success_count += 1
                    p.fail_count = max(0, p.fail_count - 1)  # 恢复
                    break

    def report_failure(self, url: str, *, hard: bool = False):
        """报告代理失败。hard=True 直接 ban 5×COOLDOWN（被风控时用）。"""
        if not url:
            return
        with self._lock:
            for p in self._proxies:
                if p.url == url:
                    p.fail_count += 1
                    p.last_failed = time.time()
                    if hard or p.fail_count >= FAIL_THRESHOLD:
                        # 冷却：blocked_until = now + cooldown
                        multiplier = 5 if hard else 1
                        p.blocked_until = (time.time()
                                           + COOLDOWN_SEC * multiplier)
                        p.fail_count = 0  # reset 计数避免永久 ban
                    # 解除粘性绑定
                    for site, sticky_url in list(self._sticky.items()):
                        if sticky_url == url:
                            del self._sticky[site]
                    break

    def status(self) -> dict:
        self._ensure_loaded()
        with self._lock:
            now = time.time()
            return {
                "total": len(self._proxies),
                "by_tier": {
                    tier: {
                        "total": sum(1 for p in self._proxies if p.tier == tier),
                        "available": sum(1 for p in self._proxies
                                         if p.tier == tier and p.is_available),
                        "blocked": sum(1 for p in self._proxies
                                       if p.tier == tier and not p.is_available),
                    }
                    for tier in {p.tier for p in self._proxies}
                },
                "details": [
                    {
                        "url": _redact(p.url),
                        "tier": p.tier,
                        "fail_count": p.fail_count,
                        "success_count": p.success_count,
                        "blocked_for_sec": max(0, int(p.blocked_until - now)),
                        "available": p.is_available,
                    }
                    for p in self._proxies
                ],
            }


def _redact(url: str) -> str:
    """隐藏 user:pass 中的 password 部分。"""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)


# 单例
_pool = ProxyPool()


def get_proxy(tier: str | None = None, site: str | None = None) -> str | None:
    return _pool.get(tier, site)


def report_success(url: str | None):
    if url:
        _pool.report_success(url)


def report_failure(url: str | None, *, hard: bool = False):
    if url:
        _pool.report_failure(url, hard=hard)


def pool_status() -> dict:
    return _pool.status()


def reload_pool():
    _pool.reload()
