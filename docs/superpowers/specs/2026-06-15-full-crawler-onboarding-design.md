# 全量 crawler 收编进统一计数(32 个)

日期：2026-06-15
状态：设计已确认，待写实现计划
关联：前序工作 `2026-06-15-crawl-billing-usage-counters-design.md`(已交付：CrawlCounter/统一入口/三计数列/三路径透传/article·sephora·generic 收编)

## 背景与目标

前序工作已建成用量计数基础设施：`usage_records` 表有 api_calls/browser_opens/pages_fetched 三列；
`CrawlerFetcher` 统一入口在成功抓取时计数；网页采集(runner)与 API/MCP/Spine 路径均已贯通。
但 35 个 crawler 中只有 3 个(article/sephora/generic)收编进统一入口，其余 **32 个仍自建 HTTP/浏览器抓取，
计数恒为 0**。

本工程把剩余 32 个 crawler 全部收编，使**所有 crawler 的抓取都产生真实计数**，
且**完全不破坏各站现有的反爬定制**。

## 已确认的设计决策

| 决策 | 选择 |
|---|---|
| 收编范围 | 全部 32 个(批 A+B+C+D) |
| 批 C/D 的 stealth/浏览器如何纳入 | 混合(方式 C)：HTTP 直连段走 `make_fetcher().get()`，stealth/浏览器段走计数包装器 |
| reddit(requests 库)计数归类 | 算 api_calls，用 `count_api_fetch` 包装，不换库 |
| 核心原则 | 反爬定制零退化——包装器只"执行+成功计数"，不介入抓取策略 |

## 核心机制：统一入口 + 两个计数包装器

`BaseCrawler` 已有 `self.counter`(CrawlCounter)与 `make_fetcher()`。新增两个轻量包装器，
覆盖非 curl_cffi-GET 的抓取形态：

```python
class BaseCrawler:
    # 已有：make_fetcher() → curl_cffi GET/POST 走统一入口，成功自动计 api_calls

    def count_browser_fetch(self, fn, *, success=None):
        """执行一次浏览器抓取(StealthyFetcher/playwright)，成功则 browser_opens += 1。
        success(result)->bool 判定成功；默认 result 为真值即成功。
        调用方可传自定义判断(如 html 长度/非反爬页)。"""
        result = fn()
        ok = success(result) if success else bool(result)
        if ok:
            self.counter.browser_opens += 1
        return result

    def count_api_fetch(self, fn, *, success=None):
        """执行一次非 curl_cffi 的 HTTP API 抓取(如 reddit 的 requests)，成功则 api_calls += 1。"""
        result = fn()
        ok = success(result) if success else bool(result)
        if ok:
            self.counter.api_calls += 1
        return result
```

**每种抓取形态用最合适的工具(方式 C)：**
- 纯 HTTP GET/POST/JSON → `make_fetcher().get()/.post()`，自动计 api_calls。
- StealthyFetcher / playwright → `count_browser_fetch(lambda: ...)`，计 browser_opens。
- reddit 的 requests → `count_api_fetch(lambda: ...)`，计 api_calls，不换库。

**反爬零退化保证**：stealth 的失败判断标记、persist_profile、Kasada warmup、多路径切换等
全部留在各 crawler 原地；包装器不读取也不改变这些逻辑，只在"成功"时自增计数。

## 收编模板(复用 article/sephora 已验证模式)

每个 curl_cffi crawler 收编时的机械映射：
1. 删除 `from curl_cffi import requests as creq`(若收编后不再用)。
2. `_session()` → `_headers()` 返回 dict(保留定制 UA/Accept/Referer/Cookie)。
3. `sess.get(url, ...)` → `fetcher.get(url, headers=self._headers(), cookies=..., allow_redirects=..., timeout=...)`。
   循环内复用同一个 `fetcher = self.make_fetcher(kind=, source=)`(绑定 self.counter)。
4. 响应字段映射：`resp.status_code`→`res.status or 0`、`resp.text`→`res.text`、
   `resp.content`→`res.content`(gzip)、`str(resp.url)`→`res.final_url`、`resp.json()`→`res.json()`。
5. `resp.raise_for_status()` → 改判 `res.ok`。
6. 删除 `sess.proxies = {self.proxy}`——代理由 `make_fetcher` 默认 `use_proxy=True` 经 ProxyMiddleware 处理。
7. 保留：`self.guard(res.status or 0, where)`、`self._blocked(res.text)`、`self.snapshot(...)`、全部解析逻辑。

## 分批收编(按风险递增)

每批为独立可交付单元、各自回归。

