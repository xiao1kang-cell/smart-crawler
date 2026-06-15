# 全量 crawler 收编进统一计数 实现计划(32 个)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把剩余 32 个 crawler 全部收编进统一计数，使所有 crawler 的抓取都产生真实 api_calls/browser_opens，且不破坏各站反爬定制。

**Architecture:** 在 BaseCrawler 加两个计数包装器(count_browser_fetch / count_api_fetch)；纯 HTTP 段走已有 make_fetcher().get()/.post()(计 api_calls)，stealth/浏览器段走 count_browser_fetch(计 browser_opens)，reddit 的 requests 走 count_api_fetch。分四批收编，每批独立回归。

**Tech Stack:** Python 3.14, SQLAlchemy, curl_cffi, scrapling(StealthyFetcher), playwright, pytest(marker unit)。测试用 `cd backend && .venv/bin/python -m pytest`。

**关联 spec:** `docs/superpowers/specs/2026-06-15-full-crawler-onboarding-design.md`

**工作目录:** 路径相对 `backend/`。所有提交 message 结尾加 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

**已知预存失败(非本工程引入，忽略):** test_pipeline_promo::test_normalize_keeps_original_none_when_missing、test_runner_diagnostics::test_execute_job_zero_products_preserves_specific_failure、test_worker_memory_gate::test_run_loop_claims_when_gate_open。这三个源于用户在制重构，**不要算作本工程的回归**。

**通用收编模板(每个 curl_cffi crawler 套用):**
1. 删 `from curl_cffi import requests as creq`(若收编后不再用)。
2. `_session()` → `_headers()` 返回 dict(保留定制 UA/Accept/Referer/Cookie)。
3. 循环外建一次 `fetcher = self.make_fetcher(kind=..., source=<platform>)`，循环内复用。
4. `sess.get(url, **kw)` → `fetcher.get(url, headers=self._headers(), cookies=..., allow_redirects=..., timeout=...)`。
5. 字段映射：`resp.status_code`→`res.status or 0`、`resp.text`→`res.text`、`resp.content`→`res.content`、`str(resp.url)`→`res.final_url`、`resp.json()`→`res.json()`、`resp.raise_for_status()`→改判 `if not res.ok:`。
6. 删 `sess.proxies = {...}`(make_fetcher 默认 use_proxy=True 经 ProxyMiddleware 处理)。
7. 保留 `self.guard(res.status or 0, where)`、`self._blocked(res.text)`、`self.snapshot(...)`、全部解析逻辑。

**每个收编 crawler 的离线单测模板(套用 article/sephora 已验证模式):**
monkeypatch crawler 的 `make_fetcher` 返回假 fetcher(其 .get 内 `crawler.counter.api_calls += 1` 并返回带 fixture 的 FetchResult)；断言 counter 累加 + 解析不退化(用 fixture HTML/JSON)。stealth/浏览器站则 monkeypatch `count_browser_fetch`。不触网。

---

### Task 1: 计数包装器 count_browser_fetch / count_api_fetch

**Files:**
- Modify: `backend/app/crawlers/base.py`(BaseCrawler 加两方法，在 make_fetcher 之后、crawl 抽象方法之前)
- Test: `backend/tests/test_count_wrappers.py`(新建)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_count_wrappers.py
from __future__ import annotations

import pytest

from app.crawlers.base import BaseCrawler, CrawlResult
from app.models import Site

pytestmark = pytest.mark.unit


class _Dummy(BaseCrawler):
    platform = "generic"

    def crawl(self) -> CrawlResult:
        return CrawlResult()


def _site():
    return Site(site="t", url="https://example.com", country="US",
                proxy_tier="none", platform="generic")


def test_count_browser_fetch_success_increments():
    c = _Dummy(_site())
    out = c.count_browser_fetch(lambda: "<html>ok</html>")
    assert out == "<html>ok</html>"
    assert c.counter.browser_opens == 1
    assert c.counter.api_calls == 0


def test_count_browser_fetch_falsy_does_not_count():
    c = _Dummy(_site())
    c.count_browser_fetch(lambda: None)
    assert c.counter.browser_opens == 0


