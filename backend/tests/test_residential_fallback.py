"""住宅代理自动升级 —— 单 job 累计 3 次 429/anti_bot 后切 residential。"""
import app.fetching as fetching
from app.fetching import CrawlerFetcher, FetchContext, FetchResult
from app.crawl_diagnostics import (
    FailureInfo, HTTP_429, ANTI_BOT_CHALLENGE, STAGE_FETCH)
from app.models import Site


def _site():
    return Site(site="costway_it", platform="magento", proxy_tier="none",
                country="IT", url="https://www.costway.it/")


def _fetcher(monkeypatch, residential_available=True):
    monkeypatch.setattr(fetching, "acquire_rate", lambda *a, **k: None)
    monkeypatch.setattr(fetching.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetching.proxy_pool, "has_available_proxy",
                        lambda tier, site=None: residential_available)
    return CrawlerFetcher(FetchContext(site=_site(), use_proxy=True, retries=0))


def test_effective_tier_starts_as_configured(monkeypatch):
    f = _fetcher(monkeypatch)
    assert f.effective_tier() == "none"


def test_upgrades_after_threshold(monkeypatch):
    f = _fetcher(monkeypatch)
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(3):
        f.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))
    assert f.effective_tier() == "residential"


def test_anti_bot_also_counts(monkeypatch):
    f = _fetcher(monkeypatch)
    fail = FailureInfo(ANTI_BOT_CHALLENGE, STAGE_FETCH, "bot", True, "代理")
    for _ in range(3):
        f.note_failure(FetchResult(ok=False, url="u", status=200, failure=fail))
    assert f.effective_tier() == "residential"


def test_below_threshold_no_upgrade(monkeypatch):
    f = _fetcher(monkeypatch)
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(2):
        f.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))
    assert f.effective_tier() == "none"


def test_no_upgrade_when_pool_empty(monkeypatch):
    f = _fetcher(monkeypatch, residential_available=False)
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(5):
        f.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))
    # 代理池空 → 不升级,仍 none
    assert f.effective_tier() == "none"


def test_proxy_middleware_uses_effective_tier(monkeypatch):
    """升级后 ProxyMiddleware 用 residential 取代理。"""
    # proxy_lease_ttl_sec=0 强制走 get_proxy 分支（不走 lease），
    # 以便 monkeypatch 的 get_proxy 能观察到 tier 值。
    monkeypatch.setenv("PROXY_LEASE_TTL_SEC", "0")
    f = _fetcher(monkeypatch)
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(3):
        f.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))

    seen = {}
    monkeypatch.setattr(fetching.proxy_pool, "get_proxy",
                        lambda tier, site=None: seen.setdefault("tier", tier) or "http://p:1")
    kwargs = {}
    fetching.ProxyMiddleware().before_request(f, "u", kwargs)
    assert seen["tier"] == "residential"


def test_new_instance_resets(monkeypatch):
    """新 fetcher（新 job）默认不带升级状态。"""
    f1 = _fetcher(monkeypatch)
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(3):
        f1.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))
    assert f1.effective_tier() == "residential"
    f2 = _fetcher(monkeypatch)
    assert f2.effective_tier() == "none"


def test_pool_empty_logs_diag_only_once(monkeypatch):
    """住宅池持续空 + 失败持续来 → 诊断每 job 至多记一次。"""
    f = _fetcher(monkeypatch, residential_available=False)
    calls = []
    monkeypatch.setattr(f, "_record_no_proxy_diag", lambda: calls.append(1))
    fail = FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢")
    for _ in range(20):
        f.note_failure(FetchResult(ok=False, url="u", status=429, failure=fail))
    assert len(calls) == 1
