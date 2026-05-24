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
    if platform == "magento":
        from .magento import MagentoCrawler
        return MagentoCrawler(site)
    if platform == "shoper":
        from .shoper import ShoperCrawler
        return ShoperCrawler(site)
    if platform == "wayfair":
        from .wayfair import WayfairCrawler
        return WayfairCrawler(site)
    if platform == "overstock":
        from .overstock import OverstockCrawler
        return OverstockCrawler(site)
    if platform == "westelm":
        from .westelm import WestElmCrawler
        return WestElmCrawler(site)
    if platform == "idealo":
        from .idealo import IdealoCrawler
        return IdealoCrawler(site)
    if platform == "bol":
        from .bol import BolCrawler
        return BolCrawler(site)
    if platform == "cdiscount":
        from .cdiscount import CdiscountCrawler
        return CdiscountCrawler(site)
    if platform == "ikea":
        from .ikea import IkeaCrawler
        return IkeaCrawler(site)
    if platform == "allegro":
        from .allegro import AllegroCrawler
        return AllegroCrawler(site)
    if platform == "otto":
        from .otto import OttoCrawler
        return OttoCrawler(site)
    if platform == "article":
        from .article import ArticleCrawler
        return ArticleCrawler(site)
    if platform == "cratebarrel":
        from .cratebarrel import CrateBarrelCrawler
        return CrateBarrelCrawler(site)
    if platform == "houzz":
        from .houzz import HouzzCrawler
        return HouzzCrawler(site)
    if platform == "ebay":
        from .ebay import EbayCrawler
        return EbayCrawler(site)
    raise ValueError(f"未知平台: {platform}")
