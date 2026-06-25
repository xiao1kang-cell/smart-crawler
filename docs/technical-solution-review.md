# smart-crawler 技术方案审查稿

> 日期：2026-06-25  
> 用途：供技术负责人审查当前整体技术方案、分布式 mini-worker 方案与 NAS 存储方案。  
> 范围：基于当前仓库代码与现有设计文档整理，不等同于最终上线验收报告。

## 1. 项目定位

smart-crawler 当前已经从早期的单机 MVP 爬虫，演进为一个面向跨境电商与通用网页数据采集的服务化平台。

核心能力包括：

- 竞品站商品、价格、促销、评论、趋势与覆盖率采集。
- 客户前台看板、报表、任务、按需抓取、红人发现等产品功能。
- 超管后台，用于租户、用户、代理池、队列、数据质量、计费、审计管理。
- 面向 AI Agent 的 MCP 与 REST v2 API。
- PostgreSQL 队列化采集、多 worker 并发、代理租约、用量计费、多租户隔离。
- 通用数据脊柱 SPINE，用于任意 URL 的 raw snapshot、schema extraction、dataset 查询与 TTL warehouse-first 复用。

一句话概括：

```text
smart-crawler = 采集引擎 + 数据仓库 + Agent API/MCP + 客户控制台 + 超管运营后台
```

## 2. 当前总体架构

```text
Browser / Customer / AI Agent
        |
        v
Cloudflare Tunnel / smartcrawler.io
        |
        v
NAS smart-crawler Web 容器
  - FastAPI
  - REST API
  - MCP /mcp
  - 前台 /app
  - 后台 /admin
  - scheduler
        |
        v
NAS PostgreSQL
  - warehouse data
  - crawl_jobs / spine_jobs
  - proxy leases / health
  - tenants / users / api keys
  - usage / audit
        |
        v
Workers
  - NAS Docker workers
  - Mac mini native workers
        |
        v
Target Sites / Proxy Pools / LLM Gateway
```

## 3. 部署组件

当前 `docker-compose.yml` 中的主要服务：

| 服务 | 职责 | 状态 |
|---|---|---|
| `postgres` | PostgreSQL 16 主数据库 | 已配置 |
| `smart-crawler` | FastAPI + 前台 + 后台 + MCP + scheduler | 已配置 |
| `worker_1..worker_13` | 独立采集 worker，消费 `crawl_jobs` | 已配置 |
| `worker_dispatcher` | 可选分发器，给多节点 worker 分配任务 | 已配置 profile |
| `cloudflared` | Cloudflare Tunnel，对外暴露 `smartcrawler.io` | 已配置 profile |

镜像构建方式：

- 前台：`frontend-app`，Vue 3 + Vite。
- 后台：`admin-app`，Vue 3 + Vite。
- 后端：Python 3.12 slim + FastAPI + Playwright Chromium。

## 4. 后端技术栈

| 类别 | 技术 |
|---|---|
| Web/API | FastAPI, Uvicorn |
| 数据库 | SQLAlchemy, PostgreSQL, SQLite local fallback |
| 调度 | APScheduler |
| 抓取 | curl_cffi, requests, Playwright, Patchright, Scrapling |
| 解析 | selectolax, lxml, JSON-LD/HTML heuristic extraction |
| 报表 | pandas, openpyxl |
| MCP | FastMCP |
| LLM | OpenAI/Anthropic compatible SDK, flatkey.ai gateway |
| 前端 | Vue 3, Vue Router, Pinia, Nuxt UI, ECharts, lucide-vue-next |

## 5. 数据模型分层

### 5.1 电商 warehouse 层

核心表：

- `sites`：站点配置与追踪状态。
- `products`：SKU/SPU 商品主表。
- `price_history`：价格历史。
- `categories`：分类树。
- `promotions`：促销活动。
- `trends`：站点日趋势。
- `site_metrics`：站点汇总指标缓存。

### 5.2 采集队列与诊断层

- `crawl_jobs`：电商站点采集任务队列。
- `crawl_urls`：URL frontier，记录发现、抓取、解析生命周期。
- `crawl_failures`：结构化失败事件。

