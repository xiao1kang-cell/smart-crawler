# 分布式抓取：NAS 调度/存储 + Mac mini 执行 — 设计文档

- 日期：2026-06-23
- 状态：待审阅
- 作者：wangxiaokang（与 Claude 协作）

## 1. 背景与目标

当前 smart-crawler 在 NAS（Synology，Tailscale `100.116.163.64`）上以
`docker compose` 跑全套：`postgres` + `smart-crawler`（FastAPI + scheduler）+
多个 `worker_N` 抓取容器 + `cloudflared`。worker 与 PG 在同一容器网络内。

**目标**：让 NAS 只做**任务调度 + 数据存储**，把**抓取执行**下沉到
Mac mini（第一批 `mini1` + `mini4`），后续扩展到 N 台。

> **mini1 实况（2026-06-23 探测）**：M4（T8132）/ 16GB RAM / 10 核 / arm64 /
> macOS 26.2；SSH 免密直连免跳板；默认 `python3` 为 **3.14.3，无 3.12**
> （装机必须 `brew install python@3.12`，与生产 Dockerfile 一致，避免 3.14
> 的 C 扩展轮子兼容问题）；有**两个 Tailscale 接口** `utun0=100.75.94.90`
> （SSH 走此）与 `utun5=100.95.220.89`（tailscale CLI 自报）——故 pg_hba
> 必须放行**整段 `100.64.0.0/10`** 而非单 IP。NAS PG 当前不可达（阶段0前
> 未暴露，符合预期）。
>
> **mini4 实况（2026-06-24 探测）**：SSH `solvea@100.72.33.57` 免密直连；
> hostname `mini4.local`；arm64；macOS 26.3.1。节点标识使用 `US-macmini4`。

**关键约束（用户确认）**：
1. 出口 IP 三种都要、按任务选：住宅代理 / 数据中心代理 / mini 自身 IP。
2. mini 上 worker 以**原生 launchd 进程**运行（用 macOS 真 Chrome，指纹优势）。
3. 代码/配置用 **rsync/scp 推送**到 mini（mini 在 Tailscale 网段，直连免跳板）。
4. 任务分配**不分地域、任意抢**（claim_job 现有逻辑，无 region 亲和）。
5. **多 mini 不能取到相同出口 IP**（分布式互斥）。
6. **代理健康度按节点隔离**：同一住宅 IP 在 NAS 用不了但 mini1 能用，节点间
   健康判定不能互相传染。
7. **NAS 已入队任务正常采集不能中断**（其余租户零影响）。
8. **第一阶段只针对指定租户用 mini 抓取验证**，策略 B：该租户**只**走 mini。

## 2. 现状分析（代码事实）

- **worker 本就无状态**：`worker.py` 轮询 PG `crawl_jobs` →
  `runner.claim_job()` 乐观锁原子领取 → `runner.execute_job()` 跑 crawler →
  商品/促销/诊断直接写回 PG → `recompute` 更新大盘。多 worker 靠同一
  `DATABASE_URL` 抢同一队列。**把 worker 搬到 mini = 让它连 NAS 的 PG，
  架构不需要重写。**
- **数据天然回流**：结果直接写 NAS PG，FastAPI 读 PG 出报表，**无需把数据从
  mini 搬回**。
- **代理租约已有跨进程互斥**：`proxy_pool._try_create_proxy_lease()`
  （`proxy_pool.py:589`）对每个 `ProxyEndpoint` 用 `with_for_update()` 行锁，
  统计未释放的活跃 `ProxyLease`，`active_count >= endpoint.max_concurrency`
  就跳过。**只要 PG 共享，这套天然是分布式互斥。**
- **代理可走 DB**：`ProxyPool(prefer_db=True)` 已能从 `proxy_endpoints` 表读
  代理；文件 `proxies.txt` / env `RESIDENTIAL_PROXY` 是兜底。
- **唯一本地文件耦合**：`snapshot.save()` 写 `data/snapshots/*.gz`
  （`snapshot.py`，原始响应归档，失败静默、非关键路径）。
