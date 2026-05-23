# Crawlee 在 smart-crawler 的集成路径

> **决策状态**：技术选型 · 评估期（W0-W2 PoC · W3 决策）
> **PoC 文件**：`backend/app/crawlee_poc.py`
> **目标**：ALL-IN 路线（46 站 + 5 社媒 + ASR + 多区域 K8s）下，把"调度 / 重试 / 持久化 / 代理路由"从自维护切到 Crawlee 底座，减少 60% 平台代码量

---

## 1. 定位 · Crawlee 在 smart-crawler 的角色

```
┌────────────────────────────────────────────────────────────────────┐
│  smart-crawler 业务层（不变）                                       │
│  · site profile (sites.yaml)                                       │
│  · 字段映射 (export.py · 32/20/13 字段 schema)                     │
│  · NLP / 情感分析 (nlp.py · llm.py)                                │
│  · 销量倒推（评论增量法 · pipeline.py）                            │
│  · 计费 / 用量 (billing.py · 新增)                                 │
└────────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────────┐
│  抓取调度层（待迁移到 Crawlee）                                     │
│  · 现状：自定义 runner.py + scheduler.py + worker.py               │
│  · 目标：Crawlee Crawler + RequestQueue + Dataset                  │
└────────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────────┐
│  HTTP 抓取层（保留 + 增强）                                         │
│  · curl_cffi（impersonate Chrome TLS · 保留）                      │
│  · Scrapling StealthyFetcher（高反爬站 · 保留）                    │
│  · Crawlee PlaywrightCrawler（社媒 + Cloudflare 站 · 新增）        │
└────────────────────────────────────────────────────────────────────┘
```

**核心定位**：Crawlee 替换"调度 + 重试 + 持久化 + session pool"基础设施，**不替换** curl_cffi / StealthyFetcher / 业务 schema。

---

## 2. 何时用 Crawlee vs 何时用 curl_cffi

| 场景 | 选择 | 理由 |
|---|---|---|
| 已有 Vidaxl 12 站（curl_cffi 跑顺） | **保留 curl_cffi** | 不修复运行的东西 |
| Costway 9 站（API 分页） | **保留 curl_cffi** | TLS impersonation 不可替代 |
| SONGMICS 6 站（Shopify 全量拉） | **保留 curl_cffi** | 极简 · Crawlee 反而过重 |
| Amazon US/UK | **Scrapling + curl_cffi 双备份** | 反爬强度高 · 现有方案稳定 |
| Reddit playbook | **保留现有** | snscrape 类似工具更直接 |
| **新增** Lazada/Shopee/Flipkart（VIVO POC） | **Crawlee PlaywrightCrawler** | 大量 JS 渲染 + session pool 必需 |
| **新增** Instagram / TikTok | **Crawlee PlaywrightCrawler** | 复杂会话 + cookie 持久化 |
| **新增** Facebook | **Crawlee PlaywrightCrawler + StealthyFetcher** | 反爬最强 |
| **新增** 海尔 friend brand 监测 | **Crawlee BeautifulSoupCrawler** | 多区域 K8s 部署 · 需要 Request Queue 跨节点共享 |
| **新增** 汽车垂媒 7 站（极氪） | **Crawlee BeautifulSoupCrawler** | 标准 HTML 站 · 用 Crawlee 节省 60% 开发时间 |

**判断规则**：
- **保留现有**：已稳定运行 · 无新需求 · 反爬已通过
- **走 Crawlee**：新平台 + 需要 session pool / cookie 持久化 / 跨节点 Request Queue
- **混合**：Crawlee 做调度 · 内部 fetcher 仍用 curl_cffi / StealthyFetcher（Crawlee 允许 plug-in HTTP 客户端）

---

## 3. 集成路径 · 4 周分阶段

### W1（PoC 验证 · 不动生产）

**目标**：跑通 `crawlee_poc.py` · 用 Hacker News 验证安装 / 抓取 / dataset 三件套

- [ ] `pip install 'crawlee[beautifulsoup]'`
- [ ] 跑 `python -m backend.app.crawlee_poc`（用户验收时跑，不要现在跑）
- [ ] 验证输出前 10 条 + 时间戳
- [ ] 加 `crawlee` 到 `requirements.txt`（待用户决策后）

### W2（Vidaxl 一站迁移 PoC）

**目标**：选择 `vidaxl_us`（业务暂停 · 安全） · 用 Crawlee 重写采集 · 对比性能

- [ ] 新建 `backend/app/crawlers/vidaxl_crawlee.py`（不替换 `vidaxl.py`）
- [ ] 复用现有 `sites.yaml` 配置 · 复用 32 字段 schema
- [ ] 集成 curl_cffi 作为 Crawlee 的 HTTP client（自定义 `HttpxHttpClient` 子类）
- [ ] 对比指标：抓取速度 / 失败率 / 代码行数 / 重试逻辑复杂度
- [ ] 不上生产 · 不修改现有 `runner.py`

### W3（决策 + Crawlee adapter 层）

**目标**：基于 W2 结果决定是否全面迁移 · 落地 adapter 层

