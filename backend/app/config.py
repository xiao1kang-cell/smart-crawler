"""全局配置：加载 sites.yaml + 代理凭据 + 运行参数。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
FRONTEND_DIR = PROJECT_DIR / "frontend"
SITES_YAML = BACKEND_DIR / "sites.yaml"

DATA_DIR.mkdir(exist_ok=True)

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def load_config() -> dict:
    with open(SITES_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_settings() -> dict:
    cfg = load_config()
    return cfg.get("settings", {})


def get_sites() -> list[dict]:
    return load_config().get("sites", [])


def user_agents() -> list[str]:
    return list(_USER_AGENTS)


# ---- 代理凭据：从环境变量读取，未配置时返回 None（不阻塞 Shopify/Homary）----
def proxy_for_tier(tier: str) -> str | None:
    """按 proxy_tier 返回代理 URL。住宅代理凭据由 Hunter 通过环境变量提供。

    环境变量约定：
      RESIDENTIAL_PROXY = http://user:pass@host:port
      DATACENTER_PROXY  = http://user:pass@host:port
    """
    if tier == "residential":
        return os.environ.get("RESIDENTIAL_PROXY")
    if tier == "datacenter":
        return os.environ.get("DATACENTER_PROXY")
    return None