- **租户字段已存在**：`CrawlJob.requested_by_workspace_id`（`models.py:438`）
  记录触发租户；`workspace_sites` 表（`models.py:231`）记录 site→workspace。
- **健康设施已有**：`crawl_jobs.worker` / `heartbeat_at` 字段、`/admin/health`
  端点、worker 自带 `_reclaim_stale_crawl_jobs` 心跳兜底。

### 已识别的差距（必须修复，否则会撞 IP / 路由不全）

| # | 差距 | 位置 | 后果 |
|---|------|------|------|
| D1 | 文件/env 代理无租约，`db_candidates` 为空直接返回 `candidates[0]` | `proxy_pool.py:601` | 多 mini 用文件/env 代理各取第一个 → 必撞 |
| D2 | lease（带并发锁）仅当 `proxy_lease_ttl_sec > 0` 才启用，否则走 `get_proxy()` 无锁 | `fetching.py:432-446` | 默认路径无跨进程并发控制 → 多 mini 撞 IP |
| D3 | scheduled/daily_delta/daily_refresh 入队不传 `requested_by_workspace_id` | `scheduler.py:41`、`daily_delta.py:74/115` | 定时 job 租户字段为 NULL，单按该字段路由覆盖不到定时任务 |
| D4 | **代理健康度全局共享**：`proxy_health` 唯一键仅 `proxy_hash`，无节点维度 | `models.py ProxyHealth`、`proxy_health.py`、`proxy_pool.py:520` | 住宅 IP 可用性因出口节点而异（NAS 用不了但 mini1 能用）；NAS 标 `down` 会连带禁用 mini1 上明明可用的 IP（假阴性传染），反之亦然（假阳性传染）|

## 3. 整体架构

### 角色划分

- **NAS（控制面 + 数据面）**：`postgres`、`smart-crawler`（FastAPI + scheduler）、
  前端、`cloudflared`。**第一阶段 NAS worker 照常运行不停**（兜底其余租户）。
- **Mac mini（执行面）**：launchd 守护 N 个 `python -m app.worker` 原生进程，
  连回 NAS PG 抢任务、执行、写回。

### 连接通道（全部走 Tailscale 100.x 网段）

```
mini worker ──(Tailscale)──► NAS PostgreSQL :5432   领任务 / 写结果 / 心跳
mini worker ──(直连 or 代理)──► 目标站点            抓取（出口 IP 三选）
mini worker ──► LLM 网关（flatkey.ai）              清洗/解析
mini worker ──► 代理供应商                            residential / datacenter tier
```

### 数据流

scheduler/API 在 NAS 入队 → worker `claim_job`（按租户过滤）→ `execute_job`
跑 crawler → 结果直接写 NAS PG → `recompute` 更新大盘 → FastAPI 读 PG 出报表。

## 4. 多 mini 出口 IP 防撞机制

**基石（已存在）**：`proxy_leases` 表 + `with_for_update()` 行锁 +
`endpoint.max_concurrency` = 跨进程原子租约。共享 PG 即分布式互斥。

**必须的改动**：

1. **修复 D2 — 强制 lease 路径**：全局默认 `proxy_lease_ttl_sec > 0`
   （不再默认 0），使所有走代理的抓取都经过带并发锁的 `lease_proxy()`。
   提供 env 开关可临时回退到旧 `get_proxy()` 路径（回滚用）。
2. **修复 D1 — mini 禁用文件/env 代理**：mini 上**不下发** `proxies.txt`、
   **不设** `RESIDENTIAL_PROXY` / `DATACENTER_PROXY` / `PROXIES_FILE`。
   代理全部从 NAS PG `proxy_endpoints` 读 → 都有 `endpoint.id` → 都走租约。
3. **`max_concurrency` 旋钮**：住宅独享 IP 设 `1`（同一时刻仅一个 worker）；
   带轮换的住宅网关（一个 URL 背后是 IP 池）设 `N`。这是"几个 worker 能共用
   一个出口"的控制点。

