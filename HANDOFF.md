# smart-crawler 交接文档

> 项目于 **2026-05-17 暂停**，将在另一台机器继续维护。
> 本文档让接手者快速恢复上下文并继续。

## 一句话

为 AI Agent 打造的跨境电商竞品数据采集引擎：46 个独立站 + 评论渠道，
数据经 MCP / REST 暴露给 Agent。生产部署在 NAS，域名 smartcrawler.io。

## 线上状态（暂停时）

| 项 | 状态 |
|---|---|
| 生产部署 | NAS（192.168.1.80）Docker 容器 `smart-crawler:8077` |
| 公网 | `https://smartcrawler.io`（Cloudflare Tunnel） |
| 数据库 | **SQLite**（`data/smart_crawler.db`，容器卷持久化） |
| 数据量 | 商品 58,396 / 促销 ~36k / 评论 1,346（Trustpilot） |
| 站点覆盖 | **42 / 46 站有数据** |
| MCP | `smartcrawler.io/mcp`，9 个工具，streamable-http，Bearer API Key 鉴权 |
| 调度 | 容器内 APScheduler：商品日更 02:00 / 评论·Shopping 周更 |

## 凭据位置

**不在仓库里。** 真实密钥在 NAS 的 `/volume1/docker/smart-crawler/app/.env`
（本地开发副本在 `~/smart-crawler/.env`，已 gitignore）。包含：
管理员账号密码、SC_SECRET、PostgreSQL 密码、LLM 网关 key、VOC_API_KEY。
`.env.example` 是模板。

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
SSH 跳板：iMac `siliconno3@192.168.1.87` → NAS `solvea@192.168.1.80`，
scp 用密码认证（`-o PubkeyAuthentication=no`）。

## 待办（按优先级）

### 高 — 已就绪，待执行
- **PostgreSQL 迁移**：代码、postgres 容器、迁移脚本全部就绪，**未执行**
  （暂停前不宜跑未实测的生产迁移）。步骤见 `backend/scripts/PG_MIGRATION.md`。
  执行后把 `.env` 的 `DATABASE_URL` 切到 postgresql 行。
- **MCP 注册中心提交**：仓库转 public 后，按 `deliverables/mcp-registry-submission.md`
  提交官方 Registry（`scripts/publish.sh` via publish-mcp 技能）+ mcp.so 等。

### 中 — 采集器收尾（4 个空站）
- `vidaxl_us`：HTTP Basic Auth 墙（Demandware）。客户账号进不去，需站点级
  htpasswd 或走官方 Dropshipping API（`VIDAXL_API_EMAIL`/`VIDAXL_API_TOKEN`，
  vidaxl.py 已内置 `_crawl_api` 路径）。
- `vidaxl_ca`：sitemap 路径不对，小修。
- `costway_pl` / `flexispot_pl`：波兰站采集返 0，需查 geo / 结构差异。

### 低 — 功能项
- Google Shopping：DOM 抓不动，需定 SERP API 方案。
- 模块三：商品属性提取 + 看板评论 Tab。
- 评论扩量：NAS 上 google_map / reviews_io 渠道仍为 0。

## 关键设计

- 采集器按 `site.platform` 注册（`backend/app/crawlers/registry.py`）：
  shopify / nuxt / vue_spa / generic / flexispot / vidaxl / vonhaus / **magento**。
  加站点改 `backend/sites.yaml`，`_seed_sites` 会同步平台变更到 DB。
- `magento` 采集器（本轮新增）：robots.txt 发现 sitemap + 递归展开 +
  并发抓取 + JSON-LD/OG 判别商品。覆盖 Costway 欧洲站、VonHaus。
- MCP 服务器 `app/mcp_server.py`（FastMCP），挂在 FastAPI `/mcp`；
  鉴权中间件在 `main.py`，校验 `Authorization: Bearer sck_...`。
- 发现层 `app/api/discovery.py`：`/llms.txt`、`/.well-known/*`、`/agents.json`。
- 反爬 `app/antiban.py`：熔断 + 限速档 + 站点冷却 + IP 配额。
- 注意：`apikey.hash_key` 依赖 `SC_SECRET`，轮换 SC_SECRET 会使所有
  已发 API Key 失效（需重新生成）。
