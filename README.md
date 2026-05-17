# smart-crawler — 遨森标杆数据采集平台

> 竞品标杆网站商品 / 价格 / 促销数据的采集、清洗、分析与可视化平台。
> 本仓库为 **MVP（P0 三品牌）**，落地自《遨森标杆数据采集平台需求规格说明书》（65 条功能需求 / 46 站 / 21 评论平台）。

---

## 这是什么

输入 **Aosom 竞品标杆网站**，输出 **结构化竞品数据交付物**：

- 后端采集器：自动抓取 SONGMICS / Homary / Costway 三品牌独立站的全量商品
- 数据管线：清洗、去重、格式标准化、变更检测、价格曲线、促销识别
- REST API + Web 看板：对标需求文档 PDF 的 3-Tab 界面（总体分析 / 产品分析 / 销售促销）
- Excel 导出：列结构对标甲方样本报表
- 定时调度：每站点可配置采集频率

## MVP 实测成果（2026-05-16 首次采集）

| 站点 | 平台 | 采集量 | 促销识别 | 采集方式 | 耗时 |
|------|------|--------|----------|----------|------|
| SONGMICS US | Shopify | 4,153 SKU / 1,494 SPU / 449 分类 | 580 | `/products.json` 直拉 | 24s |
| Homary US | Nuxt SSR | 演示抓 150 条（站内共 4,347） | 0* | sitemap + SSR HTML 解析 | ~3s/条 |
| Costway US | Vue3 SPA | 1,056 SKU（每分类 N 页可配） | 925 | `/api/*` JSON 直连 | 62s |

\* Homary 商品页未单独暴露划线原价，原价=售价，故首次采集促销为 0；价格促销将由后续多次采集的价格曲线对比识别。

---

## 快速开始

```bash
# 一键启动（首次会自动建 venv + 装依赖 + 装 Playwright）
./run.sh
# 打开 http://localhost:8077
```

手动方式：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/playwright install chromium          # Costway 兜底用，curl_cffi 直连优先