**出口 IP 三选映射**（沿用现有 `Site.proxy_tier` + `_candidate_tiers_from_rules`）：

| 需求 | 配置 | 机制 |
|------|------|------|
| 住宅代理 | `proxy_tier=residential` | DB 租约 |
| 数据中心代理 | `proxy_tier=datacenter` | DB 租约 |
| mini 自身 IP | `proxy_tier=none`（直连） | lease 返回 None，不占代理；各 mini 出口本就不同，天然不撞 |

**残留风险（已知，记录在案）**：lease 是"抓取请求级"（TTL 数百秒），非"整个
job 级"。一个 job 内多次请求会多次租/还，对 `max_concurrency=1` 的住宅独享 IP
仍保证同一时刻只一个 holder，但不保证同一 job 全程同一 IP。若某反爬站需要
"整会话粘同一 IP"，用站点级 `proxy_lease_ttl_sec` 覆盖到 job 时长或设站点粘性。

**残留风险 2（D2 防撞覆盖边界，2026-06-24 代码审查发现）**：代理获取有两条
路径——① 经 `BaseCrawler.make_fetcher()` → `CrawlerFetcher`/`ProxyMiddleware`
的抓取请求，受 lease 并发锁保护（D2 覆盖）；② `BaseCrawler.__init__` 里
`self.proxy = get_proxy(...)`（base.py:51）以及少数 crawler（google_maps、
influencer 系列、ondemand/runner）**直接调用 `get_proxy`**，不走 lease、无并发
锁。**所以"默认走租约防撞"只覆盖经 ProxyMiddleware 的请求路径，不覆盖直接
get_proxy 的路径。** 第一阶段现在包含 mini1 + mini4 两台，因此启用前必须确认
验证租户的主流抓取路径经 `make_fetcher`/`ProxyMiddleware`，并且 mini 上不下发
`proxies.txt`、不设 `RESIDENTIAL_PROXY` / `DATACENTER_PROXY` / `PROXIES_FILE`。
`self.proxy` 主要用于 `ip_record` 日志与个别特殊 crawler；若要把这些直接
`get_proxy` 的热点 crawler 也放到多 mini 跑，需先改为走 lease，或给这些出口
纳入 `max_concurrency` 约束。

## 4.5 代理健康度按节点隔离（修复 D4，方案 A）

**问题本质**：住宅 IP 的可用性是 **(IP, 出口节点) 二元组的属性**，不是 IP 的
全局属性。同一住宅 IP，NAS 的网络环境用不了（IP 段被供应商限制等），mini1 的
美国家宽环境却能用。当前 `proxy_health` 把它建模成 IP 的全局属性 →
节点间健康判定互相传染（NAS 标 down 连带禁 mini1，反之亦然）。

> **重要边界**：只有**健康度/黑名单**这一层需要按节点隔离。`ProxyLease` 租约
> 表（防撞，第 4 节）**仍是、也应该是全局的**——"同一时刻一个 IP 只一个
> holder"是跨节点的物理约束，与节点无关。两层职责不同，不要混淆。

**方案 A：给健康度加 node 维度。**

1. **schema 变更**：`ProxyHealth` 加 `node = Column(String, index=True)`；
   唯一键从 `(proxy_hash)` 改为 `(proxy_hash, node)`。
   迁移：现有行回填 `node='nas'`（历史健康数据归属 NAS）。
2. **写入带 node**：`record_proxy_result(..., node=...)` 按
   `(proxy_hash, node)` upsert，每节点只更新自己那行。
   - 调用点：`fetching.py:476`、`proxy_probe.py:149`。
3. **读取带 node**：`unhealthy_proxy_hashes(session, node=...)` 只返回**该节点**
   判定为坏的 IP；`_persistent_unhealthy_hashes()` 透传当前节点。
   - 调用点：`proxy_pool.py:296/355/512`。
