"""增强版代理池 —— 健康检查 + 失败标记 + 自动剔除 + 粘性会话。

替代 proxy.py 简单 round-robin。新特性：
  · 每个代理记录连续失败次数，>N 次自动剔除（冷却后再试）
  · 健康检查：定期 ping ifconfig.me 验证代理活
  · 粘性会话：同一 site 在一次 crawl 中复用同一代理（避免半途切 IP 触发反爬）
  · 多 tier 优先级：residential > datacenter > free-pool

数据格式（优先级：PROXIES_FILE > backend/proxies.local.txt > backend/proxies.txt 模板）：
  [residential]
  http://user:pass@host:port    # 商业住宅代理
  socks5://host:port            # Tailscale/SSH 隧道

  [datacenter]
  http://host:port              # 数据中心 IP

环境变量：
  PROXY_FAIL_THRESHOLD=3        连续失败几次剔除（默认 3）
  PROXY_COOLDOWN_SEC=600        剔除后冷却时间（默认 10min）
  PROXY_HEALTH_INTERVAL=300     健康检查间隔（默认 5min）
  PROXIES_FILE=/path/proxies.txt 私有代理配置文件路径
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


def _default_proxy_file() -> Path:
    env_file = os.environ.get("PROXIES_FILE")
    if env_file:
        return Path(env_file)
    backend_dir = Path(__file__).resolve().parent.parent
    local_file = backend_dir / "proxies.local.txt"
    if local_file.exists():
        return local_file
    return backend_dir / "proxies.txt"


_PROXY_FILE = _default_proxy_file()

FAIL_THRESHOLD = int(os.environ.get("PROXY_FAIL_THRESHOLD", "3"))
COOLDOWN_SEC = int(os.environ.get("PROXY_COOLDOWN_SEC", "600"))


@dataclass
class ProxyEntry:
    url: str                          # http://user:pass@host:port
    tier: str                         # residential / datacenter
    exclude: set[str] = field(default_factory=set)  # 不可用于的平台关键词(如 amazon)
    id: int | None = None
    source: str = "file"
    pool_slugs: set[str] = field(default_factory=set)
    provider: str | None = None
    country: str | None = None
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

    def allows(self, site: str | None) -> bool:
        """该代理是否可用于抓 site。site 命中任一排除关键词(子串)即不可用。"""
        if not site or not self.exclude:
            return True
        s = site.lower()
        return not any(kw in s for kw in self.exclude)


def _parse_proxy_line(line: str, tier: str) -> ProxyEntry:
    """解析一行代理。行尾可带 `# no:amazon,ebay` 标注排除平台。

    无 `#` 标注 → exclude 为空,行为与旧版完全一致(向后兼容)。
    """
    exclude: set[str] = set()
    url = line.strip()
    if "#" in line:
        url_part, _, comment = line.partition("#")
        url = url_part.strip()
        low = comment.lower()
        if "no:" in low:
            after = low.split("no:", 1)[1]          # `no:amazon,ebay` → `amazon,ebay`
            for kw in after.replace(",", " ").split():
                kw = kw.strip()
                if kw:
                    exclude.add(kw)
    return ProxyEntry(url=url, tier=tier, exclude=exclude)


class ProxyPool:
    def __init__(self, *, use_persistent_health: bool = False, prefer_db: bool = True):
        import os
        self._lock = threading.Lock()
        self._proxies: list[ProxyEntry] = []
        # 用 PID 作为起始 index，确保 4 个并行 runner 起步不同代理
        # PID 1234 → index 1234, PID 5678 → index 5678
        self._index: int = os.getpid()
        self._sticky: dict[str, str] = {}   # site -> proxy URL（粘性会话）
        self._loaded = False
        self.use_persistent_health = use_persistent_health
        self.prefer_db = prefer_db

    def _load(self) -> None:
        proxies: list[ProxyEntry] = self._load_from_db() if self.prefer_db else []
        if not proxies and _PROXY_FILE.exists():
            current_tier = "datacenter"
            for line in _PROXY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current_tier = line[1:-1].strip().lower()
                    continue
                proxies.append(_parse_proxy_line(line, current_tier))
        # 环境变量也加进来
        for tier in ("residential", "datacenter"):
            env = os.environ.get(f"{tier.upper()}_PROXY")
            if env:
                if not any(p.url == env for p in proxies):
                    proxies.insert(0, ProxyEntry(url=env, tier=tier))
        self._proxies = proxies
        self._loaded = True

    def _load_from_db(self) -> list[ProxyEntry]:
        try:
            from .db import SessionLocal
            from .proxy_config import bootstrap_proxy_config
            from .models import ProxyEndpoint, ProxyPoolConfig, ProxyPoolMember
        except Exception:
            return []
        db = SessionLocal()
        try:
            bootstrap_proxy_config(db)
            db.commit()
            endpoints = (db.query(ProxyEndpoint)
                         .filter(ProxyEndpoint.active == True)  # noqa: E712
                         .order_by(ProxyEndpoint.id.asc())
                         .all())
            if not endpoints:
                return []
            endpoint_ids = [row.id for row in endpoints]
            memberships = (db.query(ProxyPoolMember, ProxyPoolConfig)
                           .join(ProxyPoolConfig, ProxyPoolConfig.id == ProxyPoolMember.pool_id)
                           .filter(ProxyPoolMember.endpoint_id.in_(endpoint_ids),
                                   ProxyPoolMember.active == True,  # noqa: E712
                                   ProxyPoolConfig.active == True)  # noqa: E712
                           .all())
            pools_by_endpoint: dict[int, set[str]] = {}
            for member, pool in memberships:
                pools_by_endpoint.setdefault(member.endpoint_id, set()).add(pool.slug)
            proxies: list[ProxyEntry] = []
            for row in endpoints:
                if not row.proxy_url:
                    continue
                tier = (row.endpoint_type or "datacenter").strip().lower()
                pool_slugs = pools_by_endpoint.get(row.id, set()) | {tier, "all"}
                proxies.append(ProxyEntry(
                    id=row.id,
                    url=row.proxy_url,
                    tier=tier,
                    exclude=set(row.exclude_sites or []),
                    source=row.source or "db",
                    pool_slugs=pool_slugs,
                    provider=row.provider,
                    country=row.country,
                ))
            return proxies
        except Exception:
            return []
        finally:
            db.close()

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
            site: str | None = None,
            force_rotate: bool = False) -> str | None:
        """取一个可用代理。
        - tier='residential' / 'datacenter' / None=不限
        - site 非 None: 默认**不**粘性，每次轮换；force_rotate=True 时清除当前粘性
        - 旧粘性行为已废弃 → 默认每次都轮换，否则 10 代理只用 1 个
        """
        candidate_tiers = _candidate_tiers_from_rules(site, tier)
        if not candidate_tiers or candidate_tiers[0] in (None, "none", ""):
            return None
        self._ensure_loaded()
        unhealthy = (_persistent_unhealthy_hashes()
                     if self.use_persistent_health else set())
        with self._lock:
            # 显式 force_rotate：解除当前 site 粘性绑定
            if site and force_rotate:
                self._sticky.pop(site, None)
            # 注：默认不再检查 _sticky（之前的粘性把所有请求压到 1 个代理）

            # 找候选：tier 匹配 + 可用 + 未被该 site 排除。
            # 规则/池可配置 fallback_pool_slug；只有主池没有可用代理时才降级。
            candidates = []
            for tier_text in candidate_tiers:
                tier_text = (tier_text or "").strip().lower()
                if tier_text in ("", "none"):
                    return None
                pool_slug = (tier_text.split(":", 1)[1]
                             if tier_text.startswith("pool:") else None)
                candidates = [
                    p for p in self._proxies
                    if self._tier_matches(p, tier_text, pool_slug)
                    and p.is_available and p.allows(site)
                    and _proxy_hash(p.url) not in unhealthy
                ]
                if candidates:
                    break
            if not candidates:
                return None

            # round-robin：从全局 index 选下一个候选（每次都换，不再粘性）
            n = len(candidates)
            self._index = (self._index + 1) % n
            chosen = candidates[self._index]
            chosen.last_used = time.time()
            # 不再写入 _sticky，让每次调用都轮换
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

    @staticmethod
    def _tier_matches(proxy: ProxyEntry, tier: str, pool_slug: str | None) -> bool:
        if pool_slug:
            return pool_slug in proxy.pool_slugs
        return proxy.tier == tier

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
            unhealthy = (_persistent_unhealthy_hashes()
                         if self.use_persistent_health else set())
            def available(proxy: ProxyEntry) -> bool:
                return proxy.is_available and _proxy_hash(proxy.url) not in unhealthy

            return {
                "total": len(self._proxies),
                "by_tier": {
                    tier: {
                        "total": sum(1 for p in self._proxies if p.tier == tier),
                        "available": sum(1 for p in self._proxies
                                         if p.tier == tier and available(p)),
                        "blocked": sum(1 for p in self._proxies
                                       if p.tier == tier and not available(p)),
                    }
                    for tier in {p.tier for p in self._proxies}
                },
                "details": [
                    {
                        "hash": _proxy_hash(p.url),
                        "url": _redact(p.url),
                        "tier": p.tier,
                        "endpoint_id": p.id,
                        "source": p.source,
                        "pools": sorted(p.pool_slugs),
                        "provider": p.provider,
                        "country": p.country,
                        "exclude": sorted(p.exclude),
                        "fail_count": p.fail_count,
                        "success_count": p.success_count,
                        "blocked_for_sec": max(0, int(p.blocked_until - now)),
                        "available": available(p),
                    }
                    for p in self._proxies
                ],
            }


def _redact(url: str) -> str:
    """隐藏 user:pass 中的 password 部分。"""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)


def _proxy_hash(url: str) -> str:
    import hashlib
    return hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()


def _resolve_tier_from_rules(site: str | None, configured_tier: str | None) -> str | None:
    candidates = _candidate_tiers_from_rules(site, configured_tier)
    return candidates[0] if candidates else configured_tier


def _candidate_tiers_from_rules(site: str | None,
                                configured_tier: str | None) -> list[str | None]:
    if not site:
        return _with_pool_fallback(configured_tier)
    try:
        from .db import SessionLocal
        from .models import ProxyPoolConfig, ProxyRule
    except Exception:
        return _with_pool_fallback(configured_tier)
    db = SessionLocal()
    try:
        rules = (db.query(ProxyRule)
                 .filter(ProxyRule.enabled == True)  # noqa: E712
                 .order_by(ProxyRule.priority.asc(), ProxyRule.id.asc())
                 .all())
        site_l = site.lower()
        for rule in rules:
            pattern = (rule.site_pattern or "").strip().lower()
            if not pattern:
                continue
            match_type = (rule.match_type or "contains").lower()
            matched = (
                (match_type == "exact" and site_l == pattern)
                or (match_type == "prefix" and site_l.startswith(pattern))
                or (match_type not in ("exact", "prefix") and pattern in site_l)
            )
            if not matched:
                continue
            mode = (rule.proxy_mode or "pool").strip().lower()
            if mode == "none":
                return ["none"]
            if mode == "pool":
                primary = f"pool:{rule.pool_slug}" if rule.pool_slug else configured_tier
                fallback = (f"pool:{rule.fallback_pool_slug}"
                            if rule.fallback_pool_slug else
                            _pool_fallback_tier(db, primary))
                return _unique_tiers([primary, fallback])
            if mode in ("datacenter", "residential"):
                fallback = (f"pool:{rule.fallback_pool_slug}"
                            if rule.fallback_pool_slug else
                            _pool_fallback_tier(db, mode))
                return _unique_tiers([mode, fallback])
        return _with_pool_fallback(configured_tier, db=db)
    except Exception:
        return _with_pool_fallback(configured_tier)
    finally:
        db.close()


def _unique_tiers(values: list[str | None]) -> list[str | None]:
    out: list[str | None] = []
    seen: set[str] = set()
    for value in values:
        key = (value or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out or [None]


def _with_pool_fallback(configured_tier: str | None,
                        db=None) -> list[str | None]:
    if db is None:
        try:
            from .db import SessionLocal
        except Exception:
            return _unique_tiers([configured_tier])
        own_db = SessionLocal()
        try:
            return _unique_tiers([configured_tier,
                                  _pool_fallback_tier(own_db, configured_tier)])
        finally:
            own_db.close()
    return _unique_tiers([configured_tier, _pool_fallback_tier(db, configured_tier)])


def _pool_fallback_tier(db, tier_text: str | None) -> str | None:
    tier = (tier_text or "").strip().lower()
    if not tier or tier == "none":
        return None
    slug = tier.split(":", 1)[1] if tier.startswith("pool:") else tier
    try:
        from .models import ProxyPoolConfig
        row = (db.query(ProxyPoolConfig)
               .filter(ProxyPoolConfig.slug == slug,
                       ProxyPoolConfig.active == True)  # noqa: E712
               .first())
        if row and row.fallback_pool_slug:
            return f"pool:{row.fallback_pool_slug}"
    except Exception:
        return None
    return None


def _persistent_unhealthy_hashes() -> set[str]:
    try:
        from .db import SessionLocal
        from .proxy_health import unhealthy_proxy_hashes
    except Exception:
        return set()
    db = SessionLocal()
    try:
        return unhealthy_proxy_hashes(db)
    except Exception:
        return set()
    finally:
        db.close()


# 单例
_pool = ProxyPool(use_persistent_health=True)


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