cd backend
../.venv/bin/python -m app.cli init            # 建库 + 载入 20 个站点
../.venv/bin/python -m app.cli crawl --site songmics_us
../.venv/bin/python -m app.cli crawl --brand Homary
../.venv/bin/python -m app.cli export --out ../deliverables/report.xlsx --site costway_us
../.venv/bin/uvicorn app.main:app --port 8077
```

环境变量（可选，难站走代理时）：

```bash
export RESIDENTIAL_PROXY=http://user:pass@host:port   # 住宅代理（被 ASN 封锁时）
export HOMARY_LIMIT=150            # Homary 单次抓取条数（全量 4000+ 约 2 小时）
export COSTWAY_PAGES_PER_CAT=3     # Costway 每分类抓取页数（pagesize=48）
```

---

## 架构

```
backend/
  app/
    main.py          FastAPI 入口 + 托管前端 + 启动调度
    db.py            SQLAlchemy + SQLite
    models.py        Site/Product/PriceHistory/Category/Promotion/Trend/CrawlJob
    pipeline.py      清洗 / 去重 / 标准化 / 变更检测 / 入库 upsert
    analytics.py     销量·营收估算 + 趋势日汇总
    runner.py        采集编排：建任务→跑采集器→入库→促销识别
    scheduler.py     APScheduler 定时采集
    crawlers/
      base.py        采集器基类
      shopify.py     SONGMICS — /products.json
      homary.py      Homary — sitemap + SSR HTML
      costway.py     Costway — /api/* JSON
      registry.py    按平台选采集器
    api/routes.py    REST API
  sites.yaml         20 个站点配置
frontend/index.html  Vue3 + ECharts 单文件看板
```

技术选型：FastAPI · SQLAlchemy · SQLite · curl_cffi（带浏览器 TLS 指纹）·
Playwright（难站兜底）· APScheduler · pandas/openpyxl · Vue3 + ECharts。
选型依据见 `research/github-crawler-survey.md`。

## MCP 接入（面向 AI Agent）

smart-crawler 把竞品数据采集能力直接做成 **MCP 服务器**，AI Agent 无需自己写爬虫即可调用。
落地原则：**Agents 是新的分发渠道 —— 做能力，不做界面。**

- **端点**：`https://smartcrawler.io/mcp`
- **传输**：streamable-http
- **鉴权**：请求头 `Authorization: Bearer sck_...`（API Key，在控制台「API 接入」生成）
- **清单**：项目根 [`server.json`](./server.json)（官方 MCP Registry 格式）
- **发现层**：`/llms.txt` · `/.well-known/mcp.json` · `/.well-known/ai-plugin.json` · `/agents.json`

### 7 个 MCP 工具

| 工具 | 说明 |
|------|------|
| `list_data_sources` | 列出全部数据源：46 个竞品站 + 评论平台 + Google Shopping |
| `search_competitor_products` | 按品牌 / 国家 / 关键词 / 品类 / 价格 / 促销搜索竞品商品 |
| `get_product_detail` | 取单个商品完整信息 + 历史价格曲线 |
| `list_promotions` | 列出竞品当前促销活动及折扣率 |
| `get_voc_reviews` | 取消费者口碑（VOC）评论 + NLP 情感 / 分类标注 |
| `voc_summary` | 口碑分析汇总：情感分布 + 痛点分类占比 |
| `competitor_landscape` | Google Shopping 某关键词下各商家出现占有率 |

### 客户端配置示例

streamable-http 远程 MCP 服务器，配置示例（Claude Desktop / Cursor 等）：

```json
{
  "mcpServers": {
    "smart-crawler": {
      "type": "streamable-http",
      "url": "https://smartcrawler.io/mcp",
      "headers": { "Authorization": "Bearer sck_你的密钥" }
    }
  }
}
```

直接 JSON-RPC 握手探测：

```bash
curl -X POST https://smartcrawler.io/mcp \
  -H 'Authorization: Bearer sck_你的密钥' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

---

## REST API

REST 为 MCP 的备选接入方式，鉴权用请求头 `X-API-Key: sck_...`。
本地启动后的端点：

| 端点 | 说明 |
|------|------|
| `GET /api/sites` | 站点列表 + 核心指标 |
| `GET /api/sites/{site}/overview` | 6 指标卡 + 趋势序列 |
| `GET /api/products` | 商品查询（站点/标签页/搜索/价格/分页） |
| `GET /api/products/{id}/price-history` | 单 SKU 价格曲线 |
| `GET /api/promotions` | 促销活动列表 |
| `GET /api/trends` `GET /api/categories` | 趋势 / 分类树 |
| `GET /api/jobs` `POST /api/jobs/trigger` | 采集任务看板 / 手动触发 |
| `GET /api/scheduler` | 定时任务列表 |
| `GET /api/export/products` | Excel 导出 |

完整交互文档：启动后访问 `http://localhost:8077/docs`。

---

## 已知限制 / 后续路线

**本 MVP 已实现**：P0 三品牌商品采集 + 清洗管线 + REST API + 3-Tab 看板 +
Excel 导出 + 定时调度 + 任务看板。

**今晚范围外（后续迭代）**：

- 其余 43 个站点（P1 Yaheetech/Vidaxl/Flexispot、P2 等）—— 采集器框架已支持，补站点配置即可
- 21 个第三方评论平台采集（Trustpilot / Google Maps / TrustedShop 等）
- 评论 NLP 情感分析与多语种分类（模块二/三）
- Google Shopping 关键词采集（模块四）
- 销量估算需 ≥2 次采集积累评论增量后才有非零值
- Costway 商品图 CDN 有防盗链，看板缩略图需后端图片代理才能显示（数据本身完整）
- 多租户、PostgreSQL、高可用部署
