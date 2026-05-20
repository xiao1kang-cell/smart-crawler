# Reddit Intelligence — smart-crawler

> 从任意 subreddit 自动发现顶级贡献者，抓取完整发帖历史，生成 AI Playbook。

---

## 能力概览

| 功能 | 说明 |
|------|------|
| Top 贡献者发现 | 按发帖数 × 得分综合排名，找到真正的社区 KOL |
| 完整发帖历史 | 帖子 + 评论，包含已删帖（Arctic Shift 存档） |
| AI Playbook 生成 | 成长时间线 / 内容公式 / 5 步可复制路径 |
| 无需 API Key | Reddit JSON + Arctic Shift，零 OAuth 配置 |
| 反限流 | 1.2s 间隔，双路径自动回退 |

---

## MCP Tools（供 AI Agent 调用）

### `reddit_top_contributors`
找某 subreddit 的 top N 贡献者。

```json
{
  "tool": "reddit_top_contributors",
  "arguments": {
    "subreddit": "entrepreneur",
    "top_n": 3
  }
}
```

**返回**：
```json
{
  "subreddit": "entrepreneur",
  "top_contributors": [
    {
      "username": "johnstevens456",
      "posts_in_sub": 3,
      "post_total_score": 11374,
      "total_karma": 31017,
      "reddit_age_days": 3891,
      "link_karma": 28000,
      "comment_karma": 3017,
      "profile_url": "https://www.reddit.com/u/johnstevens456"
    }
  ]
}
```

---

### `reddit_user_activity`
获取某用户的完整发帖 + 评论历史。

```json
{
  "tool": "reddit_user_activity",
  "arguments": {
    "username": "johnstevens456",
    "subreddit": "entrepreneur",
    "post_limit": 100,
    "comment_limit": 100
  }
}
```

**返回字段**：`profile` / `posts` / `comments` / `top_posts` / `monthly_post_count` / `stats`

---

### `reddit_subreddit_playbook`
一键生成 subreddit top N 贡献者的完整 playbook（最核心工具）。

```json
{
  "tool": "reddit_subreddit_playbook",
  "arguments": {
    "subreddit": "entrepreneur",
    "top_n": 3
  }
}
```

**返回**：结构化 JSON + Markdown 格式的完整 playbook，包含：
- 成长时间线（起步期 / 爆发期 / 成熟期）
- 内容公式（什么话题 + 什么形式 + 什么时机）
- Top 帖子分析（为什么爆了）
- 5 步可复制路径

**耗时**：top_n=3 约 10-15 分钟（Reddit 限流 + LLM 分析）。

---

## CLI 使用

```bash
# 全流程（找 top 3 贡献者 → 生成 playbook）
python scripts/reddit_playbook_cli.py entrepreneur

# 只找贡献者
python scripts/reddit_playbook_cli.py entrepreneur --contributors-only

# 分析特定用户
python scripts/reddit_playbook_cli.py entrepreneur --user johnstevens456

# 指定 top N
python scripts/reddit_playbook_cli.py AmazonFBA --top 5

# 输出到指定路径
python scripts/reddit_playbook_cli.py SEO --output /tmp/seo_playbook.md
```

输出文件默认保存到 `~/smart-crawler-output/`。

---

## Python API

```python
from app.crawlers.reddit import get_top_contributors, get_user_activity
from app.reddit_playbook import generate_subreddit_playbook, generate_user_playbook

# 找 top 3 贡献者
contributors = get_top_contributors("entrepreneur", top_n=3)

# 取用户活动
activity = get_user_activity("johnstevens456", subreddit="entrepreneur")

# 生成 playbook（需要 OPENAI_API_KEY）
result = generate_subreddit_playbook("entrepreneur", top_n=3)
print(result["combined_markdown"])
```

---

## 数据来源

| 路径 | API | 限流 | 适用场景 |
|------|-----|------|----------|
| 路径 A | Reddit 公开 JSON (`/search.json?q=author:X`) | 1.2 req/s | 现有帖子、近期活动 |
| 路径 B | Arctic Shift (`arctic-shift.photon-reddit.com`) | 无限流 | 历史存档、已删帖 |

帖子优先走 Arctic Shift（历史完整），回退 Reddit 搜索。评论全走 Arctic Shift（Reddit 评论端点已全面限制）。

---

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENAI_API_KEY` | LLM 网关密钥（flatkey.ai） | 必填（Playbook 生成） |
| `LLM_BASE_URL` | LLM 网关地址 | `https://app.flatkey.ai/v1` |
| `LLM_PLAYBOOK_MODEL` | Playbook 分析模型 | `gpt-5.4-mini` |
| `HTTP_PROXY` | 代理（可选） | 无 |

---

## MCP 接入方式

### 方式 1：本地直连

```json
{
  "mcpServers": {
    "smart-crawler": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/smart-crawler/backend",
      "env": { "OPENAI_API_KEY": "your-key" }
    }
  }
}
```

### 方式 2：远程 HTTP（smartcrawler.io）

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

---

## Demo 数据

10 个 subreddit × top 3 贡献者的采集结果：[reddit_demo.html](../deliverables/reddit_demo.html)

原始 JSON：[reddit_demo.json](../deliverables/reddit_demo.json)
