"""Load KEY=VALUE environment files for launchd-managed workers.

launchd does not source shell profiles, so worker processes use SC_ENV_FILE to
load their runtime configuration before modules read environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_env_file(default: str | os.PathLike[str] | None = None) -> Path | None:
    explicit = os.environ.get("SC_ENV_FILE") or os.environ.get("ENV_FILE")
    if explicit:
        return Path(explicit)

    if default:
        root = Path(default).parent
        fallback = Path(default)
    else:
        root = Path(__file__).resolve().parents[2]
        fallback = root / ".env"

    env = (os.environ.get("APP_ENV") or os.environ.get("SC_ENV") or os.environ.get("ENV") or "").strip().lower()
    aliases = {
        "prod": "production",
        "online": "production",
        "local": "test",
        "dev": "test",
        "development": "test",
    }
    suffix = aliases.get(env, env) if env else ""
    if suffix:
        return root / f".env.{suffix}"
    default_env = (os.environ.get("SC_DEFAULT_ENV") or "test").strip().lower()
    default_suffix = aliases.get(default_env, default_env)
    default_path = root / f".env.{default_suffix}" if default_suffix else None
    if default_path and default_path.exists():
        return default_path
    return fallback


def load_env_file(default: str | os.PathLike[str] | None = None) -> Path | None:
    path = resolve_env_file(default)
    if not path or not path.exists():
        return
    _load_one_env_file(path)
    for extra_path in _extra_env_files(path):
        if extra_path.exists():
            _load_one_env_file(extra_path)
    return path


def _load_one_env_file(path: Path) -> None:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _extra_env_files(base_path: Path) -> list[Path]:
    raw = os.environ.get("SC_EXTRA_ENV_FILES") or os.environ.get("SC_EXTRA_ENV_FILE") or ""
    paths: list[Path] = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = base_path.parent / candidate
        paths.append(candidate)
    return paths
