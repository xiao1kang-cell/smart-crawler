# smart-crawler MCP 本地与线上接入

## 本地 MCP

本地版只用于开发、验证 Codex/Claude 调工具，以及调试 `/api/v2` crawler 能力。
它不会替代 NAS/生产部署。

常用命令：

```bash
./scripts/local/start_mcp.sh
./scripts/local/status_mcp.sh
./scripts/local/stop_mcp.sh
```

启动脚本会做三件事：

- 用 `screen` 在本机启动 `uvicorn app.main:app`，默认端口 `8077`。
- 创建或复用本地 `sck_...` API key，默认 scopes 为 `crawler:read`、`crawler:scrape`。
- 把 Codex MCP server `smart-crawler-local` 指到 `http://127.0.0.1:8077/mcp`。

本地 key 默认不能执行高成本整站采集。`crawl_site(dry_run=true)` 可以验证，
但 `dry_run=false` 需要额外给 key 加 `crawler:crawl` scope。

## 线上 MCP

线上版用于给外部 Claude、Codex、Cursor 或客户系统调用。推荐 endpoint：

```text
https://smartcrawler.io/mcp
```

请求头：

```text
Authorization: Bearer sck_...
```

外部客户默认只发：

- `crawler:read`：查仓库、查 sources、map 已覆盖 URL、查 crawl job。
- `crawler:scrape`：单 URL 抓取、batch scrape、extract、社媒/评论 live 抓取。

只有明确购买或内部授权整站采集时，才发：

- `crawler:crawl`：允许 `crawl_site(dry_run=false)` 入队整站采集。

管理员或内部服务 key 可以使用：

- `admin:*`：绕过 scope 限制。

## Codex 使用

本地：

```bash
./scripts/local/start_mcp.sh
codex mcp get smart-crawler-local
```

线上：

```bash
codex mcp add smart-crawler \
  --url https://smartcrawler.io/mcp \
  --bearer-token-env-var SMARTCRAWLER_API_KEY
```

然后在 shell 里设置：

```bash
export SMARTCRAWLER_API_KEY=sck_xxx
```

如果使用本地脚本生成的配置，`smart-crawler-local` 会把 token 写进
`~/.codex/config.toml` 的 `http_headers`，因此不会再出现
`SMARTCRAWLER_LOCAL_API_KEY is not set`。

## Claude 使用

Claude Desktop / Claude Code 的 MCP 配置核心字段相同：

```json
{
  "mcpServers": {
    "smart-crawler": {
      "type": "http",
      "url": "https://smartcrawler.io/mcp",
      "headers": {
        "Authorization": "Bearer sck_xxx"
      }
    }
  }
}
```

本地调试时把 `url` 改为：

```text
http://127.0.0.1:8077/mcp
```

## Cursor 使用

在 `~/.cursor/mcp.json` 中配置：

```json
{
  "mcpServers": {
    "smart-crawler": {
      "url": "https://smartcrawler.io/mcp",
      "headers": {
        "Authorization": "Bearer sck_xxx"
      }
    }
  }
}
```

## 安全与生产检查

- 不要把真实 API key、proxy 密码、NAS 账号写进仓库。
- 仓库里的 `backend/proxies.txt` 只保留模板。
- NAS/生产代理放在私有文件，并设置 `PROXIES_FILE=/srv/smart-crawler/secrets/proxies.txt`。
- `/api/v2` 默认使用 DB 持久限流，多 worker 共享窗口；本地临时调试可设 `V2_RATE_LIMIT_BACKEND=memory`。
- MCP 工具调用会写入 `usage_records`，endpoint 形如 `/mcp/query_crawler_warehouse`。
- 外部 key 默认不给 `crawler:crawl`，整站采集必须单独授权。
