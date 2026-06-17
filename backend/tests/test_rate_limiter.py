"""按站点令牌桶限速 —— 并发合计不超速。"""
import threading
import time

from app.antiban import SiteRateLimiter


def test_serial_requests_spaced_by_interval():
    rl = SiteRateLimiter()
    started = time.monotonic()
    for _ in range(3):
        rl.acquire("siteA", interval=0.2)
    elapsed = time.monotonic() - started
    # 第 1 个立即放行，第 2、3 个各等 ~0.2s → 总计 ≥ 0.4s
    assert elapsed >= 0.38


def test_concurrent_threads_share_one_bucket():
    """8 线程抢同一站点桶，合计放行速率不超过 1/interval。"""
    rl = SiteRateLimiter()
    interval = 0.1
    n = 8
    started = time.monotonic()

    def worker():
        rl.acquire("siteB", interval=interval)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    # n 个请求合计至少跨越 (n-1)*interval 秒
    assert elapsed >= (n - 1) * interval * 0.9


def test_different_sites_independent():
    rl = SiteRateLimiter()
    started = time.monotonic()
    rl.acquire("s1", interval=0.3)
    rl.acquire("s2", interval=0.3)  # 不同站点不互相阻塞
    elapsed = time.monotonic() - started
    assert elapsed < 0.1


def test_acquire_respects_max_wait():
    """max_wait 封顶：极端 interval 下单次阻塞不超过 max_wait。"""
    rl = SiteRateLimiter()
    rl.acquire("s3", interval=0.1)          # 占用一个 slot
    started = time.monotonic()
    rl.acquire("s3", interval=100.0, max_wait=0.2)  # 本应等 100s，被 max_wait 截断
    elapsed = time.monotonic() - started
    assert elapsed <= 0.5


def test_fetcher_calls_acquire_before_request(monkeypatch):
    """CrawlerFetcher 每次请求前调用一次限速。"""
    import app.fetching as fetching
    from app.fetching import CrawlerFetcher, FetchContext, FetchResult
    from app.models import Site

    calls = []
    monkeypatch.setattr(fetching, "acquire_rate",
                        lambda site, platform, **kw: calls.append((site, platform)))

    site = Site(site="costway_it", platform="magento", proxy_tier="none",
                country="IT", url="https://www.costway.it/")
    fetcher = CrawlerFetcher(FetchContext(site=site, use_proxy=False))
    # 桩掉真实网络：直接返回成功
    monkeypatch.setattr(
        fetcher, "_request_once",
        lambda method, url, **kw: FetchResult(ok=True, url=url, status=200, text="ok"))

    fetcher.get("https://www.costway.it/p/123")
    assert calls == [("costway_it", "magento")]
