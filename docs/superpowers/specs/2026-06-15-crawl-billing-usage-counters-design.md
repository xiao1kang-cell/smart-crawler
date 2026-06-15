# 计费修复 + 全路径用量计数（第 0 步 + 第 1 步 + 批 1 试点）

日期：2026-06-15
状态：设计已确认，待写实现计划

## 背景与问题

当前计费系统存在两个问题：

1. **网页触发的抓取不计费、不记录用量。**
   路径为 `前端 → POST /api/jobs/trigger → runner.enqueue() → CrawlJob → worker → runner.execute_job() → crawler.crawl()`，
   全程没有任何 `record_usage()` 调用（`runner.py:76-166` 确认）。`CrawlJob` 表只有
   `requested_by_workspace_id` / `requested_by_user_id`，连 api_key 都没有。
   而 API / MCP / Spine 三条路径都通过 `_meter()` / `record_usage()` 计费。

2. **缺少调用次数统计。**
   目前只有 `antiban.ip_record()` 按 IP 日配额计数（反封禁用途，非计费），以及
   `ApiKey.request_count` 在 MCP 中间件 +1。"调了多少次 API、打开了多少次浏览器、翻了多少页"
   这些指标没有任何聚合统计。

### 现状关键事实（影响方案）

- 35 个 crawler 中**只有 generic 走统一入口 `CrawlerFetcher`**，且 generic 自身还保留一条裸
  `creq.Session` 并行路径（`generic.py:63-93`）。其余 25 个直连 `curl_cffi`、12 个混用
  `curl_cffi + StealthyFetcher`、4 个主用浏览器。
- 底层原语高度收敛：几乎所有 HTTP 经 `curl_cffi`，几乎所有浏览器渲染经 `scrapling.StealthyFetcher`
  （+ flexispot 一处 `playwright.sync_playwright()`）。
- 但"走 curl_cffi"形态多样：flexispot 用 `Session().post()` + playwright 截 token；
  shopify 依赖 `resp.json()` / `raise_for_status()` / `self.guard()` 熔断控制流。
  `CrawlerFetcher` 当前**只有 `.get()`**，不支持 POST、不返回 JSON、未把浏览器当一等公民。

## 已确认的决策

| 决策点 | 选择 |
|---|---|
| 网页抓取如何计费 | **只记录用量，不扣额度**（记进 Usage 表，`credits_used` 照算用于统计/成本核算，不占用/限制 workspace 月度额度） |
| 计数存储 | 在现有 `usage_records` 表加计数列（不建明细表） |
| 计数列 | `api_calls` / `browser_opens` / `pages_fetched` |
| 计数准确性目标 | 全路径准确（长期方向：把所有 crawler 收编进统一入口 `CrawlerFetcher`） |
| 失败/重试 | **不计数**（只在拿到成功响应时 +1） |
| `pages_fetched` 定义 | `= api_calls + browser_opens`（成功抓取的页面总数，写入时计算） |
| 实现方向 | 统一入口（方案 B），而非 monkeypatch 插桩。一旦全部走统一入口，计数作为显式字段挂在 `FetchContext`，无需 contextvar 魔法 |
| **本 spec 范围** | 第 0 步（扩 `CrawlerFetcher`）+ 第 1 步（计数贯通 + 修 bug）+ 批 1 试点（纯 curl_cffi GET 站点）。批 2/3 收编留作后续 spec |

## 计数列语义

| 列 | 语义（只算成功，失败/重试不计） | +1 时机 |
|---|---|---|
| `api_calls` | curl_cffi 成功拿到可用响应的次数。同一 URL 重试 3 次才成功 → 只 +1；始终失败 → +0 | `FetchResult.ok == True` 且 `fetcher` 为 curl_cffi 类 |
| `browser_opens` | 浏览器（StealthyFetcher / playwright）成功渲染到可用页面的次数 | `FetchResult.ok == True` 且 `fetcher` 为 scrapling/playwright 类 |
| `pages_fetched` | 成功抓取的页面总数 | 写入 Usage 时计算 `= api_calls + browser_opens` |

"成功"判定复用 `CrawlerFetcher` 现有逻辑：HTTP 2xx/3xx 且非反爬挑战页（`_looks_like_anti_bot`），
即 `FetchResult.ok`。HTTP 4xx/5xx、反爬页、异常，一律不计。

## 架构设计

### 计数器：显式字段，无魔法

新增 `CrawlCounter`（轻量数据类，建议放 `app/usage_metering.py` 或 `fetching.py`）：