状态机：

```text
pending -> running -> success / failed / partial / skipped
```

worker 支持：

- 原子 claim，避免重复领取。
- heartbeat，避免误杀长任务。
- runtime timeout。
- stale running job 回收。
- 内存闸 `MEM_GATE_THRESHOLD`。
- trigger allowlist。
- workspace allowlist/blocklist。

### 5.3 多租户与权限层

- `workspaces`
- `workspace_members`
- `workspace_sites`
- `users`
- `user_sessions`
- `invite_codes`
- `report_configs`
- `report_runs`

租户隔离原则：

- warehouse 数据共享，不为每个租户复制一份商品大表。
- workspace 控制哪些站点可见、可配置、可出报表。
- API key 与 usage 归属 workspace。

### 5.4 API Key、计费与缓存层

- `api_keys`
- `usage_records`
- `rate_limit_events`
- `agent_cache`

用量记录维度：

- endpoint
- records
- credits
- bytes_returned
- duration_ms
- api_calls
- browser_opens
- pages_fetched

### 5.5 代理池层

- `proxy_endpoints`
- `proxy_pools`
- `proxy_pool_members`
- `proxy_rules`
- `proxy_leases`
- `proxy_health`

职责拆分：

```text
proxy_leases  = 全局并发互斥，防同一出口 IP 被多个 worker 同时使用
proxy_health  = 节点级健康判断，防某节点的失败误伤其他节点
```

### 5.6 SPINE 通用数据层

SPINE 用于把系统从“电商表适配器集合”扩展为“通用网页数据采集平台”。

核心表：

- `raw_snapshots`：raw 层，保存原始抓取元数据，正文 gzip 在磁盘。
- `datasets`：view 层入口，命名数据集。
- `extracted_records`：normalized 层，任意 schema 结构化结果。
- `spine_jobs`：通用异步抓取队列。

SPINE 设计原则：

- 不破坏现有电商表。
- 默认 warehouse-first。
- TTL 内命中不重爬。
- force_live 默认进入 staging，避免污染主库。
- 低置信或疑似反爬结果进入 staging/quarantine。

## 6. 采集体系

站点配置来自 `backend/sites.yaml`，当前覆盖：

- SONGMICS
- Costway
- Homary
- Yaheetech
- Vidaxl
- Flexispot
- BCP
- VonHaus
- Woltu
- Wayfair
- Overstock
- Idealo
- Otto
- Bol
- CDiscount
- IKEA
- Crate&Barrel
- WestElm
- Allegro
- Article
- eBay
- Walmart
- Target
- AliExpress
- Etsy
- BestBuy
- Sephora
- Lazada 等

采集器分发逻辑：

```text
Site.platform -> crawler registry -> specialized crawler
unknown platform -> GenericCrawler
```

主路径：

```text
scheduler / API trigger
  -> enqueue crawl_jobs
  -> worker claim_job
  -> crawler fetch
  -> pipeline clean/upsert
  -> promotions detection
  -> analytics recompute
  -> site_metrics refresh
  -> job success/failed
```

## 7. API 与 MCP 方案

### 7.1 REST API

主要分层：

- `/api/*`：客户控制台 API。
- `/api/v2/*`：Agent-first / Firecrawl-compatible API。
- `/discover/*`：红人发现，Apify-compatible run/dataset lifecycle。
- `/api/admin/spine/*`：超管后台管理 API。
- `/health`：健康检查。

### 7.2 MCP

MCP 端点：

```text
/mcp
```

鉴权：

```text
Authorization: Bearer sck_...
```

Agent-first 主推工具：

| 工具 | 说明 |
|---|---|
| `query_warehouse(intent, limit)` | 自然语言查询已有 warehouse，0 credits |
| `scrape_url(url)` | 单 URL 抓取，warehouse-first |
| `crawl_site(url, dry_run=true)` | 全站采集可行性/成本预估，默认不入队 |

兼容/扩展工具：

- 商品搜索、商品详情、促销查询。
- Amazon VOC。
- Reddit top contributors / user activity / playbook。
- 红人发现。
- 通用数据集 `crawl_custom_source` / `query_dataset`。

