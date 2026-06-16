"""代理配置服务。

DB 是长期配置源；proxies.local.txt / proxies.txt 只用于首次导入和空库兜底。
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .models import ProxyEndpoint, ProxyPoolConfig, ProxyPoolMember, ProxyRule
from .proxy_health import proxy_hash, redact_proxy

DEFAULT_POOL_SPECS = {
    "datacenter": {"name": "普通 IP 池", "pool_type": "datacenter"},
    "residential": {"name": "住宅 IP 池", "pool_type": "residential"},
    "all": {"name": "全部可用代理", "pool_type": "mixed"},
}


def default_proxy_file() -> Path:
    env_file = os.environ.get("PROXIES_FILE")
    if env_file:
        return Path(env_file)
    backend_dir = Path(__file__).resolve().parent.parent
    local_file = backend_dir / "proxies.local.txt"
    if local_file.exists():
        return local_file
    return backend_dir / "proxies.txt"


def normalize_endpoint_type(value: str | None) -> str:
    value = (value or "datacenter").strip().lower()
    aliases = {
        "dc": "datacenter",
        "normal": "datacenter",
        "ordinary": "datacenter",
        "plain": "datacenter",
        "res": "residential",
        "resi": "residential",
        "home": "residential",
    }
    return aliases.get(value, value)


def parse_proxy_line(line: str, endpoint_type: str) -> dict | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    exclude: set[str] = set()
    if "#" in text:
        url_part, _, comment = text.partition("#")
        text = url_part.strip()
        low = comment.lower()
        if "no:" in low:
            after = low.split("no:", 1)[1]
            for kw in after.replace(",", " ").split():
                kw = kw.strip()
                if kw:
                    exclude.add(kw)
    if not text:
        return None
    return {"proxy_url": text, "endpoint_type": endpoint_type,
            "exclude_sites": sorted(exclude)}


def parse_proxy_file(path: Path | None = None) -> list[dict]:
    path = path or default_proxy_file()
    if not path.exists():
        return []
    items: list[dict] = []
    current_type = "datacenter"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_type = normalize_endpoint_type(line[1:-1])
            continue
        item = parse_proxy_line(line, current_type)
        if item:
            items.append(item)
    return items


def _split_proxy_url(proxy_url: str) -> tuple[str | None, str | None, int | None]:
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme or None
    host = parsed.hostname
    port = parsed.port
    return scheme, host, port


def ensure_default_pools(db: Session) -> dict[str, ProxyPoolConfig]:
    rows = {row.slug: row for row in db.query(ProxyPoolConfig).all()}
    now = datetime.utcnow()
    for slug, spec in DEFAULT_POOL_SPECS.items():
        row = rows.get(slug)
        if row is None:
            row = ProxyPoolConfig(
                slug=slug,
                name=spec["name"],
                pool_type=spec["pool_type"],
                active=True,
                description="系统默认代理池",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.flush()
            rows[slug] = row
        else:
            if not row.name:
                row.name = spec["name"]
            if not row.pool_type:
                row.pool_type = spec["pool_type"]
            if row.active is None:
                row.active = True
    return rows


def upsert_proxy_endpoint(
    db: Session,
    *,
    proxy_url: str,
    endpoint_type: str = "datacenter",
    name: str | None = None,
    provider: str | None = None,
    country: str | None = None,
    active: bool = True,
    exclude_sites: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    max_concurrency: int | None = None,
    source: str = "admin",
    notes: str | None = None,
) -> ProxyEndpoint:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        raise ValueError("proxy_url required")
    endpoint_type = normalize_endpoint_type(endpoint_type)
    scheme, host, port = _split_proxy_url(proxy_url)
    h = proxy_hash(proxy_url)
    row = db.query(ProxyEndpoint).filter(ProxyEndpoint.proxy_hash == h).first()
    now = datetime.utcnow()
    if row is None:
        row = ProxyEndpoint(proxy_hash=h, created_at=now)
        db.add(row)
    row.proxy_url = proxy_url
    row.proxy_redacted = redact_proxy(proxy_url)
    row.endpoint_type = endpoint_type
    row.scheme = scheme
    row.host = host
    row.port = port
    row.name = name or row.name or host or endpoint_type
    row.provider = provider if provider is not None else row.provider
    row.country = country if country is not None else row.country
    row.active = bool(active)
    row.exclude_sites = sorted({str(x).strip().lower() for x in (exclude_sites or [])
                                if str(x).strip()})
    row.tags = sorted({str(x).strip() for x in (tags or [])
                       if str(x).strip()}) or row.tags
    if max_concurrency is not None:
        row.max_concurrency = max(1, int(max_concurrency))
    elif not row.max_concurrency:
        row.max_concurrency = 1
    row.source = source or row.source or "admin"
    row.notes = notes if notes is not None else row.notes
    row.updated_at = now
    db.flush()
    attach_endpoint_to_default_pools(db, row)
    return row


def attach_endpoint_to_default_pools(db: Session, endpoint: ProxyEndpoint) -> None:
    pools = ensure_default_pools(db)
    for slug in (endpoint.endpoint_type, "all"):
        pool = pools.get(slug)
        if not pool:
            continue
        existing = (db.query(ProxyPoolMember)
                    .filter(ProxyPoolMember.pool_id == pool.id,
                            ProxyPoolMember.endpoint_id == endpoint.id)
                    .first())
        if existing is None:
            db.add(ProxyPoolMember(pool_id=pool.id, endpoint_id=endpoint.id,
                                   active=True, weight=1, priority=100,
                                   created_at=datetime.utcnow(),
                                   updated_at=datetime.utcnow()))


def import_proxy_file(db: Session, path: Path | None = None) -> dict:
    ensure_default_pools(db)
    added = 0
    updated = 0
    for item in parse_proxy_file(path):
        before = (db.query(ProxyEndpoint)
                  .filter(ProxyEndpoint.proxy_hash == proxy_hash(item["proxy_url"]))
                  .first())
        upsert_proxy_endpoint(db, source="file", **item)
        if before is None:
            added += 1
        else:
            updated += 1
    db.flush()
    return {"added": added, "updated": updated,
            "path": str(path or default_proxy_file())}


def bootstrap_proxy_config(db: Session) -> dict:
    ensure_default_pools(db)
    count = db.query(ProxyEndpoint).count()
    if count:
        return {"imported": False, "endpoint_count": count}
    result = import_proxy_file(db)
    return {"imported": True, "endpoint_count": result["added"],
            **result}


def endpoint_dict(row: ProxyEndpoint, *, pools: list[str] | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "hash": row.proxy_hash,
        "proxy": row.proxy_redacted,
        "endpoint_type": row.endpoint_type,
        "tier": row.endpoint_type,
        "scheme": row.scheme,
        "host": row.host,
        "port": row.port,
        "provider": row.provider,
        "country": row.country,
        "active": bool(row.active),
        "exclude": row.exclude_sites or [],
        "tags": row.tags or [],
        "max_concurrency": row.max_concurrency or 1,
        "source": row.source,
        "notes": row.notes,
        "pools": pools or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def pool_dict(row: ProxyPoolConfig, *, members: int = 0, available: int = 0) -> dict:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "pool_type": row.pool_type,
        "active": bool(row.active),
        "fallback_pool_slug": row.fallback_pool_slug,
        "description": row.description,
        "member_count": members,
        "available_count": available,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def rule_dict(row: ProxyRule) -> dict:
    return {
        "id": row.id,
        "site_pattern": row.site_pattern,
        "match_type": row.match_type,
        "proxy_mode": row.proxy_mode,
        "pool_slug": row.pool_slug,
        "fallback_pool_slug": row.fallback_pool_slug,
        "priority": row.priority or 100,
        "enabled": bool(row.enabled),
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

