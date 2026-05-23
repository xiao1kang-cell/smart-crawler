# smart-crawler 交接文档

> 上次大修：**2026-05-19** —— PG 迁移、reviews 扩量、空站收敛、Google Shopping 双路。
> 之前里程碑：2026-05-17 客户交付暂停点。

## 一句话

为 AI Agent 打造的跨境电商竞品数据采集引擎：46 个独立站 + 多评论渠道，
数据经 MCP / REST 暴露给 Agent。生产部署在 NAS，域名 smartcrawler.io。

## 线上状态（2026-05-19）

| 项 | 状态 |
|---|---|
| 生产部署 | NAS（192.168.1.80, hostname=vocserver）Docker 容器 `smart-crawler:8077` |
| 公网 | `https://smartcrawler.io`（Cloudflare Tunnel） |
| 数据库 | **PostgreSQL 16-alpine**（容器 `smart-crawler-pg`，DB size 192MB） |
| 数据量 | 商品 **62,926** / 促销 **37,591** / 评论 **8,602** / 价格历史 343,456 |
| 评论分布 | trustpilot 1,346 + reviews_io 1,672 + google_map 5,584 |
| 站点覆盖 | **44 / 46 站有数据**（空站 2 个：vidaxl_us / vidaxl_ca，均等客户 Dropshipping 凭据） |
| Shopping | 15 关键词 / 522 results（双路：Google stealth + Bing 兜底） |
| MCP | `smartcrawler.io/mcp`，9 个工具，streamable-http，Bearer API Key 鉴权 |
| 调度 | 容器内 APScheduler：商品日更 02:00 / 评论·Shopping 周更 |
| 容器资源 | smart-crawler 138MB mem / 0.3% CPU；pg 273MB；磁盘 15TB 用 42G (0.3%) |

## 凭据位置

**不在仓库里。** 真实密钥在 NAS 的 `/volume1/docker/smart-crawler/app/.env`
（本地开发副本在 `~/smart-crawler/.env`，已 gitignore）。包含：
管理员账号密码、SC_SECRET、`POSTGRES_PASSWORD`、`DATABASE_URL`、LLM 网关 key、VOC_API_KEY。
`.env.example` 是模板。

**SSH 入 NAS**：`ssh solvea@192.168.1.80`（直连，pubkey auth，**无需密码**也不需要跳板）。

## 在新机器上继续

```bash
git clone git@github.com:mguozhen/smart-crawler.git
cd smart-crawler
cp .env.example .env          # 填入真实值（问 Hunter 或从旧机 .env 拷）
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium
# 本地起服务
cd backend && uvicorn app.main:app --port 8077
```

部署到 NAS：打包 `backend/ frontend/ Dockerfile docker-compose.yml .env`
→ scp 到 `/volume1/docker/smart-crawler/app/` → `docker compose up -d --build`。

## 待办（按优先级）

### 高 — 业务核心

- **空站 vidaxl_ca**：根因已确认（2026-05-19 实测）= **业务暂停，非技术问题**。
  VidaXL 已关闭加拿大市场，页面显示 "We're pausing orders until further notice."，
  所有类别 0 商品，sitemap 为空。无任何爬取手段可绕过。等 VidaXL 重开加拿大站即可，
  `vidaxl.py` 代码无需改动，重开后 sitemap 会自动填充。
- **空站 vidaxl_us**：根因 = **全站 HTTP 401**（Salesforce Commerce Cloud B2B Auth 墙），
  连 `robots.txt` 都 401。目前唯一可行路径：
  (a) 住宅代理（`proxies.txt` 的 `[residential]` 段配好后取消注释），或
  (b) VidaXL Dropshipping API 凭据（`VIDAXL_API_EMAIL` + `VIDAXL_API_TOKEN`）。
  `vidaxl.py:_crawl_api` 代码已就绪，等凭据即可。
- ~~**修空站 costway_pl**~~：✅ 已通（2026-05-19）。根因：costway.pl 不是 Magento，
  而是 **Shoper**（波兰本土电商系统），无 sitemap，JSON-LD 把商品字段拆成多个 block
  按 @id 合并。新写 `app/crawlers/shoper.py`，类别页发现 + 同 @id block 字段合并。
  实测 50 商品 quick run 47 入库（94%），duration 129s。