## 8. 前端产品形态

### 8.1 客户前台 `/app`

页面包括：

- 总览
- 报告
- 标杆维护
- 问答
- 商品库
- 覆盖率
- 任务
- 按需抓取
- 红人
- 设置
- 账号

### 8.2 超管后台 `/admin`

页面包括：

- 概览
- 租户用户
- 通用数据集
- spine 队列
- 数据质量
- 计费
- 代理池
- 健康
- 审计

## 9. 分布式 mini-worker 方案

### 9.1 目标

让 NAS 做任务调度、数据存储与产品入口；让 Mac mini 做抓取执行。

目标收益：

- 降低 NAS CPU/内存压力。
- 使用 mini 自身出口 IP、macOS 原生环境、真 Chrome/Playwright，提高难站成功率。
- 后续可以水平增加多台 mini。
- 保持数据统一写回 NAS PostgreSQL，不引入新的数据同步链路。

### 9.2 架构

```text
NAS
  - PostgreSQL
  - FastAPI / MCP / frontend / admin
  - scheduler
  - optional NAS workers
        ^
        |
        | Tailscale PostgreSQL connection
        |
Mac mini workers
  - launchd
  - python -m app.worker
  - WORKER_ID=US-macmini1-1
  - NODE_ID=US-macmini1
```

第一批节点：

| 节点 | SSH | NODE_ID | worker id 示例 |
|---|---|---|---|
| mini1 | `solvea@100.75.94.90` | `US-macmini1` | `US-macmini1-1` |
| mini4 | `solvea@100.72.33.57` | `US-macmini4` | `US-macmini4-1` |

### 9.3 mini 运行方式

mini 目录结构：

```text
~/smart-crawler/
  backend/
  scripts/
  deploy/
  logs/
  .venv/
```

安装：

```bash
brew install python@3.12
python3.12 -m venv ~/smart-crawler/.venv
source ~/smart-crawler/.venv/bin/activate
pip install -r backend/requirements.txt
python -m playwright install chromium
```

launchd env 示例：

```bash
RUN_SCHEDULER=0
DATABASE_URL=postgresql+psycopg://sc_worker:<password>@<nas-tailscale-ip>:5432/smart_crawler
WORKER_ID=US-macmini1-1
NODE_ID=US-macmini1
WORKER_ASSIGNED_ONLY=1
WORKSPACE_ALLOWLIST=<target_workspace_id>
SNAPSHOT_ENABLED=0
PROXY_LEASE_TTL_SEC=300
LLM_BASE_URL=https://api.flatkey.ai
LLM_MODEL=claude-haiku-4-5
```

关键点：

- `RUN_SCHEDULER=0`：mini 不调度任务。
- `WORKER_ASSIGNED_ONLY=1`：只领取分配给自己节点的任务。
- `NODE_ID`：代理健康与节点归因使用。
- `SNAPSHOT_ENABLED=0`：mini 不保存本地快照，防数据分散。
- `DATABASE_URL` 指向 NAS PostgreSQL。

### 9.4 任务分配策略

当前系统的 `crawl_jobs` 表天然支持多 worker 抢任务。分布式后 NAS worker 和 mini worker 共享同一队列，靠 DB 原子更新避免重复领取。

灰度策略：

```text
目标租户 xiaokang:
  mini1 / mini4:
    WORKSPACE_ALLOWLIST=<xiaokang_workspace_id>

  NAS workers:
    WORKSPACE_BLOCKLIST=<xiaokang_workspace_id>
```

效果：

- xiaokang 租户任务只由 mini 执行。
- 其他租户继续由 NAS worker 执行。
- 通过 `crawl_jobs.worker` 可以直接归因执行节点。
- 回退时去掉 NAS worker 的 blocklist，NAS 立即接管。

### 9.5 代理防撞设计

分布式核心风险是多 worker 撞同一个出口 IP。

解决方案：

```text
proxy_leases:
  全局租约，跨 NAS/mini 共享，用于并发互斥。

proxy_health:
  按 (proxy_hash, node) 记录健康状态，用于节点级隔离。
```