def test_count_browser_fetch_custom_success():
    c = _Dummy(_site())
    # 自定义判断：html 长度 > 10 才算成功
    c.count_browser_fetch(lambda: "short", success=lambda r: len(r) > 10)
    assert c.counter.browser_opens == 0
    c.count_browser_fetch(lambda: "a long enough html body",
                          success=lambda r: len(r) > 10)
    assert c.counter.browser_opens == 1


def test_count_api_fetch_success_increments():
    c = _Dummy(_site())
    out = c.count_api_fetch(lambda: {"data": 1})
    assert out == {"data": 1}
    assert c.counter.api_calls == 1
    assert c.counter.browser_opens == 0


def test_count_fetch_propagates_exception():
    c = _Dummy(_site())
    def boom():
        raise RuntimeError("fetch failed")
    with pytest.raises(RuntimeError):
        c.count_browser_fetch(boom)
    assert c.counter.browser_opens == 0   # 异常不计数
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_count_wrappers.py -v`
Expected: FAIL(`AttributeError: 'BaseCrawler' object has no attribute 'count_browser_fetch'`)

- [ ] **Step 3: 实现**

在 `backend/app/crawlers/base.py` 的 `make_fetcher` 方法之后、`@abstractmethod crawl` 之前插入：

```python
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
```

- [ ] **Step 4: 跑确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_count_wrappers.py -v`
Expected: PASS(5 passed)

- [ ] **Step 5: 回归**

Run: `cd backend && .venv/bin/python -m pytest tests/test_base_crawler_counter.py tests/test_crawler_registry.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler && git add backend/app/crawlers/base.py backend/tests/test_count_wrappers.py && git commit -m "feat(crawlers): BaseCrawler 加 count_browser_fetch/count_api_fetch 计数包装器

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 批 A — 纯 curl_cffi GET(Task 2-10，9 个)

每个 Task 结构相同，差异仅在目标文件。下面给统一执行说明 + 每个 crawler 的特注。

**每个批 A crawler 的执行步骤:**
1. Read 目标 crawler 全文 + 确认其 `_session()`/`sess.get` 调用点。
2. 按"通用收编模板"改：`_session()`→`_headers()`，`sess.get`→循环外建的 `fetcher.get(..., headers=self._headers())`，响应字段映射，删 proxy 自管，保留 guard/_blocked/snapshot/解析。
3. 写离线单测 `backend/tests/test_onboard_<name>.py`(套用单测模板，fixture 用该站真实结构的最小 HTML)。
4. 跑该单测 + `tests/test_crawler_registry.py tests/test_crawler_limit.py` 回归。
5. 提交：`feat(crawlers): <name> 收编进统一入口(批A)`。

### Task 2: 收编 etsy(批A，最简单，作模板验证)
**Files:** Modify `backend/app/crawlers/etsy.py`；Test `backend/tests/test_onboard_etsy.py`
- etsy 是批A最干净的(SRP 翻页 + JSON-LD，无 warmup/gzip)。先做它确认模板。
- [ ] Read etsy.py 确认 `_session`(约61-73行) + crawl 里 `sess.get` 翻页循环。
- [ ] 按模板收编：循环外 `fetcher = self.make_fetcher(kind="product", source="etsy")`，翻页 `fetcher.get(url, headers=self._headers())`，`resp.status_code`→`res.status or 0`，`resp.text`→`res.text`，保留 guard/解析。
- [ ] 写 `test_onboard_etsy.py`：monkeypatch make_fetcher 返回假 fetcher(get 内 counter.api_calls+=1，返回带 1 个 Etsy 商品 JSON-LD 的 fixture HTML)，crawler.limit=1，断言 crawl 后 counter.api_calls≥1 且解析出≥0 商品(结构合理)。
- [ ] 跑：`cd backend && .venv/bin/python -m pytest tests/test_onboard_etsy.py tests/test_crawler_registry.py tests/test_crawler_limit.py -v`，预期 PASS。
- [ ] 提交 `feat(crawlers): etsy 收编进统一入口(批A)`。

### Task 3: 收编 shoper(批A)
**Files:** Modify `backend/app/crawlers/shoper.py`；Test `backend/tests/test_onboard_shoper.py`
- [ ] Read shoper.py(约53-67 _session) + sitemap 抓取。按模板收编(sitemap GET + 商品 GET 都走 fetcher)。
- [ ] 写单测(fixture: sitemap xml + 一个商品页)；断言 counter 累加 + 解析不退化。
- [ ] 跑单测 + registry/limit 回归。
- [ ] 提交 `feat(crawlers): shoper 收编进统一入口(批A)`。

### Task 4: 收编 vonhaus(批A，含 gzip)
**Files:** Modify `backend/app/crawlers/vonhaus.py`；Test `backend/tests/test_onboard_vonhaus.py`
- [ ] Read vonhaus.py(约70-85 _session，gzip 解析点)。收编时 sitemap 的 gzip 用 `res.content` 解压(FetchResult 已有 content)。
- [ ] 写单测(fixture 含 gzip sitemap 用 `gzip.compress(...)` 构造 + 商品页)；断言 counter 累加 + gzip 解析正常 + 商品解析不退化。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): vonhaus 收编进统一入口(批A)`。

