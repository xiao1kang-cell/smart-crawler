# smart-crawler MCP 服务器 — 注册中心提交材料

> 准备日期：2026-05-17
> MCP 端点：`https://smartcrawler.io/mcp`（streamable-http，7 工具，Bearer 鉴权）
> GitHub 仓库：`mguozhen/smart-crawler` — **当前为 PRIVATE（私有）**
> 项目根清单：`server.json`（schema `2025-10-17`，已校验通过）

---

## ⚠️ 阻塞项：仓库私有

很多注册中心靠抓取 **public GitHub 仓库** 来收录或评分：

- **Glama** — 完全自动抓 public GitHub，私有仓库无法被索引/评分。
- **awesome-mcp-servers**（punkpeye / wong2）— 列表条目是指向 GitHub 仓库的链接，私有仓库链接对读者 404。
- **官方 MCP Registry** — `mcp-publisher` 用 GitHub OIDC 鉴权 `io.github.mguozhen/*` 命名空间，**发布动作本身不要求仓库 public**，但 registry 条目里的 `repository.url` 会指向私有仓库，下游消费者点进去会 404。
- **mcp.so / PulseMCP** — 收录主要看 MCP 端点与元数据，可填私有仓库，但同样存在「点链接 404」问题。

**建议**：提交前先把 `mguozhen/smart-crawler` 设为 public（`gh repo edit mguozhen/smart-crawler --visibility public`），或保持私有但接受 Glama / awesome-list 无法收录、其余渠道仓库链接对外不可达。**本批材料默认仓库会转 public**——若维持私有，跳过 Glama 与两个 awesome-list。

---

## 渠道 1 — 官方 MCP Registry ★必发，CLI 可自动化★

- **地址**：`registry.modelcontextprotocol.io` ｜ 仓库 `modelcontextprotocol/registry`
- **提交方式**：`mcp-publisher` CLI，GitHub OIDC 鉴权（`io.github.<owner>` 命名空间最简单，无需 DNS 验证）
- **前置**：项目根 `server.json` 已就绪并校验通过（见下）。

**发布步骤**（在仓库根目录执行）：

```bash
# 1. 校验 server.json 结构
bash ~/.claude/skills/publish-mcp/scripts/publish.sh validate

# 2. 探测 MCP 端点存活（401 = 存活且需鉴权，正常）
bash ~/.claude/skills/publish-mcp/scripts/publish.sh check https://smartcrawler.io/mcp

# 3. 登录 + 发布（会触发浏览器/设备码 GitHub 授权）
bash ~/.claude/skills/publish-mcp/scripts/publish.sh publish
#   等价于： mcp-publisher login github  &&  mcp-publisher publish
```

> 自动化程度：✅ 完全 CLI 化。但 `mcp-publisher login github` 需要交互式 GitHub 授权
> （浏览器或设备码），**这一步需要用户在场点确认**，本批未代为执行。
> 发布成功后，mcp.so / Glama / PulseMCP 等下游目录会在 24~48h 内陆续自动同步。

**`server.json` 内容**（已写入项目根，schema = `2025-10-17`，repo id = `1241179415`）：

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-10-17/server.schema.json",
  "name": "io.github.mguozhen/smart-crawler",
  "description": "Cross-border e-commerce competitor intelligence engine for AI agents...",
  "version": "0.1.0",
  "repository": { "url": "https://github.com/mguozhen/smart-crawler", "source": "github", "id": "1241179415" },
  "remotes": [{
    "type": "streamable-http",
    "url": "https://smartcrawler.io/mcp",
    "headers": [{ "name": "Authorization", "value": "Bearer {api_key}", "isRequired": true, "isSecret": true, ... }]
  }]
}
```

---

## 渠道 2 — mcp.so ★流量最大社区目录★

- **提交 URL**：https://mcp.so/submit
- **提交方式**：⏳ 网页表单（也会被官方 Registry 自动同步收录，发完渠道 1 可先等同步）
- **自动化**：否，需用户去网页填表

**表单字段内容**：

| 字段 | 填写内容 |
|------|----------|
| Name | `smart-crawler` |
| Type | MCP Server |
| Server URL / Endpoint | `https://smartcrawler.io/mcp` |
| Transport | Streamable HTTP |
| GitHub / Repo URL | `https://github.com/mguozhen/smart-crawler` |
| Homepage | `https://smartcrawler.io` |
| Description | Cross-border e-commerce competitor intelligence engine for AI agents. 46 brand storefronts across 9 home-goods brands + 21 review channels — structured competitor product/price/promotion data, voice-of-customer reviews with NLP sentiment analysis, and Google Shopping competitive landscape. 7 MCP tools. Bearer API-key auth. |
| Category / Tags | E-Commerce, Web Scraping, Data Extraction, Competitor Intelligence |
| Auth note | Requires `Authorization: Bearer sck_...` API key (generated in console) |

---

## 渠道 3 — PulseMCP

- **提交 URL**：https://www.pulsemcp.com/submit （或站内 "Submit a server"）
- **提交方式**：⏳ 网页表单
- **自动化**：否

**表单字段内容**：

| 字段 | 填写内容 |
|------|----------|
| Server name | smart-crawler |
| Hosting | Remote (Hosted) |
| Remote URL | `https://smartcrawler.io/mcp` |
| Transport | streamable-http |
| GitHub URL | `https://github.com/mguozhen/smart-crawler` |
| Website | `https://smartcrawler.io` |
| Short description | Competitor intelligence MCP for cross-border e-commerce: product/price/promotion data, VOC reviews + NLP sentiment, Google Shopping landscape across 46 storefronts. |
| Long description | smart-crawler turns web data collection into an agent-callable service: adaptive crawling, anti-blocking, traceable. 7 tools — list_data_sources, search_competitor_products, get_product_detail, list_promotions, get_voc_reviews, voc_summary, competitor_landscape. Covers 9 home-goods brands (SONGMICS / VASAGLE / FEANDREA / Costway / Homary / Vidaxl / Flexispot / VonHaus) across 46 storefronts in 12 countries, plus 21 review channels. |
| Categories | E-Commerce, Data Extraction, Web Scraping |
| Authentication | API key via `Authorization: Bearer sck_...` header |

