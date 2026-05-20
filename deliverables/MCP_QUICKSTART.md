# smart-crawler MCP — 5 分钟接入指南

> 给 AI Agent 一个能用的"竞品情报 + Reddit 社区分析"服务。
> 任何支持 MCP 协议的 Agent（Claude Desktop / Cursor / Cline / Continue）都能直接调。

---

## 1. 服务概览

| 项目 | 值 |
|------|----|
| **MCP Endpoint** | `https://smartcrawler.io/mcp/` ← 末尾斜杠必须有 |
| **传输** | streamable-http |
| **鉴权** | `Authorization: Bearer sck_...` |
| **工具数** | 12 |
| **官网** | https://smartcrawler.io |
| **文档** | https://smartcrawler.io/docs |
| **API Key 申请** | mcp@smartcrawler.io |

---

## 2. 12 个工具

### 🛒 电商竞品情报（7）

| 工具 | 一句话 |
|------|--------|
| `list_data_sources` | 列出全部数据源：46 站 + 评论平台 + Google Shopping |
| `search_competitor_products` | 按品牌 / 国家 / 关键词 / 价格 / 促销搜竞品商品 |
| `get_product_detail` | 单商品完整信息 + 历史价格曲线 |
| `list_promotions` | 当前促销活动 + 折扣率 |
| `get_voc_reviews` | 消费者口碑评论 + NLP 情感 / 分类 |
| `voc_summary` | 口碑分析汇总（情感分布 + 痛点占比） |
| `competitor_landscape` | Google Shopping 关键词商家份额 |

### 📦 亚马逊 VOC（2）

| 工具 | 一句话 |
|------|--------|
| `amazon_voc_report` | ASIN 评论抓取 + AI VOC 分析（痛点 / 卖点 / Listing 优化） |
| `fetch_amazon_reviews` | ASIN 原始评论数组（不做分析） |

### 🤖 Reddit 社区情报（3）

| 工具 | 一句话 |
|------|--------|
| `reddit_top_contributors` | 找 subreddit top N 贡献者 |
| `reddit_user_activity` | 用户完整发帖 + 评论历史（含已删帖） |
| `reddit_subreddit_playbook` | 一键生成 top N 完整 Playbook（成长 + 5 步路径） |

---

## 3. 接入方式（按 Agent 选）

### Claude Desktop / Claude Code CLI

**配置文件位置**：
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Claude Code CLI: `~/.claude.json`（mcpServers 字段）

**加入这一段**：

```json
{
  "mcpServers": {
    "smart-crawler": {
      "type": "streamable-http",
      "url": "https://smartcrawler.io/mcp/",
      "headers": {
        "Authorization": "Bearer sck_你的密钥"
      }
    }
  }
}
```

重启 Claude → 在对话里直接说："找 r/entrepreneur 最有影响力的 3 个人，生成 playbook"

---

### Cursor

**配置文件**：`~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "smart-crawler": {
      "url": "https://smartcrawler.io/mcp/",
      "headers": {
        "Authorization": "Bearer sck_你的密钥"
      }
    }
  }
}
```

打开 Cursor → Cmd+Shift+J → MCP → 看到 smart-crawler 12 个工具就成功了。

---

### Cline (VSCode 插件)

VSCode 设置 → 搜 "Cline MCP" → 打开 MCP Servers 配置 → 加：

```json
{
  "smart-crawler": {
    "transport": {
      "type": "streamable-http",
      "url": "https://smartcrawler.io/mcp/"
    },
    "headers": {
      "Authorization": "Bearer sck_你的密钥"
    }
  }
}
```

---

### 命令行测试（curl）

不依赖任何 Agent，直接 JSON-RPC 调：

```bash
curl -X POST https://smartcrawler.io/mcp/ \
  -H "Authorization: Bearer sck_你的密钥" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }'
```

返回 12 个工具的清单 = 接入成功。

---

## 4. 真实用法示例

### 例 1：找 subreddit KOL（10 秒）

**给 Agent 说**：
> "用 smart-crawler 找 r/AmazonFBA 的 top 3 贡献者，告诉我他们都是谁，karma 多少。"

Agent 调用：
```json
{
  "tool": "reddit_top_contributors",
  "arguments": { "subreddit": "AmazonFBA", "top_n": 3 }
}
```

