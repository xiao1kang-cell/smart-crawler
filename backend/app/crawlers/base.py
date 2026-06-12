"""采集器抽象基类。

每个采集器接收一个 Site，产出标准化前的「原始 product dict」列表。
字段命名对齐 Product 模型，pipeline.normalize() 负责后续清洗。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import get_settings, get_sites, user_agents
from ..models import Site
from ..proxy import get_proxy
from .. import snapshot as _snapshot
from ..antiban import check_blocked, humanized_sleep, ip_record, rate_delay


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
        # 每站限速档 —— 评论平台远慢于商品站（反封禁）
        self.delay = rate_delay(self.platform,
                                float(self.settings.get("request_delay", 1.5)))
        self.proxy = get_proxy(site.proxy_tier, site=site.site)

    def _resolve_limit(self, default: int, explicit: int | None = None) -> int:
        """limit 优先级：显式参数 > sites.yaml max_products > env 默认。"""
        if explicit is not None:
            return explicit
        hints = next((c for c in get_sites() if c["site"] == self.site.site), {})
        return int(hints.get("max_products", default))

    def ua(self) -> str:
        import random
        return random.choice(user_agents())

    def sleep(self) -> None:
        """C-011：拟人请求间隔 —— 随机抖动，不固定频率。"""
        humanized_sleep(self.delay)

    def guard(self, status: int, where: str = "") -> None:
        """熔断检查：记录 IP 用量，命中封禁状态码即抛 BlockedError。"""
        ip_record(self.proxy or "direct")
        check_blocked(status, where or self.site.site)

    def snapshot(self, name: str, content) -> None:
        """归档一份原始响应到大盘（见 app/snapshot.py）。"""
        _snapshot.save(self.site.site, name, content)

    @abstractmethod
    def crawl(self) -> CrawlResult:
        """执行采集，返回 CrawlResult。"""
        raise NotImplementedError