如果 W2 验证通过：
- [ ] 新建 `backend/app/crawlee_runner.py`（与 `runner.py` 并存）
- [ ] adapter：把 site profile → Crawlee Crawler 配置
- [ ] adapter：把 Crawlee dataset → smart-crawler `Product` / `Review` ORM
- [ ] 加 feature flag：`USE_CRAWLEE_FOR=['vidaxl_us']`（按站点切换）

如果 W2 验证不通过：
- [ ] 文档化原因 · 归档 PoC · 不动现有代码

### W4（新平台用 Crawlee · 老平台保留）

**目标**：所有新平台（Lazada/Shopee/Flipkart/IG/TikTok/汽车垂媒）默认用 Crawlee 底座 · 老平台不动

- [ ] Lazada/Shopee/Flipkart 三 crawler 用 Crawlee PlaywrightCrawler 实现
- [ ] 汽车垂媒 7 站用 Crawlee BeautifulSoupCrawler 实现
- [ ] 海尔 friend brand 监测用 Crawlee（多区域 K8s 部署友好）

---

## 4. 优势 / 风险 矩阵

### 优势

| 维度 | 自维护（现状） | Crawlee 底座 |
|---|---|---|
| Request Queue | 自写 `crawl_jobs` 表 + worker.py | 内置 · SQLite/PG/Redis backend |
| 重试 + 退避 | 散落各 crawler | 全局策略 · `max_request_retries` |
| 代理路由 | `proxy_pool.py` 自维护 | 内置 `ProxyConfiguration` · session-aware |
| Session pool | 各 crawler 自维护 cookies | 内置 `SessionPool` · 自动轮换 |
| Dataset 持久化 | 直写 SQLite | 内置 `Dataset` · JSON Lines / CSV / Excel |
| Fingerprint 注入 | StealthyFetcher 单独处理 | 内置 fingerprint generation |
| 跨节点队列共享 | 需自写 PG 协调 | 配置 RedisRequestQueue 即可 |
| 调度日志 + 监控 | 自维护 | 内置 statistics + dashboard hooks |

### 风险

| 风险 | 概率 | Mitigation |
|---|---|---|
| Crawlee 性能不如手写 curl_cffi | 中 | W2 PoC 量化对比 · 不行就只用调度层 |
| Crawlee API 后续变动 | 中 | Apify 维护 · 6.5k⭐ · 但仍是 alpha 期 |
| 我们的反爬手法不被 Crawlee 内置支持 | 低 | Crawlee 允许自定义 HTTP client · 可塞 curl_cffi |
| 学习曲线 | 低 | Crawlee 文档完善 · Python 版 API 简洁 |
| 现有 32/20/13 字段 schema 与 Crawlee dataset 冲突 | 低 | adapter 层做映射 |

---

## 5. ALL-IN 路线对 Crawlee 的依赖

ALL-IN 路线规模化时 · Crawlee 提供 5 个不可替代的能力：

1. **跨节点 Request Queue**（海尔多区域 K8s 部署必需）
   - SG region / EU region / US region 三套 worker 共享同一队列
   - Crawlee 配 RedisRequestQueue 一行搞定

2. **Session pool + cookie 持久化**（IG/FB/TikTok 必需）
   - 自维护成本极高 · Crawlee 内置

3. **批量代理路由**（10 商业代理池 → 100 代理池规模化）
   - Crawlee 的 ProxyConfiguration 支持 sessionId-aware 路由

4. **Dataset → Excel/CSV/JSON Lines 一键导出**
   - 现有 `export.py` 自写 · Crawlee 内置 + 自动分批

5. **Statistics + 监控 hook**
   - Crawlee 输出 `crawler.statistics.calculate()` · 可对接 Prometheus

---

## 6. 报价对客户的影响

切到 Crawlee 底座后 · 部署成本与价格对应关系：

| 客户 | 现状报价 | Crawlee 后报价 | 节省理由 |
|---|---|---|---|
| 遨森 ¥120k 起 | 同 | 同 | 老站不迁移 |
| VIVO 电商 ¥50-100w | 同 | **¥40-80w**（-20%） | Lazada/Shopee 用 Crawlee 节省 4 周开发 |
| VIVO 社媒 ¥100-300w | 同 | **¥80-240w**（-20%） | IG/FB/TikTok 用 Crawlee 节省 6 周开发 |
| 海尔 ¥80-200w | 同 | **¥80-200w** | 多 region K8s · Crawlee 是关键 enabler · 报价不降但毛利提升 |
| 极氪 ¥50-150w | 同 | **¥40-120w**（-20%） | 汽车垂媒 7 站用 Crawlee 节省 2 周 |

**结论**：Crawlee 不是省客户钱 · 是省我们开发时间 + 提升毛利。

---

## 7. 立即可做的下一步

1. **不要现在跑** `crawlee_poc.py`（用户睡觉 + 不动生产）
2. 用户验收时一起跑一次 PoC · 看输出
3. W2 选 `vidaxl_us` 做迁移对比 · 决策是否扩到 Lazada/Shopee
4. W4 之前不上生产 · feature flag 控制
5. 持续更新本文档 · 每个新平台决策都记录"为什么用 Crawlee / 为什么不用"

---

**Owner**：boyuan@solvea.cx
**Reviewer**：（待补）
**Last Updated**：2026-05-22