4. **node 取值**：进程级常量 `NODE_ID`（env），**无需逐 job 透传**。
   - NAS worker：`NODE_ID=nas`
   - mini1：`NODE_ID=US-macmini1`（或由 `WORKER_ID` 推导机器部分，去掉 `-1/-2` 序号）
   - mini4：`NODE_ID=US-macmini4`
   - 同一台 mini 的多个 worker 进程共享同一 `NODE_ID` → 它们的健康判定互通
     （同机网络环境相同，合理），但与其他节点隔离。

**效果**：每个节点维护自己的健康视角。IP 在 mini1 能用就在 mini1 用，在 NAS
坏了只在 NAS 拉黑，互不传染。契合后续 N 台 mini 扩容（各自独立视角）。

**可选增强（方案 C，不阻塞）**：未来若 `proxy_auth_failed`（凭证作废，全局
问题）误判频繁，可让这类失败仍写全局行（`node=NULL` 视为对所有节点生效），
网络层失败按节点隔离。当前 auth 失败少，暂不实现。

## 5. 不中断 NAS 采集 + 第一阶段按租户路由（策略 B）

### 5.1 不中断保证

- 第一阶段 **NAS worker 完全照常运行，一个都不停**。
- `claim_job` 是 PG 乐观锁原子领取（`runner.py:243`），NAS 与 mini 并发抢同一
  队列**天然安全**——不重复执行、不丢任务。
- 其余租户由 NAS 全量兜底，零影响。

### 5.2 按租户路由（claim_job 改动）

`claim_job` 增加两个可选过滤参数（env 驱动，向后兼容）：

```python
def claim_job(worker_id, trigger_allowlist=None,
              workspace_allowlist=None,    # mini 用：只领这些租户的 job
              workspace_blocklist=None):   # NAS 用：不领这些租户的 job
```

**租户判定（修复 D3，按优先级）**：
1. `job.requested_by_workspace_id` 命中 allowlist/blocklist → 据此判定
   （覆盖 manual / tracking_add）。
2. 为 NULL（scheduled 等）时，查 `workspace_sites`：`job.site` 是否属于目标
   租户的站点 → 据此判定（覆盖定时任务）。

实现注意：在 `claim_job` 的 SQL 过滤里以 `requested_by_workspace_id IN (...)`
为主，对 NULL 行用 `job.site IN (SELECT site FROM workspace_sites WHERE
workspace_id IN (...))` 子查询补判。两路合并为一个 allowlist / blocklist 谓词。

### 5.3 策略 B 配置（用户选定）

验证租户 = **`xiaokang`**（`X` = 其 workspace_id，部署时查 PG 解析，见第 13 节）。

| 节点 | 配置 | 效果 |
|------|------|------|
| mini1 | `WORKSPACE_ALLOWLIST=X` | 只领 xiaokang 租户的 job |
| mini4 | `WORKSPACE_ALLOWLIST=X` | 只领 xiaokang 租户的 job |
| NAS worker | `WORKSPACE_BLOCKLIST=X` | 不领 xiaokang 租户的 job |

- 租户 xiaokang 100% 由 mini 跑，`crawl_jobs.worker` 全为 `US-macmini1-*` 或
  `US-macmini4-*`，归因无歧义。
- 其余租户由 NAS 全量兜底，满足"整体不中断"。
- **代价**：验证期 xiaokang 的可用性依赖 mini1/mini4；两台都挂 → xiaokang 暂停抓。
  - **回退**：去掉 NAS `WORKSPACE_BLOCKLIST=X` 并重启 NAS worker，NAS 立即重新
    接管 xiaokang（pending job 仍在 PG，乐观锁领取，不丢）。一条命令完成。

## 6. NAS 侧改动

### 6.1 PG 暴露到 Tailscale（双层防护）

`docker-compose.yml` 把 postgres 端口**只绑 Tailscale IP**，不绑 `0.0.0.0`：

```yaml
postgres:
  ports:
    - "100.116.163.64:5432:5432"   # 仅 Tailscale 网段可达，不开公网
```

配合：
- `pg_hba.conf` 仅允许 `100.64.0.0/10`（Tailscale CGNAT 段）+ `scram-sha-256`。
- Tailscale ACL 限定只有 mini 节点可访问 NAS:5432。
- 给 worker 建**专用 PG 角色**（非 superuser），仅授 crawl 相关表 DML 权限。