---

## 渠道 4 — Smithery ★安装/托管★

- **提交 URL**：https://smithery.ai/new （GitHub 登录后在站内 Add Server）
- **提交方式**：⏳ 网页 + 仓库配置
- **自动化**：否，需 GitHub 登录
- **前置**：Smithery 偏向「可一键安装/托管」的 server。本服务器是远程 streamable-http，
  可作为远程 server 登记；如要走 Smithery 托管/扫描，需在仓库根加 `smithery.yaml`。
  **本批未生成 `smithery.yaml`** —— 远程 server 登记不强制需要，按需补。

**登记字段内容**：

| 字段 | 填写内容 |
|------|----------|
| GitHub repository | `mguozhen/smart-crawler` |
| Server type | Remote |
| Connection URL | `https://smartcrawler.io/mcp` |
| Transport | streamable-http |
| Display name | smart-crawler |
| Description | Cross-border e-commerce competitor intelligence engine — 7 MCP tools for competitor product/price/promotion data, VOC reviews with NLP sentiment, and Google Shopping landscape. |
| Config / Auth | Header `Authorization: Bearer <apiKey>`，apiKey 为必填 secret |

可选 `smithery.yaml` 草案（如决定走 Smithery 托管再加入仓库根）：

```yaml
startCommand:
  type: http
  url: https://smartcrawler.io/mcp
  configSchema:
    type: object
    required: [apiKey]
    properties:
      apiKey:
        type: string
        description: smart-crawler API key, prefixed with sck_
```

---

## 渠道 5 — awesome-mcp-servers（GitHub 列表，PR 收录）

提交方式：fork → 在对应分类加一行 → 提 PR。**自动化：✅ 可用 `gh` CLI 全自动**，
但属对外操作（开 PR），本批仅准备内容、未执行。

### 5a. punkpeye/awesome-mcp-servers（最大）

- 仓库：`github.com/punkpeye/awesome-mcp-servers`
- **分类**：`🔎 Search & Data Extraction`
- **要加的那一行 Markdown**（按该列表格式：链接 + 图例符号 + 一句话）：

```markdown
- [mguozhen/smart-crawler](https://github.com/mguozhen/smart-crawler) 🐍 ☁️ - Cross-border e-commerce competitor intelligence — product/price/promotion data, VOC reviews with NLP sentiment, and Google Shopping landscape across 46 storefronts. Remote MCP server.
```

图例：`🐍` = Python 代码库，`☁️` = 云服务（远程 API）。本服务器是远程托管的 Python 服务，故 `🐍 ☁️`。
> 备选分类 `🛒 E-Commerce` 也成立；该列表里抓取/数据提取类服务器惯例归 `🔎 Search & Data Extraction`，优先用它。

### 5b. wong2/awesome-mcp-servers

- 仓库：`github.com/wong2/awesome-mcp-servers`
- 该列表分类较简，归到 **Web Scraping / Data Extraction** 或 **Other** 区段。
- **要加的那一行**（wong2 列表不带图例符号）：

```markdown
- [smart-crawler](https://github.com/mguozhen/smart-crawler) - Cross-border e-commerce competitor intelligence: product/price/promotion data, VOC reviews + NLP sentiment, Google Shopping landscape. Remote MCP server, 7 tools.
```

**PR 操作（待用户确认后执行）**：

```bash
gh repo fork punkpeye/awesome-mcp-servers --clone --remote
# 编辑 README.md，在 "Search & Data Extraction" 分类下按字母序插入上面那行
git checkout -b add-smart-crawler && git commit -am "Add smart-crawler MCP server" && git push
gh pr create --repo punkpeye/awesome-mcp-servers --title "Add smart-crawler MCP server" \
  --body "Adds smart-crawler — a remote MCP server for cross-border e-commerce competitor intelligence (7 tools, streamable-http)."
# wong2 同理
```

---

## 渠道汇总表

| 渠道 | 提交方式 | 自动化 | 状态 | 阻塞 |
|------|----------|--------|------|------|
| 官方 MCP Registry | `mcp-publisher` CLI | ✅ CLI（login 需用户授权） | ⏳ 待用户跑 `publish.sh publish` | 无（server.json 已就绪） |
| mcp.so | 网页 https://mcp.so/submit | ❌ 表单 | ⏳ 待用户提交 | 无（字段已备好） |
| PulseMCP | 网页 https://www.pulsemcp.com/submit | ❌ 表单 | ⏳ 待用户提交 | 无 |
| Smithery | 网页 https://smithery.ai/new | ❌ 表单 + GitHub 登录 | ⏳ 待用户提交 | 无 |
| Glama | 自动抓 public GitHub | 🟡 自动 | ⏳ 待仓库转 public 后自动收录 | **仓库私有** |
| awesome-mcp (punkpeye) | fork + PR | ✅ `gh` CLI | ⏳ 待用户确认后开 PR | 仓库私有则链接 404 |
| awesome-mcp (wong2) | fork + PR | ✅ `gh` CLI | ⏳ 待用户确认后开 PR | 仓库私有则链接 404 |

**发布顺序建议**：先把仓库转 public → 跑官方 Registry（`publish.sh publish`）→ 等 24~48h 下游同步 → 再手动补 mcp.so / PulseMCP / Smithery 表单 + 两个 awesome-list PR。
