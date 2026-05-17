"""采集器工厂 —— 按 site.platform 选择采集器。"""
from __future__ import annotations

from ..models import Site
from .base import BaseCrawler


def get_crawler(site: Site) -> BaseCrawler:
    platform = site.platform
    if platform == "shopify":
        from .shopify import ShopifyCrawler
        return ShopifyCrawler(site)
    if platform == "nuxt":
        from .homary import HomaryCrawler
        return HomaryCrawler(site)
    if platform == "vue_spa":
        from .costway import CostwayCrawler
        return CostwayCrawler(site)
    if platform == "generic":
        from .generic import GenericCrawler
        return GenericCrawler(site)
    if platform == "flexispot":
        from .flexispot import FlexispotCrawler
        return FlexispotCrawler(site)
    if platform == "vidaxl":
        from .vidaxl import VidaxlCrawler
        return VidaxlCrawler(site)
    if platform == "vonhaus":
        from .vonhaus import VonHausCrawler
        return VonHausCrawler(site)
    raise ValueError(f"未知平台: {platform}")