```python
@dataclass
class CrawlCounter:
    api_calls: int = 0
    browser_opens: int = 0

    @property
    def pages_fetched(self) -> int:
        return self.api_calls + self.browser_opens
```

- 由 `BaseCrawler.__init__` 创建 `self.counter = CrawlCounter()`。
- 注入到每个 `CrawlerFetcher`（经 `FetchContext.counter`）。
- `CrawlerFetcher` 在 `result.ok` 时按 `result.fetcher` 分流自增。
- `CrawlResult` 收尾时从 `self.counter` 带出三个计数。

线程边界问题消失：计数器是显式对象引用，`advanced_scrape_url` 即使用 `ThreadPoolExecutor`，
只要把同一个 counter 引用传进去即可，不依赖 contextvar 传播。

### 第 0 步：把 `CrawlerFetcher` 扩成够用的网络层（前置，必须先做）

`fetching.py` 现状：`CrawlerFetcher.get()` 返回 `FetchResult`，内含 status/text/content/ok/fetcher，
已有代理（`ProxyMiddleware`）、重试（`_should_retry`）、反爬识别（`_looks_like_anti_bot`）、
失败分类、URL 状态、proxy_health 记录、stealth 兜底（`_get_stealth`）。

需要补齐的能力：

1. **计数**：`FetchContext` 加 `counter: CrawlCounter | None = None`。
   在 `get()`（及新 `request()`）拿到最终结果后，若 `result.ok` 且 `counter is not None`：
   - `result.fetcher in {"curl_cffi"}` → `counter.api_calls += 1`
   - `result.fetcher in {"scrapling", "playwright"}` → `counter.browser_opens += 1`
   只对**最终成功**的那次结果计数；重试过程中失败的中间结果不计（它们 `ok=False`）。

2. **POST / 通用 method 支持**：加 `request(method, url, ...)` 与便捷 `post()`，
   复用 `_get_once` 的同一套 Session 构建、代理、重试、反爬、计数逻辑
   （把 `_get_once` 泛化为 `_request_once(method, ...)`，`get` 委托给它）。

3. **JSON / 控制流兼容**：迁移时用 `res.ok` / `res.status` 替代 `raise_for_status()`，
   用 `FetchResult.text` 自行 `json.loads`（或在 `FetchResult` 上加便捷 `.json()` 方法）。
   熔断改用 `FetchContext.fail_fast_blocked`（已存在，命中 401/403/429 抛 `BlockedError`，
   等价于现有 `self.guard()`）。

4. **浏览器一等公民**：现有 `_get_stealth()` 是私有兜底。提供主动入口
   `get(url, render=True)` 或 `fetch_browser(url)`，让主用浏览器的 crawler（trustpilot /
   google_maps 等，后续批次）也能走统一入口并被计数。本 spec 只需把入口建好并计数正确，
   实际迁移浏览器类 crawler 属批 3（后续 spec）。

### 第 1 步：计数贯通到 Usage + 修计费 bug

#### 数据模型

`models.Usage` 加三列，默认 0、非空：

```python
api_calls = Column(Integer, nullable=False, default=0)
browser_opens = Column(Integer, nullable=False, default=0)
pages_fetched = Column(Integer, nullable=False, default=0)
```

幂等迁移：`ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS ...`，
同时兼容 Postgres（生产）与 SQLite（测试），接入现有启动建表 / 迁移流程。

#### record_usage 扩展

`billing.record_usage()` 增加三个参数（默认 0，向后兼容）：

```python
def record_usage(api_key_id, endpoint, record_count, bytes_returned, duration_ms,
                 credits_used=None, workspace_id=None,
                 api_calls=0, browser_opens=0, pages_fetched=0) -> None:
```

#### 修 bug：网页/后台采集写 Usage

`runner.execute_job()` 成功分支（`runner.py:129-166`）新增一次记录：

```python
record_usage(
    api_key_id=None,                                   # 网页触发无 api_key
    workspace_id=job.requested_by_workspace_id,        # 归属 workspace
    endpoint="/crawl/job",
    record_count=stats["inserted"] + stats["updated"],
    credits_used=<按现有口径估算>,                       # 照算，用于统计/成本核算
    bytes_returned=0,
    duration_ms=int(job.duration_sec * 1000),
    api_calls=crawler.counter.api_calls,
    browser_opens=crawler.counter.browser_opens,
    pages_fetched=crawler.counter.pages_fetched,
)
```

