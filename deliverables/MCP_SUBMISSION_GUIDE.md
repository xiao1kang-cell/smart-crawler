# MCP Catalog 提交清单 — smart-crawler

> 目标：让 AI Agent 在 Smithery / Glama / MCP Hub 直接发现并安装 smart-crawler。
> 工具索引就是 Agent 时代的市场份额。

---

## 提交前自检

- [x] `smithery.yaml` 在仓库根目录（已存在）
- [x] `mcp.json` 在仓库根目录（已存在）
- [x] `README.md` 头部说明 MCP endpoint（待补充章节）
- [x] `docs/reddit-intelligence.md` API 文档
- [ ] 仓库 push 到 `mguozhen/smart-crawler` 最新
- [ ] GitHub repo 设为 Public，加 topic `mcp`, `mcp-server`, `reddit`, `ecommerce`, `competitor-analysis`

---

## 1. Smithery（首选）

**网址**：https://smithery.ai/submit

**步骤**：
1. 用 GitHub 登录
2. 输入仓库 URL：`https://github.com/mguozhen/smart-crawler`
3. Smithery 自动读取根目录 `smithery.yaml`
4. 填写：
   - **Name**: `smart-crawler`
   - **Description**: `Cross-border ecommerce intelligence + Reddit community analysis — 46 competitor sites, VOC reviews, Google Shopping landscape, and subreddit growth playbooks. Zero-config, no API keys required.`
   - **Category**: `Web Scraping` / `Data & Analytics`
   - **Icon**: 上传 `frontend/public/logo.png`（如有）
5. 配置 secrets schema（自动从 `smithery.yaml` 读取）
6. 提交审核（一般 1-3 天）

**审核通过后**：用户在 Smithery 搜 "reddit" / "subreddit" / "ecommerce" 都能找到。

---

## 2. Glama MCP Hub

**网址**：https://glama.ai/mcp/servers

**步骤**：
1. 点 "Submit Server"
2. 仓库 URL: `https://github.com/mguozhen/smart-crawler`
3. Glama 会自动 scrape `mcp.json` 和 `README.md`
4. 提交（即时上线）

Glama 优势：自动生成在线试用沙箱，潜在客户能直接试。

---

## 3. Awesome MCP Servers（官方维护列表）

**网址**：https://github.com/punkpeye/awesome-mcp-servers

**步骤**：
1. Fork 该仓库
2. 编辑 `README.md`，在合适分类下加一行：
   ```md
   ### Web Scraping & Crawling
   - [smart-crawler](https://github.com/mguozhen/smart-crawler) — Ecommerce competitor data + Reddit community intelligence. 46 sites + subreddit playbook generator.
   ```
3. 提 PR

合并后会被各种 awesome-mcp 镜像站和 Agent 框架抓取。

---

## 4. Anthropic MCP Servers 列表（社区）

**网址**：https://github.com/modelcontextprotocol/servers

**适用于**：核心 MCP 协议官方仓库。提交需要严格符合规范。

**先决条件**：
- 通过 `mcp-validator` 工具校验
- 提供完整 README + 示例
- 单元测试覆盖

**优先级**：中（耗时长，门槛高，但权威性最高）。先把 Smithery + Glama 做完。

---

## 5. Cline / Continue.dev / Cursor 内置目录

| 平台 | 提交方式 | 网址 |
|------|----------|------|
| Cline | PR 到 cline/mcp-marketplace | https://github.com/cline/mcp-marketplace |
| Continue.dev | 加到 config.json 示例库 | https://docs.continue.dev |
| Cursor | 暂未开放公共目录（关注 Cursor changelog） | - |

---

## 提交完成后

更新 `deliverables/customer_email_reddit.md` 末尾，加：
```
✅ 现在可以在 Smithery / Glama 直接安装：
- https://smithery.ai/server/smart-crawler
- https://glama.ai/mcp/servers/smart-crawler
```

---

## 一周后跟进指标

- Smithery 搜索关键词 rank（"reddit", "subreddit", "ecommerce scraper"）
- Glama 总安装数 / 日新增
- GitHub repo Stars / Watchers
- MCP tool 实际调用量（埋在 mcp_server.py 里的 telemetry）
