# 限速 + 住宅 IP 兜底 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在统一 fetcher 层加入按站点令牌桶限速、Retry-After 优先的指数退避、以及单 job 累计 3 次 429 后自动升级住宅代理，把生产环境 99.8% 的 429（costway magento 站）压到接近 0。

**Architecture:** 三个组件全部落在 `app/antiban.py`（RateLimiter）和 `app/fetching.py`（退避 + 升级，实现现有空壳 RetryMiddleware）。爬虫代码零改动——magento 等已走统一 `CrawlerFetcher.get()`，限速接进 `_retry_loop` 后自动受益。升级状态作用域 = 单个 fetcher 实例 = 单个 job，job 结束自动复位。

**Tech Stack:** Python 3 / threading（令牌桶锁）/ curl_cffi / pytest（tmp_path + monkeypatch 风格，见 `backend/tests/test_proxy_exclude.py`）。

设计依据：`docs/superpowers/specs/2026-06-17-rate-limit-residential-fallback-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `backend/app/antiban.py` | 新增 `RateLimiter` 类 + 模块级 `acquire(site, platform)`；shoper 速率 0.35→1.0 | Modify |
| `backend/app/fetching.py` | `_retry_loop` 接入 `acquire`；实现 `RetryMiddleware`（退避 + Retry-After + 升级计数）；`ProxyMiddleware` 支持升级后改用 residential tier；`FetchContext` 加阈值字段 | Modify |
| `backend/tests/test_rate_limiter.py` | 令牌桶单元测试 | Create |
| `backend/tests/test_fetch_backoff.py` | 退避（Retry-After / 指数）单元测试 | Create |
| `backend/tests/test_residential_fallback.py` | 住宅升级 / 复位 / 代理池空单元测试 | Create |

所有命令的工作目录均为 `backend/`（`pytest` 从 backend 运行，`PYTHONPATH` 已含 backend）。

---

### Task 1: RateLimiter 令牌桶（按站点合计限速）

**Files:**
- Modify: `backend/app/antiban.py`（在文件末尾新增类与模块级函数）
- Test: `backend/tests/test_rate_limiter.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_rate_limiter.py`:

```python
"""按站点令牌桶限速 —— 并发合计不超速。"""
import threading
import time

from app.antiban import RateLimiter


def test_serial_requests_spaced_by_interval():
    rl = RateLimiter()
    started = time.monotonic()
    for _ in range(3):
        rl.acquire("siteA", interval=0.2)
    elapsed = time.monotonic() - started
    # 第 1 个立即放行，第 2、3 个各等 ~0.2s → 总计 ≥ 0.4s
    assert elapsed >= 0.38


def test_concurrent_threads_share_one_bucket():
    """8 线程抢同一站点桶，合计放行速率不超过 1/interval。"""
    rl = RateLimiter()
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
    rl = RateLimiter()
    started = time.monotonic()
    rl.acquire("s1", interval=0.3)
    rl.acquire("s2", interval=0.3)  # 不同站点不互相阻塞
    elapsed = time.monotonic() - started
    assert elapsed < 0.1


