# @smart-crawler/mcp

Install helper for the smart-crawler Agent-first MCP server.

```bash
npx -y @smart-crawler/mcp install --client codex --env-var SMARTCRAWLER_API_KEY
npx -y @smart-crawler/mcp install --client claude --url https://smartcrawler.io/mcp
npx -y @smart-crawler/mcp install --client cursor --local
```

The helper prints copy/paste configuration only. It never stores or prints the
actual API key.

Primary tools:

- `query_warehouse(intent, limit)` — warehouse-first, 0 credits.
- `scrape_url(url)` — one-URL scrape with 5-minute agent memory.
- `crawl_site(url, dry_run=true)` — validate a full crawl before spending credits.