返回：
```json
{
  "top_contributors": [
    { "username": "SlyGuyxD", "posts_in_sub": 12, "total_karma": 3917, "reddit_age_days": 2982 },
    ...
  ]
}
```

---

### 例 2：生成 Reddit 完整 Playbook（3-5 分钟）

> "帮我生成 r/entrepreneur top 3 贡献者的成长 playbook，重点看他们怎么从 0 做到顶级 KOL 的。"

Agent 调用 `reddit_subreddit_playbook` → 返回 Markdown + JSON 双格式，包含：
- 每位贡献者的成长时间线（起步期 / 爆发期 / 成熟期）
- 内容公式（什么话题 + 什么形式有效）
- 爆款帖拆解（为什么这帖爆了）
- 5 步可复制路径

---

### 例 3：竞品价格监控

> "搜一下 SONGMICS 在美国的所有储物柜商品，按当前售价排序，标出在促销的。"

Agent 调用：
```json
{
  "tool": "search_competitor_products",
  "arguments": {
    "brand": "SONGMICS",
    "country": "US",
    "keyword": "storage cabinet",
    "on_promotion": true,
    "limit": 20
  }
}
```

---

### 例 4：亚马逊 ASIN 口碑分析

> "我们的 ASIN B08XYZ123 在美区，帮我分析评论里的主要痛点和卖点。"

Agent 调用 `amazon_voc_report` → 返回情感分布 + 痛点分类 + Listing 优化建议（中英双语）。

---

## 5. 数据特点

| 特性 | 说明 |
|------|------|
| **无需 Reddit API Key** | 走公开 JSON + Arctic Shift 历史存档 |
| **含已删帖** | Arctic Shift 是 Reddit 全站镜像 |
| **限流自适应** | 1.2 req/s，不会被 Reddit 封 |
| **跨境电商覆盖** | 46 站：SONGMICS / Costway / Homary / Vidaxl / Flexispot 等 9 大品牌 |
| **9 国市场** | US / UK / DE / FR / IT / ES / NL / PL / CA |
| **评论数据** | Trustpilot / Reviews.io / Google Maps |

---

## 6. 申请 API Key

发邮件到 **mcp@smartcrawler.io**，附：
1. 你的姓名 + 公司
2. 用途简述（哪些工具 / 预估调用量）

我们当天回邮件 + 给 Key。**免费 trial 含**：
- 100 次 `reddit_top_contributors`
- 30 次 `reddit_user_activity`
- 5 次 `reddit_subreddit_playbook`（含 LLM playbook 生成）
- 1000 次电商查询（`search_competitor_products` / `list_promotions` 等）

---

## 7. 常见问题

**Q：MCP 调用要不要自己装 Python / Node？**  
A：不需要。endpoint 是远程 HTTP，Agent 直接走 HTTPS 连。

**Q：为什么 URL 末尾要带斜杠？**  
A：服务器配的是 `/mcp/`，没斜杠会 307 重定向到 http（curl 默认会跟错），Agent 框架则可能直接报错。

**Q：playbook 生成为什么这么慢？**  
A：30 个用户串行 × 每人 Reddit 限流采集（~10s）+ LLM 分析（~30s），所以单 subreddit top 3 ≈ 3-5 分钟。如果只要数据不要 playbook，用 `reddit_user_activity` 单独调，10 秒/人。

**Q：能本地部署吗？**  
A：可以。GitHub 仓库：https://github.com/mguozhen/smart-crawler  
   `python -m app.mcp_server` 起本地 stdio MCP server（见仓库 README）。

**Q：支持哪些 LLM gateway？**  
A：`reddit_subreddit_playbook` 默认走 flatkey.ai，也支持 OpenAI / Anthropic / Azure（在服务端配 `LLM_BASE_URL` 即可，客户无感知）。

---

## 8. 资源链接

| 资源 | URL |
|------|-----|
| 官网 | https://smartcrawler.io |
| MCP Endpoint | https://smartcrawler.io/mcp/ |
| GitHub | https://github.com/mguozhen/smart-crawler |
| 文档 | https://smartcrawler.io/docs |
| Reddit 功能详解 | https://github.com/mguozhen/smart-crawler/blob/main/docs/reddit-intelligence.md |
| Demo 报告（30 位贡献者深度档案） | 见附件 `reddit_deep_research.html` |
| 申请 API Key | mcp@smartcrawler.io |

---

*smart-crawler · MCP Quickstart · 2026-05-20*