### Task 5: 收编 homary(批A，含 gzip + 并发)
**Files:** Modify `backend/app/crawlers/homary.py`；Test `backend/tests/test_onboard_homary.py`
- [ ] Read homary.py(约54-68 _session，108-110 gzip，并发爬取点)。**注意并发**：若用线程池并发抓取，每个线程要复用同一 fetcher(counter 是共享对象，自增非原子但 GIL 下计数误差可接受；若担心可在单测注明)。按模板收编。
- [ ] 写单测；断言 counter 累加 + 解析不退化。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): homary 收编进统一入口(批A)`。

### Task 6: 收编 magento(批A，含 gzip)
**Files:** Modify `backend/app/crawlers/magento.py`；Test `backend/tests/test_onboard_magento.py`
- [ ] Read magento.py(约65-80 _session，128-131 gzip)。按模板收编，gzip 用 res.content。
- [ ] 写单测(fixture sitemap + JSON-LD 商品页)；断言累加 + 解析。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): magento 收编进统一入口(批A)`。

### Task 7: 收编 cdiscount(批A，Akamai)
**Files:** Modify `backend/app/crawlers/cdiscount.py`；Test `backend/tests/test_onboard_cdiscount.py`
- [ ] Read cdiscount.py(约70-85 _session；注意它有个 `.post()` 属 fetch_api 辅助——若是 POST 用 `fetcher.post()`，否则 GET 用 fetcher.get)。按模板收编。
- [ ] 写单测；断言累加 + 解析。跑单测 + 回归。提交 `feat(crawlers): cdiscount 收编进统一入口(批A)`。

### Task 8: 收编 bestbuy(批A，含 warmup + session rotate)
**Files:** Modify `backend/app/crawlers/bestbuy.py`；Test `backend/tests/test_onboard_bestbuy.py`
- [ ] Read bestbuy.py(约64-79 _session，impersonate=chrome131，20s warmup，session rotate per 50)。
- [ ] 收编：warmup 请求作为一次 `fetcher.get(warmup_url, headers=self._headers())`(自然计 api_calls)。session rotate 原是为换 IP/会话——收编后由 ProxyMiddleware 每请求轮换代理替代，可删除手动 rotate 逻辑(或保留为 no-op，实现时取最贴近原行为者)。impersonate=chrome131 通过 fetcher.get(impersonate="chrome131") 透传(确认 _request_once 透传 impersonate kwargs；若不透传则保留在 headers 或在 make_fetcher 层加)。
- [ ] 写单测(fixture: SRP 列表页 + PDP)；断言累加 + 解析不退化。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): bestbuy 收编进统一入口(批A)`。

### Task 9: 收编 walmart(批A，Akamai + warmup + rotate)
**Files:** Modify `backend/app/crawlers/walmart.py`；Test `backend/tests/test_onboard_walmart.py`
- [ ] Read walmart.py(约65-79 _session，warmup，rotate per 50)。同 bestbuy 处理 warmup/rotate。
- [ ] 写单测；断言累加 + 解析。跑单测 + 回归。提交 `feat(crawlers): walmart 收编进统一入口(批A)`。

### Task 10: 收编 ebay(批A，含 gzip + 双 JSON-LD 块)
**Files:** Modify `backend/app/crawlers/ebay.py`；Test `backend/tests/test_onboard_ebay.py`
- [ ] Read ebay.py(约156-175 _session + warmup，204-209 gzip，SRP 多关键词×子类目)。按模板收编，gzip 用 res.content，warmup 作一次 fetcher.get。
- [ ] 写单测(fixture: SRP + PDP 含 Product + BreadcrumbList 两个 JSON-LD)；断言累加 + 双块解析不退化。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): ebay 收编进统一入口(批A)`。

