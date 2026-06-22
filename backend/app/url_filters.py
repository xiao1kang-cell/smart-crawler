"""Shared URL filters for crawl discovery and frontier recording."""
from __future__ import annotations

from urllib.parse import urlparse

SYSTEM_PATH_PREFIXES = (
    "/cdn-cgi",
    "/.well-known/acme-challenge",
    "/.well-known/baleen",
)

SYSTEM_PATH_PARTS = (
    "/challenge",
    "/captcha",
    "/bot-check",
)

STATIC_EXTENSIONS = (
    ".css", ".js", ".mjs", ".map", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".xml", ".xml.gz",
)

NON_PRODUCT_SLUGS = {
    "account", "accounts", "admin", "api", "assets", "basket", "blog",
    "cart", "checkout", "contact", "help", "login", "logout", "news",
    "newsletter", "privacy", "register", "returns", "rss", "search",
    "sitemap", "terms", "wishlist",
}


def is_system_or_challenge_url(url: str | None) -> bool:
    if not url:
        return True
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return True
    if not path or path == "/":
        return False
    if any(path == prefix or path.startswith(prefix + "/")
           for prefix in SYSTEM_PATH_PREFIXES):
        return True
    return any(part in path for part in SYSTEM_PATH_PARTS)


def is_static_asset_url(url: str | None) -> bool:
    if not url:
        return True
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return True
    return path.endswith(STATIC_EXTENSIONS)


def is_obvious_non_product_url(url: str | None) -> bool:
    if is_system_or_challenge_url(url) or is_static_asset_url(url):
        return True
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return True
    slug = path.strip("/").split("/", 1)[0].split("?", 1)[0]
    return bool(slug and slug in NON_PRODUCT_SLUGS)