出口模式：

| 需求 | 配置 | 机制 |
|---|---|---|
| 住宅代理 | `proxy_tier=residential` | DB 代理池 + lease |
| 数据中心代理 | `proxy_tier=datacenter` | DB 代理池 + lease |
| mini 自身 IP | `proxy_tier=none` | 直连，不占代理租约 |

mini 上必须避免：

- 不下发 `proxies.txt`。
- 不设置 `RESIDENTIAL_PROXY`。
- 不设置 `DATACENTER_PROXY`。
- 不设置 `PROXIES_FILE`。

原因：文件/env 代理可能绕过 DB 租约，造成多节点撞 IP。

### 9.6 PostgreSQL 连接与安全

NAS PostgreSQL 建议只暴露到 Tailscale：

```yaml
postgres:
  ports:
    - "<nas-tailscale-ip>:5432:5432"
```

安全要求：

- 只绑定 Tailscale IP，不绑定 `0.0.0.0`。
- `pg_hba.conf` 只允许 `100.64.0.0/10` 或更窄的 mini IP。
- Tailscale ACL 限定 mini 节点访问 NAS `5432`。
- 给 mini worker 建独立 PG 用户，如 `sc_worker`。
- mini worker 不使用 PostgreSQL superuser。

### 9.7 故障与回退

| 故障 | 处理 |
|---|---|
| mini 单进程挂掉 | launchd 自动拉起 |
| mini 整机挂掉 | job heartbeat 停止后 stale reclaim |
| 某代理在某节点不可用 | 只更新该 `NODE_ID` 下的 proxy_health |
| 两台 mini 都不可用 | 去掉 NAS `WORKSPACE_BLOCKLIST`，NAS worker 接管 |
| 单任务超时 | worker runtime timeout 标 failed |
| 强反爬失败 | 记录 crawl_failures，后台数据质量页诊断 |

## 10. NAS 存储方案

### 10.1 设计原则

NAS 是系统唯一可信存储中心。

原则：

- 结构化数据统一在 NAS PostgreSQL。
- 原始快照统一在 NAS 磁盘。
- 导出物统一在 NAS 磁盘。
- mini 只作为执行器，不保存关键业务数据。
- 所有备份从 NAS 发起。

### 10.2 推荐目录

生产建议使用绝对目录：

```text
/volume1/smart-crawler/
  runtime/
    docker-compose.yml
    .env
  data/
    pgdata/
    snapshots/
    exports/
    logs/
    backups/
    tmp/
  secrets/
    proxies.txt
    pg_worker_password
    tunnel_token
  deliverables/
```

当前 compose 可调整为：

```yaml
postgres:
  volumes:
    - /volume1/smart-crawler/data/pgdata:/var/lib/postgresql/data

smart-crawler:
  volumes:
    - /volume1/smart-crawler/data:/app/data
    - /volume1/smart-crawler/deliverables:/app/deliverables
```

### 10.3 数据分层

PostgreSQL 保存：

- 商品、价格、促销、趋势、评论。
- 任务队列、URL frontier、失败诊断。
- 租户、用户、session、API key。
- 代理池、代理租约、代理健康。
- usage、rate limit、agent cache。
- SPINE datasets、records、jobs。
- 审计、webhook。

磁盘保存：

- `data/snapshots/`：原始 HTML/JSON gzip。
- `data/exports/`：Excel/CSV/report 导出。
- `data/logs/`：运行与采集日志。
- `data/backups/`：pg_dump 与部署快照。
- `deliverables/`：静态交付物。

### 10.4 快照策略

原始快照用途：

- 采集错误回溯。
- 采集器升级后重解析。
- 数据纠纷时审计。
- 不重复请求目标站点即可回放解析。

建议：

- NAS 保存快照。
- mini 默认 `SNAPSHOT_ENABLED=0`。
- 如后续要保留 mini 原始正文，应上传回 NAS 或写 NAS 共享路径，不建议散落在 mini 本地。

### 10.5 备份方案

当前已有脚本：