### 6.2 worker 容器策略

第一阶段：NAS worker 容器保留并加 `WORKSPACE_BLOCKLIST=X`（其余照常）。
最终阶段（验证通过后）：逐步缩减 NAS worker（`WORKER_THREADS=0` / 停容器），
保留"一键重启 NAS worker"作为 mini 全挂兜底。

## 7. mini 装机与部署

### 7.1 装机脚本 `scripts/mini_bootstrap.sh`（每台 mini 跑一次）

```bash
brew install python@3.12
mkdir -p ~/smart-crawler && cd ~/smart-crawler
python3.12 -m venv .venv && source .venv/bin/activate
# 代码由 rsync 推来后：
pip install -r backend/requirements.txt
playwright install chromium        # 或配 channel="chrome" 用系统 Chrome
# 运行 scripts/install_mini_launchd.sh：先安装 env 草稿 + plist，不 load
```

依赖来自 `backend/requirements.txt`（含 playwright、patchright、scrapling、
psycopg[binary]）。Python 3.12（与 Dockerfile 一致）。

### 7.2 launchd 守护 `~/Library/LaunchAgents/io.smartcrawler.worker-N.plist`

每台 mini 起 1~N 个 worker（`WORKER_ID=US-macmini1-1`、`-2`…）：

```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/solvea/smart-crawler/.venv/bin/python</string>
  <string>-m</string><string>app.worker</string>
</array>
<key>WorkingDirectory</key><string>/Users/solvea/smart-crawler/backend</string>
<key>EnvironmentVariables</key><dict>SC_ENV_FILE=/Users/solvea/.smart-crawler-N.env</dict>
<key>KeepAlive</key><true/>        <!-- 崩溃自动拉起 = restart:unless-stopped -->
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>…/logs/worker-N.out.log</string>
<key>StandardErrorPath</key><string>…/logs/worker-N.err.log</string>
```

mini1 的 macOS 26.2 + Homebrew Python 3.12 需显式使用 Homebrew `expat`，
plist 与 env 都设置 `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`。

### 7.3 rsync 部署脚本 `scripts/deploy_mini.sh`（本机/NAS 跑）

```bash
MINIS=("solvea@100.75.94.90" "solvea@100.72.33.57")
for MINI in "${MINIS[@]}"; do
  rsync -az --delete \
    --exclude='.venv' --exclude='data' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.env' --exclude='proxies*.txt' \
    backend/ "$MINI":~/smart-crawler/backend/
  ssh "$MINI" 'cd ~/smart-crawler && source .venv/bin/activate && \
    pip install -q -r backend/requirements.txt && \
    for n in 1 2; do launchctl kickstart -k gui/$(id -u)/io.smartcrawler.worker-$n 2>/dev/null || true; done'
done
```

与现有 NAS 部署 scp 风格一致；mini 在 Tailscale 网段直连，免跳板。密钥/代理
文件用 `--exclude` 排除，不进 rsync。每台 `NODE_ID` 不同，`WORKER_ID` 也必须唯一。

### 7.4 mini 环境变量 `~/.smart-crawler-N.env`（权限 600）

```bash
DATABASE_URL=postgresql+psycopg://<worker_role>:***@100.116.163.64:5432/smart_crawler
RUN_SCHEDULER=0                   # mini 绝不跑 scheduler
WORKER_ID=US-macmini1-1           # worker 2 用 US-macmini1-2；mini4 用 US-macmini4-1/-2
NODE_ID=US-macmini1               # mini4 用 US-macmini4；同台多 worker 共用此值
WORKSPACE_ALLOWLIST=<X>           # 第一阶段：只领验证租户 X
# 不设 RESIDENTIAL_PROXY / DATACENTER_PROXY / PROXIES_FILE → 全走 DB 租约
SNAPSHOT_ENABLED=0               # 第一阶段关闭（见 7.5）
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
LLM_BASE_URL=https://api.flatkey.ai
LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=***
# 各站 LIMIT 同 NAS（沿用现有 env）
```

