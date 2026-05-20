# 客户邮件草稿 — Reddit Intelligence 功能交付

**Subject:** smart-crawler Reddit Intelligence 已上线 — 30 位贡献者深度档案 + MCP 接入

---

Hi [客户名],

Reddit Intelligence 已完成交付，三个东西发给你：

---

### 1. Deep Research 报告（主交付物）

10 个商业 subreddit · 30 位 top 贡献者 · 完整数据档案

→ **附件**：`reddit_deep_research.html`（221 KB，双击打开）

每位贡献者档案包含：
- 📈 月度发帖时间线（柱状图）
- 🏷️ 高频话题词云（从所有帖子标题抽取）
- 🛤️ 关键节点（首帖 → 最高分爆款 → 最近活跃，三点串成成长轨迹）
- 🔥 Top 3 爆款帖子（真实标题/分数/评论数/正文摘要，可点进 Reddit 看）
- 💬 Top 3 高赞评论
- 🧠 数据模式总结

**已覆盖 subreddit**：entrepreneur · ecommerce · AmazonFBA · dropshipping · smallbusiness · startups · marketing · SEO · SideProject · growthhacking

**几个亮点**（页面里可以看到原帖）：

| 用户 | 所在社区 | 亮点 |
|------|----------|------|
| u/officer_KD6-3-7 | r/SideProject | 单帖 4,345 分，3 帖打开局面 |
| u/Torholic | r/marketing | 单帖 1,726 分 |
| u/wilschroter | r/startups | 14 年老号，48 帖 + 100 评论 |
| u/biz_booster | r/marketing | 100 帖 100 评论，深度互动型 |
| u/johnstevens456 | r/entrepreneur | "So, I found out my employees don't want what I want" — 3,838 分 |

---

### 2. MCP 接入（5 分钟上线）

任何支持 MCP 协议的 AI Agent（Claude Desktop / Cursor / Cline）均可直接调用。

**Claude Desktop / Cursor 配置**：
```json
{
  "mcpServers": {
    "smart-crawler": {
      "url": "https://smartcrawler.io/mcp",
      "headers": { "Authorization": "Bearer YOUR_SC_KEY" }
    }
  }
}
```

配好后直接对 Claude 说：
> "帮我找 r/entrepreneur 最有影响力的 3 个人，生成他们的成长 playbook"

---

### 3. 3 个 MCP 工具

| 工具 | 用途 | 耗时 |
|------|------|------|
| `reddit_top_contributors` | 发现 subreddit top N 贡献者（karma/年龄/得分排名） | ~5 秒 |
| `reddit_user_activity` | 拉某用户完整发帖 + 评论历史（含 Arctic Shift 存档已删帖） | ~10 秒 |
| `reddit_subreddit_playbook` | 一键生成 top N 完整 playbook（成长路径 + 内容公式 + 5 步可复制） | ~3-5 分钟/人 |

CLI 也支持，本地直接跑：
```bash
python scripts/reddit_playbook_cli.py entrepreneur --top 3
```

---

### 数据说明

- **无需 Reddit API Key / OAuth** — 走公开 JSON + Arctic Shift 历史存档
- **含已删帖** — Arctic Shift 是 Reddit 全站镜像，删帖也能取到
- **限流自适应** — 1.2 req/s，永不被封
- **Playbook 生成** 需配置一个 LLM key（flatkey.ai / OpenAI / Anthropic gateway 都行）

完整 API 文档：https://smartcrawler.io/docs/reddit  
GitHub：https://github.com/mguozhen/smart-crawler

---

### 申请 API Key

回复这封邮件，我这边马上开通。免费 trial 含：
- 100 次 `reddit_top_contributors`
- 30 次 `reddit_user_activity`
- 5 次 `reddit_subreddit_playbook`（含 LLM playbook 生成）

---

有问题随时 ping。

Best,  
[你的名字]  
smart-crawler · mcp@smartcrawler.io

---

**📎 附件清单**：
- `reddit_deep_research.html` — 30 位贡献者完整档案（主交付物）
- `reddit_demo.html` — 10 subreddit 概览（轻量版）
- `reddit_showcase.html` — 产品介绍页（含 MCP tools 说明）
- `reddit_activity.json` — 原始数据（1.5 MB，可二次分析）