### Task 11: 批 A 整体回归
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_onboard_*.py tests/test_crawler_registry.py tests/test_crawler_limit.py tests/test_fetch_counter.py -q`
- [ ] Expected: 全绿(除已知 3 个预存失败外无新失败)。

---

## 批 B — JSON API / POST(Task 12-16，5 个)

**执行说明:** 用 `fetcher.get(headers={"Accept":"application/json"})` + `res.json()`；`raise_for_status()`→`if not res.ok: ...`；guard 保留。单测 fixture 用真实 JSON 结构。

### Task 12: 收编 shopify(批B，JSON API)
**Files:** Modify `backend/app/crawlers/shopify.py`；Test `backend/tests/test_onboard_shopify.py`
- [ ] Read shopify.py(约22-35 _session + _get_json，raise_for_status，翻页循环)。
- [ ] 收编 `_get_json`：`resp = sess.get(url)` → `res = self.<fetcher>.get(url, headers={"Accept":"application/json"})`；`self.guard(res.status or 0, url)`；`if not res.ok: raise ...`(替代 raise_for_status)；`return res.json()`。snapshot 用 res.text。fetcher 在 crawl 开头建一次存为 self._fetcher 或传参。
- [ ] 写单测(fixture: /products.json 返回 1 个含 variants 的 product JSON)；monkeypatch make_fetcher，断言 counter.api_calls 累加 + _expand 解析出变体行。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): shopify 收编进统一入口(批B)`。

### Task 13: 收编 target(批B，RedSky API)
**Files:** Modify `backend/app/crawlers/target.py`；Test `backend/tests/test_onboard_target.py`
- [ ] Read target.py(约99-106 .json()，offset 翻页 RedSky API)。收编为 fetcher.get + res.json()。
- [ ] 写单测(fixture: plp_search JSON)；断言累加 + 解析。跑单测 + 回归。提交 `feat(crawlers): target 收编进统一入口(批B)`。

### Task 14: 收编 costway(批B，Vue SPA API)
**Files:** Modify `backend/app/crawlers/costway.py`；Test `backend/tests/test_onboard_costway.py`
- [ ] Read costway.py(约45-50 .json()+raise_for_status，/api/products 翻页)。收编为 fetcher.get + res.json() + `if not res.ok`。
- [ ] 写单测(fixture: /api/products JSON)；断言累加 + 解析。跑单测 + 回归。提交 `feat(crawlers): costway 收编进统一入口(批B)`。

### Task 15: 收编 reviews_io(批B，公开 API 无代理)
**Files:** Modify `backend/app/crawlers/reviews_io.py`；Test `backend/tests/test_onboard_reviews_io.py`
- [ ] Read reviews_io.py(约34 .json()，page 翻页)。注意它"无需代理"——收编时 `make_fetcher(use_proxy=False)`。
- [ ] 写单测(fixture: reviews API JSON)；断言累加 + 解析。跑单测 + 回归。提交 `feat(crawlers): reviews_io 收编进统一入口(批B)`。