def test_acquire_respects_max_wait():
    """max_wait 封顶：极端 interval 下单次阻塞不超过 max_wait。"""
    rl = RateLimiter()
    rl.acquire("s3", interval=0.1)          # 占用一个 slot
    started = time.monotonic()
    rl.acquire("s3", interval=100.0, max_wait=0.2)  # 本应等 100s，被 max_wait 截断
    elapsed = time.monotonic() - started
    assert elapsed <= 0.5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_rate_limiter.py -v`
Expected: FAIL — `ImportError: cannot import name 'RateLimiter'`

- [ ] **Step 3: 实现 RateLimiter**

在 `backend/app/antiban.py` 末尾追加（文件已 `import threading, time` 在顶部）：

```python
# ---------- 按站点令牌桶限速 ----------
class RateLimiter:
    """每站点一个「下次可发包时刻」，并发线程抢同一把锁串行推进。

    语义：同一 site 的相邻 acquire 至少间隔 interval 秒。8 个并发线程
    抢同一 site 的桶时，合计放行速率不超过 1/interval —— 真正按住频率。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_at: dict[str, float] = {}   # site -> 下次可发包的 monotonic 时刻

    def acquire(self, site: str, *, interval: float,
                max_wait: float = 30.0) -> None:
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            earliest = self._next_at.get(site, now)
            wait = min(max(earliest - now, 0.0), max_wait)
            # 预约本次发包时刻，下一个等待者从这之后再排
            self._next_at[site] = max(earliest, now) + interval
        if wait > 0:
            time.sleep(wait)


_rate_limiter = RateLimiter()


def acquire_rate(site: str, platform: str,
                 default: float = 1.5, max_wait: float = 30.0) -> None:
    """模块级便捷入口：按 platform 的 RATE_TIERS 间隔限速该 site。"""
    interval = rate_delay(platform, default)
    _rate_limiter.acquire(site, interval=interval, max_wait=max_wait)
```

同时把 shoper 速率从异常值提到合理值 —— 修改 `RATE_TIERS`：

```python
    "shoper": 1.0,
```
（原为 `"shoper": 0.35,`）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_rate_limiter.py -v`
Expected: PASS（4 个用例全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/antiban.py backend/tests/test_rate_limiter.py
git commit -m "feat(antiban): 按站点令牌桶限速 RateLimiter + shoper 速率修正

并发线程抢同一站点桶,合计放行速率不超过 1/interval。"
```

---

### Task 2: fetcher 发包前接入限速

**Files:**
- Modify: `backend/app/fetching.py:150-179`（`_retry_loop`）
- Test: `backend/tests/test_rate_limiter.py`（追加 fetcher 集成用例）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_rate_limiter.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_rate_limiter.py::test_fetcher_calls_acquire_before_request -v`
Expected: FAIL — `AttributeError: module 'app.fetching' has no attribute 'acquire_rate'`

- [ ] **Step 3: 接入限速**

在 `backend/app/fetching.py` 顶部 import 区（现有 `from .antiban import BlockedError` 那行）改为：

```python
from .antiban import BlockedError, acquire_rate
```

在 `_retry_loop`（约 `:150`）的 for 循环内、`mw.before_request` 之前插入限速调用。修改后该段为：

```python
        for attempt in range(1, attempts + 1):
            request_kwargs = dict(kwargs)
            acquire_rate(self.context.site.site,
                         self.context.site.platform or "")
            for mw in self.middlewares:
                mw.before_request(self, url, request_kwargs)
            result = self._request_once(method, url, attempt=attempt, **request_kwargs)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_rate_limiter.py -v`
Expected: PASS（含新集成用例）

- [ ] **Step 5: 跑回归确认未破坏现有 fetch 测试**

Run: `cd backend && python -m pytest tests/test_fetch_counter.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/fetching.py backend/tests/test_rate_limiter.py
git commit -m "feat(fetching): _retry_loop 发包前接入按站点限速

magento 8 线程并发自动在令牌桶排队,整站合计 0.5 req/s。"
```

---

### Task 3: Retry-After 优先的指数退避

**Files:**
- Create: `backend/tests/test_fetch_backoff.py`
- Modify: `backend/app/fetching.py`（新增 `_backoff_seconds` 纯函数）

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_fetch_backoff.py`:

```python
"""429/503 退避 —— Retry-After 优先,无头则指数退避封顶 60s。"""
from app.fetching import _backoff_seconds, FetchResult
from app.crawl_diagnostics import FailureInfo, HTTP_429, STAGE_FETCH


def _result_with_retry_after(value):
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = value
    return r


def test_retry_after_numeric_seconds_honored():
    r = _result_with_retry_after(30.0)
    assert _backoff_seconds(r, attempt=1) == 30.0


def test_retry_after_capped_at_max():
    r = _result_with_retry_after(9999.0)
    assert _backoff_seconds(r, attempt=1) == 60.0


def test_no_header_exponential_sequence():
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = None
    # 2 * 2^(attempt-1) + jitter(0~1)，断言落在区间
    s1 = _backoff_seconds(r, attempt=1)
    s2 = _backoff_seconds(r, attempt=2)
    s3 = _backoff_seconds(r, attempt=3)
    assert 2.0 <= s1 < 3.0
    assert 4.0 <= s2 < 5.0
    assert 8.0 <= s3 < 9.0


def test_no_header_exponential_capped():
    r = FetchResult(ok=False, url="u", status=429)
    r.retry_after = None
    assert _backoff_seconds(r, attempt=10) == 60.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_fetch_backoff.py -v`
Expected: FAIL — `ImportError: cannot import name '_backoff_seconds'`

- [ ] **Step 3: 实现退避 + 解析 Retry-After**

在 `backend/app/fetching.py` 的 `FetchResult` dataclass 增加字段（在 `attempt: int = 1` 之后）：

```python
    retry_after: float | None = None
```

在 `_request_once` 成功解析响应后（构造 `result` 之前，约 `:243`）解析 Retry-After 头。在 `failure = classify_http_status(resp.status_code)` 之后插入：

```python
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
```

并把该值传入 `FetchResult(...)` 构造（在 `attempt=attempt,` 同级加 `retry_after=retry_after,`）。

在文件模块级（`_should_retry` 附近）新增两个纯函数：

```python
import random as _random

BACKOFF_BASE = 2.0
BACKOFF_MAX_SEC = 60.0


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头。仅支持秒数形式（HTTP-date 形式返回 None 退化为指数退避）。"""
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _backoff_seconds(result: "FetchResult", attempt: int) -> float:
    """429/503 退避秒数：Retry-After 优先（封顶 60s），无则指数退避 + 抖动。"""
    ra = getattr(result, "retry_after", None)
    if ra is not None and ra >= 0:
        return min(ra, BACKOFF_MAX_SEC)
    expo = BACKOFF_BASE * (2 ** (attempt - 1)) + _random.random()
    return min(expo, BACKOFF_MAX_SEC)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_fetch_backoff.py -v`
Expected: PASS（4 个用例全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/fetching.py backend/tests/test_fetch_backoff.py
git commit -m "feat(fetching): Retry-After 优先 + 指数退避封顶 60s

解析 Retry-After 头;无头则 2->4->8...->60s 指数退避带抖动。"
```