**不扣额度的保证**：网页抓取的 Usage 行 `api_key_id=None`，仅按 `workspace_id` 归属用于统计。
现有额度检查（`agent_runtime._balance_after` / 配额拦截）只作用于 API key 维度的请求入口，
不会对这行做拦截。需在实现时确认：写入这行**不触发**任何 workspace 级别的额度扣减或拦截。

`credits_used` 估算口径：与现有 crawl 计费一致（参考 `agent_crawler.crawl_site` 的
`max(1, min(limit, 10_000))` 思路，或按产出 record 数）——实现计划阶段定具体公式，
本 spec 只要求"照算并写入"，不做额度限制。

#### API / MCP / Spine 路径透传

在已有 `record_usage` / `_meter` 调用处（`v2.py:_meter`、`mcp_server.py:_record_tool_usage`、
`spine_queue._record_execute_usage`）把对应 scrape 的计数透传进去。
这些路径每次基本抓 1 个 URL，计数小但口径统一。
**避免双重计数**：计数只在 `CrawlerFetcher` 原语层做一次；generic 经 `CrawlerFetcher` 的路径
不再额外计数。

### 批 1 试点：纯 curl_cffi GET 站点收编

选一组形态最简单的 crawler 作为收编模板验证（纯 `creq.Session().get()`、无 POST、无浏览器）：
候选 `article` / `bestbuy` / `etsy` / `sephora` / `homary` / `magento` / `shoper` / `vonhaus` /
`cdiscount` / `costway`。

每个：把裸 `creq.Session().get()` 替换为 `CrawlerFetcher.get()`（经注入的 counter），
迁移后该 crawler 的 `api_calls` 自动从 0 变准。逐个迁移→回归（尤其反爬敏感站点行为不退化）。

批 1 的目标是**验证收编模板可行**，不要求一次迁移全部 10 个——实现计划阶段可只取 2-3 个跑通，
其余按同模板在后续批次推进。

## 数据流

```
网页触发抓取:
  /api/jobs/trigger → enqueue() → CrawlJob → worker → execute_job()
      └─ crawler.counter = CrawlCounter()
      └─ crawl() 内每个 CrawlerFetcher.get() 成功 → counter += 1
      └─ 成功后 record_usage(api_key_id=None, workspace_id=…, 三个计数=…)
            └─ 写入 usage_records（不扣额度）

API/MCP/Spine:
  scrape_url() → CrawlerFetcher（同一 counter）→ 已有 record_usage 处透传计数
```

## 错误处理

- 计数失败不应影响抓取：counter 自增是纯内存操作，无 IO，不会抛错。
- `record_usage` 写库失败：复用现有 `record_usage` 的事务处理（`SessionLocal` + commit），
  失败不阻断 `execute_job` 返回（与现有 worker 的容错一致；若需要可包 try/except 记日志）。
- 迁移 SQL 幂等，重复启动不报错。

## 测试

- **`CrawlerFetcher` 扩展单测**：
  - 计数语义：成功 +1、失败 +0、重试到成功只 +1、始终失败 +0。
  - 按 fetcher 类型分流：curl_cffi → api_calls，stealth → browser_opens。
  - POST / request 走同一套代理重试计数。
- **集成测试**：模拟一次 web 触发抓取 → 断言新增一行 Usage 且 `api_calls > 0`、
  `pages_fetched == api_calls + browser_opens`、`credits_used` 正确；
  断言 workspace 额度**未被扣减**（只记录不限制）。
- **回归**：API/MCP/Spine 路径计数透传且不双重计数；批 1 收编的 crawler 产出不退化。
- **迁移测试**：SQLite 与 Postgres 下 `ADD COLUMN IF NOT EXISTS` 幂等。

## 不做（YAGNI）

- 不做逐动作明细表（每次请求一行）。
- 不在本 spec 内收编批 2（JSON/POST 站点）和批 3（浏览器类）——留后续 spec。
- 不改现有 credit 定价。
- 不对网页抓取做额度拦截（只记录）。
- 不引入 monkeypatch / contextvar 插桩（既然走统一入口，用显式 counter）。

## 后续 spec（不在本次范围）

- 批 2 收编：JSON API / POST 站点（shopify / walmart / target / flexispot API 部分），
  依赖第 0 步的 POST/JSON 能力。
- 批 3 收编：curl_cffi + StealthyFetcher 混用的 12 个 + 主用浏览器的 4 个，
  依赖第 0 步的浏览器一等公民入口。
- 残留手工计数点：flexispot 的 playwright 截 token、google_maps 滚动加载——
  这些非"抓 URL"形态，在各自处手动 `browser_opens += 1`。
```