### Task 16: 收编 flexispot(批B，playwright token + POST API)
**Files:** Modify `backend/app/crawlers/flexispot.py`；Test `backend/tests/test_onboard_flexispot.py`
- [ ] Read flexispot.py(56-83 playwright bootstrap token，100-115 curl_cffi POST + .json()，sitemap GET)。
- [ ] 收编三段：
  - sitemap GET(_product_slugs 约37-39) → `fetcher.get(...)`。
  - playwright bootstrap(_bootstrap_token，58-83) → 用 `count_browser_fetch(lambda: self._do_bootstrap(), success=lambda tok: bool(tok and tok[0]))` 包裹，成功(拿到 token)计 1 次 browser_open。把现有 with sync_playwright 体抽进 `_do_bootstrap()` 返回 (auth, appid)。
  - 批量 POST API(crawl 约113-127) → `fetcher.post(api, data=json.dumps({"urlKey": slug}), headers=headers)`，`res.json()`。
- [ ] 写单测(monkeypatch make_fetcher + count_browser_fetch；fixture: sitemap + POST 返回的 item JSON)；断言 browser_opens≥1(bootstrap) + api_calls 累加(POST) + 解析出 SKU 行。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): flexispot 收编进统一入口(批B)`。

### Task 17: 批 B 整体回归
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_onboard_*.py tests/test_crawler_registry.py tests/test_crawler_limit.py -q`，Expected 全绿(除预存失败)。

---

## 批 C — curl_cffi + StealthyFetcher 混用(Task 18-32，15 个)

**执行说明(关键):**
- curl_cffi 直连段 → `fetcher.get()`(计 api_calls)。
- stealth 兜底段 → 把现有 `StealthyFetcher.fetch(url, **kw)` 调用包成 `self.count_browser_fetch(lambda: StealthyFetcher.fetch(url, **kw), success=<该站原成功判断>)`。**stealth 的参数、_BLOCK_MARKS、persist_profile、warmup 逻辑一律原样保留**，只在调用外层套 count_browser_fetch。
- success 判断：用该站原有的"成功"标准(如 `lambda page: bool(getattr(page,'html_content',None)) and not self._blocked(page.html_content)`)，对齐各站 _blocked 逻辑。
- 单测：monkeypatch make_fetcher(curl 段)与 count_browser_fetch(stealth 段)，断言两类计数分别累加 + 解析不退化。