---

### Task 4: RetryMiddleware 用退避替换固定 sleep

**Files:**
- Modify: `backend/app/fetching.py`（`RetryMiddleware` 类 `:388`；`_retry_loop` 的 `:178-179`）
- Test: `backend/tests/test_fetch_backoff.py`（追加集成用例）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_fetch_backoff.py` 末尾追加：

```python
def test_retry_loop_uses_backoff_not_fixed_sleep(monkeypatch):
    """重试循环按 _backoff_seconds 退避,而非固定 min(2*attempt,5)。"""
    import app.fetching as fetching
    from app.fetching import CrawlerFetcher, FetchContext, FetchResult
    from app.crawl_diagnostics import FailureInfo, HTTP_429, STAGE_FETCH
    from app.models import Site

    slept = []
    monkeypatch.setattr(fetching.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(fetching, "acquire_rate", lambda *a, **k: None)

    site = Site(site="costway_it", platform="magento", proxy_tier="none",
                country="IT", url="https://www.costway.it/")
    fetcher = CrawlerFetcher(FetchContext(site=site, use_proxy=False, retries=2))

    def fake_429(method, url, **kw):
        r = FetchResult(ok=False, url=url, status=429,
                        failure=FailureInfo(HTTP_429, STAGE_FETCH, "429", True, "慢"))
        r.retry_after = 30.0
        return r

    monkeypatch.setattr(fetcher, "_request_once", fake_429)
    fetcher.get("https://www.costway.it/p/1")
    # 至少有一次按 Retry-After=30 退避(而非旧的 5s 封顶)
    assert any(s == 30.0 for s in slept)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_fetch_backoff.py::test_retry_loop_uses_backoff_not_fixed_sleep -v`
Expected: FAIL — 旧逻辑 sleep 的是 `min(2*attempt,5)`，`slept` 里没有 30.0

- [ ] **Step 3: 用退避替换固定 sleep**

修改 `_retry_loop` 末尾的重试 sleep（现为 `:178-179`）：

```python
            if not _should_retry(self.context, result, attempt, attempts):
                break
            time.sleep(_backoff_seconds(result, attempt))
```
（删除原来的 `if self.context.rotate_proxy_on_retry: time.sleep(min(2 * attempt, 5))`）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_fetch_backoff.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/fetching.py backend/tests/test_fetch_backoff.py
git commit -m "feat(fetching): 重试循环改用 _backoff_seconds 退避

替换固定 min(2*attempt,5);429 按 Retry-After 或指数退避,根治二次 429。"
```

---

### Task 5: 住宅代理自动升级（单 job 累计 3 次 429）

**Files:**
- Modify: `backend/app/fetching.py`（`FetchContext` 加阈值字段；`CrawlerFetcher` 加升级状态与计数；`ProxyMiddleware.before_request` 改用有效 tier）
- Test: `backend/tests/test_residential_fallback.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_residential_fallback.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_residential_fallback.py -v`
Expected: FAIL — `AttributeError: 'CrawlerFetcher' object has no attribute 'effective_tier'`

- [ ] **Step 3: 实现升级机制**

在 `FetchContext` dataclass 增加字段（`counter` 之后）：

```python
    residential_fallback_threshold: int = 3
```

在 `CrawlerFetcher.__init__` 末尾增加升级状态：

```python
        self._fail_count = 0
        self._upgraded_tier: str | None = None
```

在 `CrawlerFetcher` 类内新增三个方法（紧接 `__init__` 之后）：

```python
    def effective_tier(self) -> str | None:
        """当前生效的代理 tier：升级后为 residential，否则站点配置值。"""
        return self._upgraded_tier or self.context.site.proxy_tier

    def note_failure(self, result: "FetchResult") -> None:
        """累计 429/anti_bot 失败；达阈值且住宅可用则升级。"""
        if not (result.failure and result.failure.code in (HTTP_429, ANTI_BOT_CHALLENGE)):
            return
        self._fail_count += 1
        if self._upgraded_tier is not None:
            return
        if self._fail_count < self.context.residential_fallback_threshold:
            return
        if proxy_pool.has_available_proxy("residential", site=self.context.site.site):
            self._upgraded_tier = "residential"
        else:
            self._record_no_proxy_diag()

    def _record_no_proxy_diag(self) -> None:
        """达升级阈值但住宅代理池空 —— 记一条诊断，不静默裸打。"""
        db = SessionLocal()
        try:
            info = FailureInfo(
                PROXY_UNAVAILABLE, STAGE_FETCH,
                f"{self.context.site.site} 累计 {self._fail_count} 次反爬，"
                f"但无可用住宅代理，无法升级",
                True, "检查住宅代理池余额/白名单/冷却状态")
            record_failure(db, site=self.context.site.site,
                           job_id=self.context.job_id, info=info,
                           proxy_tier=self.context.site.proxy_tier)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
```

在 fetching.py 顶部 import 区补齐用到的符号（现有 `from .crawl_diagnostics import (...)` 块内加 `ANTI_BOT_CHALLENGE` 已有、确认含 `PROXY_UNAVAILABLE` 已有、`record_failure` 已有、`HTTP_429` 已有；`FailureInfo` 已有）。无需新增 import。

修改 `_retry_loop`：在 `last = result` 之后、判断 `_should_retry` 之前调用 `note_failure`。修改后片段：

```python
            last = result
            if result.ok:
                self._blocked_events = 0
                self._count(result)
                return result
            self.note_failure(result)
            self._raise_if_blocked_budget_exceeded(result)
```

修改 `ProxyMiddleware.before_request`：把 `ctx.site.proxy_tier` 换成 fetcher 的有效 tier。修改后：

```python
    def before_request(self, fetcher: CrawlerFetcher, url: str,
                       kwargs: dict) -> None:
        ctx = fetcher.context
        if "_proxy" in kwargs:
            return
        tier = fetcher.effective_tier()
        if not ctx.use_proxy or tier in (None, "", "none"):
            return
        proxy = proxy_pool.get_proxy(tier, site=ctx.site.site)
        if proxy:
            kwargs["_proxy"] = proxy
            return
        require_proxy = (
            ctx.require_proxy
            if ctx.require_proxy is not None
            else tier not in (None, "", "none")
        )
        if require_proxy:
            kwargs["_proxy_unavailable_tier"] = tier
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_residential_fallback.py -v`
Expected: PASS（7 个用例全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/fetching.py backend/tests/test_residential_fallback.py
git commit -m "feat(fetching): 单 job 累计 3 次 429 自动升级住宅代理

升级作用域=fetcher 实例=单 job,job 结束复位;代理池空则记诊断不裸打。"
```

---

### Task 6: 给 magento 类爬虫提高重试次数

**Files:**
- Modify: `backend/app/crawlers/magento.py`（`make_fetcher` 调用处加 `retries`）
- Test: 复用现有，跑回归

- [ ] **Step 1: 定位 magento 的 make_fetcher 调用**

Run: `cd backend && grep -n "make_fetcher" app/crawlers/magento.py`
Expected: 找到构造 fetcher 的那一行（赋给 `fetcher` / `self._fetcher`）。

- [ ] **Step 2: 加 retries 参数**

把该 `make_fetcher(...)` 调用补上 `retries=2`（给退避和住宅升级留出生效轮次）。例如：

```python
        fetcher = self.make_fetcher(kind="product", source="magento", retries=2)
```
（保留原有其它参数，仅新增 `retries=2`）

- [ ] **Step 3: 跑 magento 相关回归**

Run: `cd backend && python -m pytest tests/ -k "magento or fetch or rate or backoff or residential" -v`
Expected: PASS（无回归）

- [ ] **Step 4: 提交**

```bash
git add backend/app/crawlers/magento.py
git commit -m "feat(magento): 重试次数提到 2,给退避+住宅升级留生效轮次"
```

---

### Task 7: 全量回归 + 收尾

**Files:** 无新增，验证整体。

- [ ] **Step 1: 跑全部新增测试 + 相关回归**

Run:
```bash
cd backend && python -m pytest tests/test_rate_limiter.py tests/test_fetch_backoff.py tests/test_residential_fallback.py tests/test_fetch_counter.py tests/test_proxy_exclude.py tests/test_proxy_probe.py -v
```
Expected: 全部 PASS

- [ ] **Step 2: 确认无遗留 import / 语法错误**

Run: `cd backend && python -c "import app.fetching, app.antiban; print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 3: 最终提交（如有未提交的收尾）**

```bash
git add -A backend/
git commit -m "test: 限速+住宅兜底 全量回归通过" || echo "无待提交"
```

---

## 部署与生产验证（实现完成后单独执行，不在本计划自动化范围）

1. 用 `smart-crawler-nas-deploy` skill 把改动 scp 到 NAS，重启 worker 容器。
2. 确认住宅代理池非空：`proxy_pool.has_available_proxy("residential")`。
3. 观察 24h 后 `crawl_failures` 中 costway magento 站 `http_429` 数量是否趋近 0：
   ```sql
   SELECT site, count(*) FILTER (WHERE code='http_429') AS n429
   FROM crawl_failures WHERE occurred_at > now() - interval '1 day'
   GROUP BY site ORDER BY n429 DESC;
   ```
4. 确认住宅升级只在持续撞墙站点发生（控制代理成本）。

---

## 自审记录

- **Spec 覆盖**：令牌桶限速（Task 1-2）✅；Retry-After + 指数退避（Task 3-4）✅；
  住宅升级阈值 3 + 代理池空记诊断 + 单 job 复位（Task 5）✅；重试次数（Task 6）✅；
  与熔断关系（Task 5 `note_failure` 在 `_raise_if_blocked_budget_exceeded` 之前，
  先升级后熔断）✅；shoper 速率修正（Task 1）✅。
- **非目标**：AIMD、跨 worker 分布式限速均未纳入 ✅。
- **类型一致性**：`effective_tier()` / `note_failure()` / `acquire_rate()` /
  `_backoff_seconds()` / `_parse_retry_after()` 在定义与调用处签名一致 ✅。
  `has_available_proxy(tier, site=)` 与 proxy_pool 真实签名一致 ✅。
- **无占位符**：所有步骤含完整代码与命令 ✅。
