"""采集器抽象基类。

每个采集器接收一个 Site，产出标准化前的「原始 product dict」列表。
字段命名对齐 Product 模型，pipeline.normalize() 负责后续清洗。
"""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod

from ..config import get_settings, proxy_for_tier, user_agents
from ..models import Site


class CrawlResult:
    """一次采集的产出。"""

    def __init__(self):
        self.products: list[dict] = []
        self.categories: list[dict] = []
        self.notes: list[str] = []


class BaseCrawler(ABC):
    """采集器基类。子类实现 crawl()。"""

    platform = "base"

    def __init__(self, site: Site):
        self.site = site
        self.settings = get_settings()
        self.delay = float(self.settings.get("request_delay", 1.5))
        self.proxy = proxy_for_tier(site.proxy_tier)

    def ua(self) -> str:
        return random.choice(user_agents())

    def sleep(self) -> None:
        """C-011：请求频率控制，带抖动。"""
        time.sleep(self.delay + random.uniform(0, 0.8))

    @abstractmethod
    def crawl(self) -> CrawlResult:
        """执行采集，返回 CrawlResult。"""
        raise NotImplementedError