### Task 18: 收编 ikea(批C，curl_cffi 通常成功)
**Files:** Modify `backend/app/crawlers/ikea.py`；Test `backend/tests/test_onboard_ikea.py`
- ikea 是批C最温和的(实测 curl_cffi 5 连发全 200，stealth 仅显式启用)。先做它确认混用模板。
- [ ] Read ikea.py(_session、curl 抓取、`_fetch_via_stealth` 约139行、_blocked)。
- [ ] 收编：curl 段 fetcher.get；stealth 段 `_fetch_via_stealth` 内的 `StealthyFetcher.fetch(...)` 包 count_browser_fetch(success 用 ikea 的 _blocked 判断)。其余逻辑不动。
- [ ] 写单测：fixture 含正常商品页(curl 成功路径，断言 api_calls 累加)；再 monkeypatch count_browser_fetch 验证 stealth 路径计 browser_opens。断言解析不退化。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): ikea 收编进统一入口(批C)`。

### Task 19-32: 收编其余 14 个批C crawler
对以下每个 crawler，套用 Task 18 的混用收编方式(curl→fetcher.get 计 api_calls；stealth→count_browser_fetch 计 browser_opens，保留全部反爬定制)，各配离线单测 + registry/limit 回归 + 独立提交 `feat(crawlers): <name> 收编进统一入口(批C)`：

- [ ] **Task 19: houzz** — Read houzz.py(curl + `_fetch_via_stealth` ~106，无价格实体/价格走专属 API)。收编 curl 段 + stealth 段；价格 API 调用也走 fetcher.get。单测断言两类计数 + 解析。
- [ ] **Task 20: idealo** — Read idealo.py(sitemap + 单页 GET，Akamai 503→stealth ~151)。收编。单测。
- [ ] **Task 21: trustedshops** — Read trustedshops.py(评论平台，JSON-LD，Cloudflare 403→stealth ~109，page 翻页)。收编。单测。
- [ ] **Task 22: avis_verifies** — Read avis_verifies.py(curl 主 + stealth fallback ~108，page 翻页)。收编。单测。
- [ ] **Task 23: wayfair** — Read wayfair.py(curl→stealth ~112，Cloudflare)。收编。单测。
- [ ] **Task 24: westelm** — Read westelm.py(curl 403→stealth ~141，Akamai)。收编。单测。
- [ ] **Task 25: cratebarrel** — Read cratebarrel.py(curl→stealth ~238，Akamai+IP限制，长耗时)。收编。单测。
- [ ] **Task 26: bol** — Read bol.py(sitemap-first，PDP 兜底 stealth ~241 受 TRY_PDP_ENRICH 控制，Akamai sec-if-cpt)。收编(sitemap GET→fetcher.get；PDP stealth→count_browser_fetch)。单测。
- [ ] **Task 27: overstock** — Read overstock.py(sitemap 返 200 但 body 是挑战页，PDP JSON-LD，stealth ~121)。收编；注意 sitemap 的"假 200"判断逻辑保留(用 _blocked on res.text)。单测。
- [ ] **Task 28: allegro** — Read allegro.py(首页→商品URL→stealth ~234 双阶段，403 on 任何 URL)。收编：curl 探针段 fetcher.get，stealth harvest 段 count_browser_fetch。单测。
- [ ] **Task 29: aliexpress** — Read aliexpress.py(SRP curl + PDP stealth ~181，超高反爬，body<20K 或 _BLOCK_MARKS 判失败)。收编：SRP 翻页 fetcher.get，`_fetch_via_stealth` 包 count_browser_fetch(success 用 body 长度+_BLOCK_MARKS 判断)。单测。
- [ ] **Task 30: google_shopping** — Read google_shopping.py(纯 stealth ~90/127，无 curl 段，warm_then_search + real_chrome)。**只用 count_browser_fetch** 包裹 StealthyFetcher.fetch(Bing 段若用 curl_cffi 则走 fetcher.get)。单测断言 browser_opens 累加。
- [ ] **Task 31: otto** — Read otto.py(Kasada KPSDK，首页 warmup(stealth)→商品URL harvest→curl_cffi+rotate，~142)。收编：warmup stealth 段 count_browser_fetch；商品页 curl 段 fetcher.get。**Kasada warmup/persistent profile 逻辑原样保留**，只套计数。单测。
- [ ] **Task 32: vidaxl** — Read vidaxl.py(当前 ` M` 工作树已改；三路径：API(路径1 curl)/storefront(路径2 curl)/residential proxy(路径3)；sitemap 401→stealth 兜底 ~125；proxy precheck ~121-147)。收编：基于工作树现状，各 curl 调用→fetcher.get，stealth 兜底→count_browser_fetch，**多路径切换/proxy precheck 决策逻辑留在 crawler**。单测覆盖至少一条成功路径。

### Task 33: 批 C 整体回归
- [ ] Run: `cd backend && .venv/bin/python -m pytest tests/test_onboard_*.py tests/test_crawler_registry.py tests/test_crawler_limit.py -q`，Expected 全绿(除预存失败)。

---

## 批 D — 纯浏览器 / 特殊(Task 34-36，3 个)

### Task 34: 收编 trustpilot(批D，纯 stealth 翻页)
**Files:** Modify `backend/app/crawlers/trustpilot.py`；Test `backend/tests/test_onboard_trustpilot.py`
- [ ] Read trustpilot.py(纯 StealthyFetcher ~50，解析 __NEXT_DATA__，page 翻页 ~41)。
- [ ] 收编：每页的 `StealthyFetcher.fetch(...)` 包 `count_browser_fetch(lambda: StealthyFetcher.fetch(url,**kw), success=<拿到 __NEXT_DATA__ 判断>)`，计 browser_opens。解析逻辑不动。
- [ ] 写单测(monkeypatch count_browser_fetch 返回含 __NEXT_DATA__ 的 fixture)；断言 browser_opens 随页数累加 + 解析出评论/商品。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): trustpilot 收编进统一入口(批D)`。