### 7.5 snapshot 处置

`snapshot.save` 写 `data/snapshots/*.gz`（非关键，失败静默）。**第一阶段
mini 默认关闭** `SNAPSHOT_ENABLED=0`，不影响抓取与数据入库。
注意：`spine.py` 的 `save_returning_path` 把路径写进 DB `body_path`；若 spine
任务搬到 mini 且需回溯，该路径在 NAS 不存在。**第一阶段 mini 只跑电商
crawl_jobs，spine 任务留 NAS。** 后续如需保留 snapshot，可改落共享存储
（NAS NFS 挂载），属优化项，不阻塞上线。

## 8. 灰度上线（每步可验证、可回退）

```
阶段0  NAS：PG 暴露 Tailscale + worker 专用角色。NAS worker 照常。零行为变更。
       验证：mini 上 psql 能连 NAS PG、能 SELECT crawl_jobs。
阶段1  代码：修复 D2（强制 lease）+ D4（健康按节点隔离）+ claim_job 加 workspace
       过滤参数。跑 DB 迁移（ProxyHealth 加 node 列、回填 nas）。NAS 设
       NODE_ID=nas。NAS 上全量跑测试 + 观察现有抓取无回归。NAS worker 照常。
阶段2  mini1 + mini4 装机，各起 1 worker，WORKSPACE_ALLOWLIST=X、
       NODE_ID=US-macmini1/US-macmini4；
       NAS 加 WORKSPACE_BLOCKLIST=X。手动触发租户 X 某站 → 看 job 被
       US-macmini1-1 或 US-macmini4-1 领、数据回 NAS PG、proxy_leases 无撞车、
       proxy_health 出现 node=US-macmini1/US-macmini4 的独立行（不与 nas 行互相污染）。
       其余租户 NAS 照抓不误。
阶段3  放开两台 mini 接租户 X 的 scheduled（靠 workspace_sites 映射），观察 24h：
       数据完整性、429 率、proxy_leases 活跃数 ≤ max_concurrency、
       验证"NAS 标坏的 IP 在 mini1/mini4 上仍可用"（D4 隔离生效）。
阶段4  验证通过 → 扩大 WORKSPACE_ALLOWLIST 或去掉过滤（任意抢），
       再逐步缩减 NAS worker。此步才动 NAS，可随时回退。
```

## 9. 回滚

- **mini 出问题** → `launchctl unload` 停对应 mini worker；如果两台都异常，去掉 NAS
  `WORKSPACE_BLOCKLIST=X` 并重启 NAS worker，NAS 立即接管。数据在 PG，不丢。
- **代码改动出问题**（强制 lease）→ git revert + 重新 rsync；或临时
  `proxy_lease_ttl_sec=0` env 退回旧路径。
- **健康隔离（D4）出问题** → 迁移可向后兼容：`unhealthy_proxy_hashes` 的 node
  参数缺省时回退到"全节点合并"行为；或临时让所有节点 `NODE_ID=nas` 退回全局
  共享语义。schema 的 `node` 列对旧代码无害（旧查询忽略该列）。
- **PG 暴露出问题** → 注释 compose `ports`，PG 退回容器内网。
- **彻底回滚** → 即现状"NAS 多 worker 容器"模式。

## 10. 监控（复用现有 + 补分布式视角）

- 复用：`crawl_jobs.worker` / `heartbeat_at`、`/admin/health`、worker
  `_reclaim_stale_crawl_jobs` 心跳兜底（stale 自动 fail 重排）。
- 新增轻量：
  1. admin 按 `crawl_jobs.worker` 分组的"近 N 分钟有心跳的 worker 列表"，
     一眼看 `US-macmini1-*` 是否在干活。
  2. proxy_leases 并发面板：每 endpoint 活跃租约数 vs max_concurrency，
     验证防撞生效。
  3. mini 本地：launchd `KeepAlive` 自动拉起；`worker-N.err.log` 落盘。
