"""采集器抽象基类。

每个采集器接收一个 Site，产出标准化前的「原始 product dict」列表。
字段命名对齐 Product 模型，pipeline.normalize() 负责后续清洗。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import get_settings, get_sites, user_agents
from ..fetching import CrawlCounter, CrawlerFetcher, FetchContext
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
        # 计数快照（可选）：runner 收尾时直接读 crawler.counter，这里是给
        # 需要在 CrawlResult 内携带计数的调用方留的快照位，非计算值。
        self.api_calls: int = 0
        self.browser_opens: int = 0
        self.pages_fetched: int = 0
        self.total_product_count: int | None = None
        self.coverage_complete: bool = True
        self.coverage_code: str | None = None
        self.coverage_stage: str | None = None
        self.coverage_reason: str | None = None
        self.coverage_retryable: bool | None = None
        self.coverage_suggested_action: str | None = None


class BaseCrawler(ABC):
    """采集器基类。子类实现 crawl()。"""

    platform = "base"

    def __init__(self, site: Site):
        self.site = site
        self.job_id: int | None = None
        self.settings = get_settings()
        # 每站限速档 —— 评论平台远慢于商品站（反封禁）
        self.delay = rate_delay(self.platform,
                                float(self.settings.get("request_delay", 1.5)))
        self.proxy = get_proxy(site.proxy_tier, site=site.site)
        self.counter = CrawlCounter()

    def _resolve_limit(self, default: int, explicit: int | None = None,
                       *, honor_persisted: bool = True) -> int:
        """Resolve crawl item limits.

        Explicit limits are kept for smoke tests and one-off debug runs. Some
        production crawlers need true full-store crawls, so they can opt out of
        persisted DB/YAML caps that were originally added as sampling guards.
        """
        if explicit is not None:
            return explicit
        if not honor_persisted:
            return int(default)
        config = self.site.crawler_config or {}
        if isinstance(config, dict) and config.get("max_products") not in (None, ""):
            return int(config["max_products"])
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

    def make_fetcher(self, *, kind: str = "product",
                     source: str = "unknown",
                     timeout: int = 30,
                     use_proxy: bool = True,
                     allow_stealth: bool = False,
                     **ctx_kwargs) -> CrawlerFetcher:
        """构造一个已注入本 crawler 计数器的统一 fetcher。

        额外 FetchContext 字段（retries / fail_fast_blocked /
        rotate_proxy_on_retry 等）可通过 **ctx_kwargs 透传。
        """
        return CrawlerFetcher(FetchContext(
            site=self.site,
            job_id=self.job_id,
            kind=kind,
            source=source,
            timeout=timeout,
            use_proxy=use_proxy,
            allow_stealth=allow_stealth,
            counter=self.counter,
            **ctx_kwargs,
        ))

    def count_browser_fetch(self, fn, *, success=None):
        """执行一次浏览器抓取(StealthyFetcher/playwright)，成功则 browser_opens += 1。

        fn: 无参回调，执行真实抓取并返回结果。
        success(result)->bool: 成功判定；默认 result 为真值即成功。
        异常照常上抛(与直接调用一致)，不计数。
        """
        result = fn()
        ok = success(result) if success is not None else bool(result)
        if ok:
            self.counter.browser_opens += 1
        return result

    def count_api_fetch(self, fn, *, success=None):
        """执行一次非 curl_cffi 的 HTTP API 抓取(如 reddit 的 requests)，成功则 api_calls += 1。"""
        result = fn()
        ok = success(result) if success is not None else bool(result)
        if ok:
            self.counter.api_calls += 1
        return result

    @abstractmethod
    def crawl(self) -> CrawlResult:
        """执行采集，返回 CrawlResult。"""
        raise NotImplementedError
