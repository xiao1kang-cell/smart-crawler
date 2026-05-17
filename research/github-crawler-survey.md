# GitHub 开源爬虫项目对标调研 — smart-crawler 选型依据

> 调研日期：2026-05-16。用于 smart-crawler 技术选型。

## 1. 通用爬虫框架

| 项目 | Star | 定位 | 对本项目 |
|------|------|------|----------|
| Scrapy | ~55k | Python 异步爬虫事实标准 | pipeline/中间件成熟，配 scrapy-redis 可分布式；不渲染 JS |
| Crawlee (apify) | JS~17k/Py~6k | 自带反爬默认值的现代爬虫库 | 内置指纹/代理轮换、请求队列；Python 版较新 |
| feapder | ~5k | 国产一体化框架，含管理后台 | 自带分布式/报警/监控面板，中文文档 |
| Colly | ~24k | Go 高性能爬虫 | 静态页最快；Go 技术栈 |

## 2. AI/LLM 驱动智能爬虫

| 项目 | Star | 定位 | 对本项目 |
|------|------|------|----------|
| Crawl4AI | ~58k | LLM-friendly 爬虫，输出干净 markdown | 异构站「免写选择器」抽字段；逐页 LLM 有成本 |
| Firecrawl | ~110k | 爬取→结构化 JSON 的 API（可自托管） | `/extract` schema 抽取；评论页清洗 |
| ScrapeGraphAI | ~20k | 图 + LLM 声明式抽取 | 新站 PoC 快；生产稳定性不如固定规则 |

**结论**：LLM 抽取用于「新站冷启动 / DOM 易变」兜底，不做全量主力。

## 3. 反爬对抗 / 浏览器隐身

| 项目 | Star | 定位 |
|------|------|------|
| **curl_cffi** | ~5k | 带浏览器 TLS/JA3 指纹的 HTTP 客户端 —— **本项目首选** |
| nodriver | ~3k | undetected-chromedriver 继任者，绕 Cloudflare |
| botasaurus | ~3k | 一体化反爬，能过 CF JS+CAPTCHA 挑战 |
| FlareSolverr | ~10k | Cloudflare 挑战代理服务 |

> `playwright-stealth` 已于 2025 基本失效，不作主力。

**结论**：分层 —— 默认 curl_cffi → 难站 nodriver/Playwright → 强 CF 站 botasaurus。

## 4. Shopify 采集

标准 Shopify 站公开 `/products.json?limit=250&page=N`，**无需浏览器/选择器**。
参考 `practical-data-science/ShopifyScraper`、`omkarcloud/shopify-scraper` 的
variants 展开逻辑。→ 本项目 SONGMICS 采集器即基于此。

## 5. 评论平台采集

- `omkarcloud/google-maps-reviews-scraper`、`georgekhananaev/google-reviews-scraper-pro`（增量逻辑可复用）
- `irfanalidv/trustpilot_scraper`
- Trustpilot 实战：评论数据藏在 Next.js 的 `__NEXT_DATA__` 内嵌 JSON，解析 JSON 比 DOM 稳。
- 无「一统 21 平台」开源项目 —— 每平台优先找内嵌 JSON / widget API。

## 6. Google Shopping / SERP

`NorkzYT/Novexity`（自托管 SerpAPI 替代）。Google Shopping 反爬强、开源成熟度低，
建议后续直接买 SERP API 或自托管 Novexity。

## 7. 调度与监控

| 项目 | Star | 定位 |
|------|------|------|
| Crawlab | ~12k | 框架无关的分布式爬虫管理平台（Web UI/调度/节点/日志） |
| scrapy-redis / Scrapyd / Gerapy | — | Scrapy 体系分布式 + 部署 + 可视化 |
| changedetection.io | ~30k | 网页变更/价格/补货监控，自带调度+通知 |

**结论**：MVP 用 APScheduler 进程内调度即可；平台化阶段引入 Crawlab。

## 8. 完整电商监控系统

没有覆盖「46 站 + 21 评论平台 + 清洗 + 情感分析 + 报表」全链路的开源系统 ——
这正是 smart-crawler 的价值空间。价格变更告警可叠加 changedetection.io。

---

## 选型结论（已落地本项目）

| 层 | 选型 | 理由 |
|----|------|------|
| HTTP 采集 | **curl_cffi** | 带浏览器 TLS 指纹，SONGMICS/Homary/Costway 实测均可直连 |
| 浏览器兜底 | Playwright | 强反爬站点保留能力 |
| 后端 | FastAPI + SQLAlchemy + SQLite | 轻量、自带 API 文档 |
| 调度 | APScheduler | MVP 进程内 cron |
| 前端 | Vue3 + ECharts（CDN 单文件） | 零构建，今晚即可出看板 |

**关键发现**：SONGMICS 是标准 Shopify 站、Costway 的 `/api/*` 可 curl_cffi 直连 ——
三个 P0 站点今晚均无需重型框架 / 浏览器 / LLM 即可拿到结构化数据。
