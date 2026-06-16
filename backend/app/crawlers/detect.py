"""平台探测 —— 从任意 URL 推断该用哪个 crawler。

只覆盖 Shopify(/products.json) 与通用 sitemap 两类。其余平台(含 Magento)
需人工在 sites.yaml 配 platform。探测失败只返回 None,绝不抛异常。
"""
from __future__ import annotations

from urllib.parse import urlparse

_TIMEOUT = 8


def _get(url: str, **kw):
    """单独函数,便于测试 patch。失败抛异常由调用方兜。"""
    from curl_cffi import requests as cffi
    return cffi.get(url, timeout=_TIMEOUT, impersonate="chrome", **kw)


def _safe_get(url: str):
    try:
        return _get(url)
    except Exception:
        return None


def normalize_base(url: str) -> str:
    """取 scheme+host,去 path/query(验收:仅维护网址固定部分)。"""
    p = urlparse(url if "://" in url else f"https://{url}")
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc}"


def detect_platform(url: str) -> tuple[str | None, str]:
    """返回 (platform, normalized_base)。platform 为 None 表示无法识别。"""
    base = normalize_base(url)
    host = urlparse(base).netloc.lower()

    if host.endswith("sephora.com") or host.endswith("sephora.fr"):
        return "sephora", base

    # 1) Shopify: /products.json?limit=1 返回含 products 键的 JSON
    r = _safe_get(f"{base}/products.json?limit=1")
    if r is not None and r.status_code == 200:
        try:
            if isinstance(r.json().get("products"), list):
                return "shopify", base
        except Exception:
            pass

    # 2) 通用 sitemap
    r = _safe_get(f"{base}/sitemap.xml")
    if r is not None and r.status_code == 200 and "<url" in (r.text or ""):
        return "generic", base
    r = _safe_get(f"{base}/robots.txt")
    if r is not None and r.status_code == 200 and "sitemap" in (r.text or "").lower():
        return "generic", base

    return None, base