### 批 A — 纯 curl_cffi GET(9 个)
bestbuy, cdiscount, ebay, etsy, homary, magento, shoper, vonhaus, walmart
- 直接套模板。homary/magento/vonhaus/ebay 用 `res.content` 做 gzip(FetchResult 已支持)。
- bestbuy/walmart 的 session-rotate / warmup 是反爬定制：warmup 请求作为一次 `make_fetcher().get()`
  自然计入 api_calls(或按需不计，实现时取最贴近原行为的方式)。

### 批 B — JSON API / POST(5 个)
costway, shopify, target, reviews_io, flexispot
- `make_fetcher().get(headers={"Accept":"application/json"})` + `res.json()`。
- `raise_for_status()` → `res.ok` 判断；`self.guard()` 保留。
- **flexispot 特殊**：playwright 取 token 段用 `count_browser_fetch`(计 1 次 browser_open)；
  随后批量 POST API 用 `make_fetcher().post()`(计 api_calls)。

### 批 C — curl_cffi + StealthyFetcher 混用(15 个)
aliexpress, allegro, avis_verifies, bol, cratebarrel, google_shopping, houzz, idealo,
ikea, otto, overstock, trustedshops, vidaxl, wayfair, westelm
- curl_cffi 直连段 → `make_fetcher().get()`(计 api_calls)。
- stealth 兜底段 → `count_browser_fetch(lambda: StealthyFetcher.fetch(...), success=<各站原成功判断>)`，
  原失败标记/参数/profile 逻辑原样保留。
- **otto/vidaxl 特殊**：多路径切换 / Kasada warmup 留在 crawler 内，只替换各 HTTP/stealth 调用点。
  vidaxl 当前为工作树已修改状态(` M`)，基于现状收编。
- **google_shopping** 纯 stealth(无 curl_cffi 段)，只用 `count_browser_fetch`。

### 批 D — 纯浏览器 / 特殊(3 个)
- **trustpilot**：每页 `count_browser_fetch`(计 browser_opens)。
- **google_maps**：打开浏览器那一次 `count_browser_fetch`(计 1)；后续在同一会话内滚动加载评论**不计**。
- **reddit**：`count_api_fetch` 包 requests 调用(计 api_calls)，不换库、不碰 1.2 req/s 限流与 Arctic Shift 兜底。

## 测试策略

**离线单测(每个收编的 crawler)**：复用 article/sephora 集成测试模式——
monkeypatch crawler 的 `make_fetcher`(或 `count_browser_fetch`/`count_api_fetch`)返回假 fetcher，
断言：(1) 抓取确实经过统一入口/包装器；(2) 成功抓取后 `self.counter` 累加正确；
(3) 用 fixture HTML/JSON 验证解析逻辑未退化(能解出商品或合理 notes)。不触网。

**包装器单测**：`count_browser_fetch`/`count_api_fetch` 的成功+1、失败+0、自定义 success 判断。

**回归**：每批收编后跑 `test_crawler_registry.py` / `test_crawler_limit.py` / 已有计数测试，确认无破坏。

**真实反爬行为(单测无法覆盖)**：标注为 NAS smoke 测试——收编后在生产环境对代表性站点
(每批挑 1-2 个，尤其批 C 的 Akamai/Cloudflare 站)跑真实抓取，验证 counter 累加且抓取成功率不退化。
此项不阻断单测交付，但合并前应人工确认。

## 错误处理

- 计数自增是纯内存操作，不抛错、不阻断抓取。
- 包装器的 `fn()` 若抛异常，异常照常上抛(与原直接调用行为一致)，不被吞掉；计数只在成功路径发生。
- 收编不改变各 crawler 的异常/熔断/cooldown 语义。

## 不做(YAGNI)

- 不把 stealth 兜底逻辑收进 CrawlerFetcher 的 `allow_stealth`(会丢各站定制、风险高)——明确采用包装器方式。
- 不改各站的反爬策略、限流、profile、多路径决策。
- 不改 reddit 的 requests 库与限流逻辑。
- 不改计费定价、不改计数语义(仍：只算成功、失败/重试不计、pages=api+browser)。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 收编改动 fetch 方式导致反爬行为变化(代理轮换、headers 透传遗漏) | 模板严格透传定制 headers/cookies；单测验证解析不退化；NAS smoke 验证真实成功率 |
| 批 C 的 stealth 定制在收编中被简化/丢失 | 包装器不介入 stealth 逻辑，只包裹调用；逐站保留 success 判断与参数 |
| 量大(32 个)易出错或遗漏 | 分四批、每批独立回归；每个 crawler 配离线单测 |
| 工作树有未提交在制改动(vidaxl 等) | 基于工作树现状收编；提交边界尽量聚焦(已知 main 上在制改动共存，用户已接受) |