### 中 — 评论扩量 / Shopping 验证

- **评论复用到其他品牌**：当前 reviews_io / google_map 都集中在 aosom_uk 一个 site。
  在 `review_channels.yaml` 给 Costway / Songmics / Homary / Yaheetech / Bcp /
  Vidaxl / Flexispot 加渠道，预计 reviews 总量再涨 5-10×。
- **Google Shopping 双路实战验证**：commit `79fa6f2` 重写为 scrapling+Bing 双路，
  容器内 scrapling 0.4.8 + curl_cffi 0.15.0 已装，**但未跑过**。
  跑 `GoogleShoppingCrawler('standing desk').crawl()` 验证。

### 低 — 长期

- **MCP 注册中心提交**：仓库转 public 后，按 `deliverables/mcp-registry-submission.md`
  走 `scripts/publish.sh`（publish-mcp 技能）+ mcp.so 等。
- **模块三**：商品属性提取（LLM）+ 看板评论 Tab。

## origin 分叉提醒

origin/main 上有 3 个 commit 是 Hunter 在另一台机器做的（`7b583f3` / `3fedaaa` / `fd0044e`），
本地 main 上有 3 个对应 commit（`6235bb1` 起）—— **两边在同一时间窗独立修了同一个 bug**
（review_runner site 多平台时只命中第一个）。NAS 上跑的是本地 main 版本（md5 对得上 `6235bb1`），
但 PG 中 reviews_io 1672 + google_map 5584 数据吻合 Hunter commit log 描述，说明
Hunter 的方案曾在另一处执行过。

**两边 review 修复功能等价、实现不同。合并方式待跟 Hunter 对一下，推荐 PR review**。
本地 main HEAD 备份在 `origin/local/may19-nas-snapshot` (HEAD = 79fa6f2)。

## 关键设计

- 采集器按 `site.platform` 注册（`backend/app/crawlers/registry.py`）：
  shopify / nuxt / vue_spa / generic / flexispot / vidaxl / vonhaus / **magento** /
  **google_shopping**。
  加站点改 `backend/sites.yaml`，`_seed_sites` 会同步平台变更到 DB。
- `magento` 采集器：robots.txt 发现 sitemap + 递归展开 + 并发抓取 + JSON-LD/OG
  判别商品。覆盖 Costway 欧洲站、VonHaus。
- `google_shopping` 采集器（2026-05-19 重写）：scrapling StealthyFetcher 走 Google
  Shopping + curl_cffi 拉 Bing Shopping，两路合并去重；Google 被 reCAPTCHA 拦时
  Bing 兜底。Bing 对爬虫宽容很多。
- `shopify` 采集器：products.json 不含 currency，按 `site.country` 推断。
- `homary` 采集器：智能价格解析，自适应欧式 `94,99 €` / 美式 `$94.99`。
- `shoper` 采集器（2026-05-19 新增）：处理 Shoper 平台（costway.pl）。无 sitemap，
  类别页发现路径。JSON-LD 字段拆在多个同 @id block 里，按 @id 合并字段。
  能识别 `@type: "http://schema.org/Product"` 完整 URL 形式。
- MCP 服务器 `app/mcp_server.py`（FastMCP），挂在 FastAPI `/mcp`；
  鉴权中间件在 `main.py`，校验 `Authorization: Bearer sck_...`。
- 发现层 `app/api/discovery.py`：`/llms.txt`、`/.well-known/*`、`/agents.json`。
- 反爬 `app/antiban.py`：熔断 + 限速档 + 站点冷却 + IP 配额。
- 注意：`apikey.hash_key` 依赖 `SC_SECRET`，轮换 SC_SECRET 会使所有
  已发 API Key 失效（需重新生成）。

## 数据备份

- PG 数据卷：`/volume1/docker/smart-crawler/app/data/pgdata/`（host）。
- 历史 SQLite 冷备（PG 迁移前快照）：
  `/volume1/docker/smart-crawler/app/data/smart_crawler_2026-05-17_pre-pg.db.gz` (23MB)。
- 采集快照：`data/snapshots/<site>/<date>/` (1.8GB，调试用，可滚动清理)。