- **断网容错**：mini↔NAS Tailscale 断 → worker 连 PG 失败 → `claim_job`
  异常被现有 try/except 捕获、sleep 重试（`worker.py:280`），网络恢复自动续抓。
  NAS 侧 stale reclaim 回收断网期间卡住的 running job 重排。

## 11. 测试

- **回归**：改 lease 默认值后，`backend/tests/` 全量跑通（无回归）。
- **新增单测**：
  - 并发两次 lease 同 endpoint（max_concurrency=1）→ 第二次拿不到（锁死防撞）。
  - `claim_job` 的 workspace allowlist/blocklist 过滤，含 scheduled NULL 经
    `workspace_sites` 映射命中的用例。
  - D4 健康隔离：node=nas 标某 IP 为 down 后，`unhealthy_proxy_hashes(node='US-macmini1')`
    不包含该 IP；`record_proxy_result` 对同一 IP 不同 node 写出两行独立状态。
- **端到端冒烟**：阶段2 的"mini 抓租户 X 一个站 → 数据回 NAS"即真实 e2e。

## 12. 改动清单（代码层面）

| 文件 | 改动 |
|------|------|
| `backend/app/fetching.py` | 修 D2：全局默认 `proxy_lease_ttl_sec > 0`，提供回退开关；修 D4：`record_proxy_result` 传 `node=NODE_ID`（`:476`）|
| `backend/app/runner.py` | `claim_job` 加 `workspace_allowlist` / `workspace_blocklist` 参数 + `workspace_sites` 子查询判定 |
| `backend/app/worker.py` | 读 `WORKSPACE_ALLOWLIST` / `WORKSPACE_BLOCKLIST` env 传入 claim_job |
| `backend/app/models.py` | 修 D4：`ProxyHealth` 加 `node` 列，唯一键改 `(proxy_hash, node)` |
| `backend/app/proxy_health.py` | 修 D4：`record_proxy_result` / `unhealthy_proxy_hashes` 加 `node` 参数，按 `(proxy_hash, node)` upsert/查询 |
| `backend/app/proxy_pool.py` | 修 D4：`_persistent_unhealthy_hashes()` 透传 `NODE_ID`（`:296/355/512`）|
| `backend/app/proxy_probe.py` | 修 D4：`record_proxy_result` 传 `node`（`:149`）|
| DB 迁移 | `ProxyHealth` 现有行回填 `node='nas'`，重建唯一约束 |
| `docker-compose.yml` | postgres `ports` 绑 Tailscale IP；NAS worker 加 `WORKSPACE_BLOCKLIST` + `NODE_ID=nas` |
| `scripts/mini_bootstrap.sh` | 新增：mini 装机 |
| `scripts/deploy_mini.sh` | 新增：rsync 部署 + 重启 launchd |
| `deploy/io.smartcrawler.worker.plist` | 新增：launchd 模板 |
| PG `pg_hba.conf` + worker 专用角色 | 运维侧配置 |
| admin UI | 可选：worker 在线视图、proxy_leases 并发面板、proxy_health 按 node 分组视图 |

## 13. 待定项（上线前确认）

- ~~mini SSH 用户名~~：已确认 `solvea@100.75.94.90`（mini1，免密直连）、
  `solvea@100.72.33.57`（mini4，免密直连）。
- 每台 mini worker 进程数：默认 `2`（16GB/10核，跑 2 worker + Chrome 合理）。
- snapshot：第一阶段关闭（`SNAPSHOT_ENABLED=0`）。
- 验证租户：已确认 **`xiaokang`** 租户用 mini 抓。其 `workspace_id` 在部署时
  从 NAS PG 解析（一行查询）：
  ```sql
  SELECT id, name, slug FROM workspaces WHERE name='xiaokang' OR slug='xiaokang';
  ```
  把得到的 id 填入 mini 的 `WORKSPACE_ALLOWLIST` 与 NAS worker 的
  `WORKSPACE_BLOCKLIST`。
- 各 `proxy_endpoints.max_concurrency` 的具体值（住宅独享=1，网关=N）。