- `scripts/deploy/preflight.sh`
- `scripts/deploy/backup.sh`
- `scripts/deploy/restore.sh`
- `scripts/deploy/guarded_deploy.sh`
- `scripts/deploy/post_deploy_verify.sh`

备份内容：

- PostgreSQL `pg_dump --format=custom`。
- SQLite 本地模式 `.db` 备份。
- `.env`。
- compose 文件。
- `backend/sites.yaml`。
- `backend/proxies.txt`。
- git commit / git status。
- 迁移前关键表快照。

生产备份命令建议：

```bash
BACKUP_ROOT=/volume1/smart-crawler/data/backups/deploy scripts/deploy/backup.sh
```

恢复：

```bash
CONFIRM_RESTORE=YES scripts/deploy/restore.sh /volume1/smart-crawler/data/backups/deploy/<timestamp>
```

### 10.6 留存建议

| 数据 | 建议留存 |
|---|---|
| PostgreSQL 每日备份 | 30 天 |
| PostgreSQL 周/月归档 | 6-12 个月或按合同 |
| raw snapshots | 默认 180 天 |
| exports | 长期保留 |
| deliverables | 长期保留 |
| logs | 14-30 天 |
| usage/audit | 至少 12 个月 |

建议 NAS 本地备份之外，再同步一份到外部盘或对象存储，避免单 NAS 故障。

## 11. 安全与运维要求

生产环境必须设置：

```bash
POSTGRES_PASSWORD=<strong-random>
SC_SECRET=<strong-random>
ADMIN_USERNAME=<admin>
ADMIN_PASSWORD=<strong-random>
TUNNEL_TOKEN=<cloudflare-token>
```

要求：

- 禁止使用 `change-me` / `changeme` 等默认值。
- 真实 API key、proxy 密码、NAS 密码不能入仓库。
- 生产代理放 `secrets/` 或 NAS 私有路径。
- 外部 API key 默认只给 `crawler:read`、`crawler:scrape`。
- `crawler:crawl` 只给明确购买或内部授权的 key。
- `admin:*` 只给内部后台/超级管理员。

上线流程建议：

```text
preflight
  -> backup
  -> docker compose up -d --build
  -> migration guard
  -> post deploy verify
  -> manual smoke check
```

## 12. 已知风险与审查重点

建议技术负责人重点审查以下点：

1. PostgreSQL 对 mini 暴露的网络安全边界是否足够，包括 Tailscale ACL、PG 用户权限、pg_hba。
2. `proxy_leases` 是否覆盖所有高风险代理使用路径，尤其是直接调用 `get_proxy` 的 crawler。
3. mini 上禁用文件/env 代理是否能被部署脚本强制保证。
4. `WORKSPACE_ALLOWLIST` / `WORKSPACE_BLOCKLIST` 对 scheduled job 的归属判断是否覆盖完整。
5. `SNAPSHOT_ENABLED=0` 后，mini 执行任务是否会影响 raw snapshot 审计需求。
6. NAS 存储路径从相对目录迁移到 `/volume1/smart-crawler/` 时，权限与备份脚本是否同步调整。
7. PostgreSQL 连接池在多 NAS worker + 多 mini worker 下的上限是否需要调优。
8. 强反爬站点 Playwright/Chrome 依赖在 mini 原生环境下是否一致可复现。
9. `pg_dump` 备份是否足以覆盖 raw snapshots 和 deliverables；磁盘文件需要额外 rsync/snapshot。
10. 恢复演练是否做过，包括 PostgreSQL restore、compose 重启、登录、MCP tools/list、任务重跑。

## 13. 建议下一步

1. 确认生产 NAS 绝对目录方案：`/volume1/smart-crawler/...`。
2. 调整 compose volume 到 NAS 绝对路径。
3. 为 mini worker 创建最小权限 PG 用户。
4. 在 Tailscale 内只暴露 PostgreSQL 给 mini 节点。
5. 先让 xiaokang 租户灰度跑 mini1/mini4。
6. 对比 NAS worker 与 mini worker 的成功率、耗时、失败类型、代理健康。
7. 做一次完整备份和恢复演练。
8. 再决定是否扩大到更多租户和更多 mini。

