"""平台识别与 URL 分类 —— 按域名选采集器,按路径判单品/列表页。"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# 域名关键字 → 平台。Shopee/Lazada/美客多 各有多国域名。
_DOMAIN_MARKERS = (
    ("mercadolibre", "mercadolibre"),
    ("mercadolivre", "mercadolibre"),   # 巴西站
    ("lazada", "lazada"),
    ("shopee", "shopee"),
)

# 单品 URL 特征(命中即 product,否则按 listing)
_PRODUCT_PATTERNS = {
    "mercadolibre": re.compile(r"/ML[A-Z]-?\d+", re.I),   # MLM-123 / MLB123
    "lazada": re.compile(r"/products/.+\.html", re.I),
    "shopee": re.compile(r"-i\.\d+\.\d+|/product/\d+/\d+", re.I),
}


def detect_platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for marker, platform in _DOMAIN_MARKERS:
        if marker in host:
            return platform
    return None


def classify_url(url: str) -> str:
    """返回 'product' 或 'listing'。无法识别平台或未命中单品特征时默认 'listing'。"""
    platform = detect_platform(url)
    pat = _PRODUCT_PATTERNS.get(platform)
    if pat and pat.search(url):
        return "product"
    return "listing"


def get_crawler(platform: str):
    if platform == "mercadolibre":
        from .mercadolibre import MercadoLibreOnDemand
        return MercadoLibreOnDemand()
    if platform == "lazada":
        from .lazada import LazadaOnDemand
        return LazadaOnDemand()
    if platform == "shopee":
        from .shopee import ShopeeOnDemand
        return ShopeeOnDemand()
    raise ValueError(f"未知按需抓取平台: {platform}")