### Task 35: 收编 google_maps(批D，单次开浏览器 + 滚动)
**Files:** Modify `backend/app/crawlers/google_maps.py`；Test `backend/tests/test_onboard_google_maps.py`
- [ ] Read google_maps.py(StealthyFetcher ~86，打开一次 + 滚动加载评论 ~62)。
- [ ] 收编：把"打开浏览器渲染商家页"那一次 `StealthyFetcher.fetch(...)` 包 count_browser_fetch(计 1)。**滚动加载是同一会话内的交互，不计**(不要给滚动循环套计数)。解析逻辑不动。
- [ ] 写单测(monkeypatch count_browser_fetch)；断言 browser_opens == 1(无论滚动多少次)+ 解析出评论。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): google_maps 收编进统一入口(批D)`。

### Task 36: 收编 reddit(批D，requests → count_api_fetch)
**Files:** Modify `backend/app/crawlers/reddit.py`；Test `backend/tests/test_onboard_reddit.py`
- [ ] Read reddit.py(requests 库 ~15，Reddit JSON API `/r/{sub}.json` + Arctic Shift 兜底 ~30-47，1.2 req/s 限流 ~26)。
- [ ] 收编：每次 `requests.get(...).json()` 包 `self.count_api_fetch(lambda: _requests.get(url, ...), success=lambda r: r is not None and r.status_code == 200)`，计 api_calls。**不换库、不动限流(_SLEEP)与 Arctic Shift 兜底**。
- [ ] 写单测(monkeypatch reddit 模块的 requests.get 返回假 response with .json()/.status_code)；断言 api_calls 随每次 API 调用累加 + 解析出帖子。
- [ ] 跑单测 + 回归。提交 `feat(crawlers): reddit 收编进 count_api_fetch 计数(批D)`。

### Task 37: 全量最终回归
- [ ] Run: `cd backend && .venv/bin/python -m pytest -m unit -q`
- [ ] Expected: 仅已知 3 个预存失败(test_pipeline_promo / test_runner_diagnostics::zero_products / test_worker_memory_gate)，无其它失败。所有 test_onboard_*.py 全绿。
- [ ] 确认 32 个 crawler 全部收编：`cd backend && grep -L "make_fetcher\|count_browser_fetch\|count_api_fetch" app/crawlers/*.py | grep -vE "base.py|detect.py|registry.py|__init__.py|_stealth_config.py"` 应为空(每个具体 crawler 都至少用到一个计数入口)。

---

## 自检(Self-Review)

**Spec 覆盖:**
- 两个计数包装器 → Task 1 ✓
- 方式 C(HTTP 走 make_fetcher，stealth 走 count_browser_fetch) → 批 A/B(HTTP) + 批 C(混用) + 批 D ✓
- reddit 算 api_calls 用 count_api_fetch → Task 36 ✓
- 批 A(9)→Task 2-10；批 B(5)→Task 12-16；批 C(15)→Task 18-32；批 D(3)→Task 34-36 ✓(共 32)
- flexispot playwright token 计 1 browser_open → Task 16 ✓
- google_maps 滚动不计 → Task 35 ✓
- otto/vidaxl 特殊路径保留 → Task 31/32 ✓
- 反爬定制零退化(包装器不介入策略) → 批 C/D 执行说明明确 ✓
- 每批独立回归 → Task 11/17/33/37 ✓
- 测试策略(离线单测 + NAS smoke 标注) → 各 Task 单测 + spec 已述 smoke ✓

**Placeholder 扫描:** 批 A/C 的逐 crawler 任务给出"Read 真实结构后套模板"是收编的固有性质(每个 crawler 真实代码需打开)，已提供统一模板、字段映射、单测骨架、特注差异点，非占位符。Task 1 给了完整代码。

**类型一致性:** count_browser_fetch(fn, *, success=None) / count_api_fetch(fn, *, success=None) 签名跨任务一致；make_fetcher(kind, source, use_proxy, **ctx_kwargs) 与前序一致；FetchResult.ok/status/text/content/final_url/json() 与前序一致。

**已知限制(诚实记录):** 批 A/C 的逐 crawler 任务依赖收编时 Read 真实代码——这是 32 个异构 crawler 不可避免的(无法在计划阶段预写每个的完整 diff)。统一模板 + 字段映射 + 特注 + 单测骨架已把每个任务降到"机械套用"级别。真实反爬成功率需 NAS smoke 验证(spec 已述)，不阻断单测交付。
