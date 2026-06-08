# smart-crawler-mcp

Install helper for the smart-crawler Agent-first MCP server.

```bash
npx -y smart-crawler-mcp install --client codex --env-var SMARTCRAWLER_API_KEY
npx -y smart-crawler-mcp install --client claude --url https://smartcrawler.io/mcp
npx -y smart-crawler-mcp install --client cursor --local
npx -y smart-crawler-mcp dxt --env-var SMARTCRAWLER_API_KEY
```

The helper prints copy/paste configuration only. It never stores or prints the
actual API key.

## Connect (Claude Code / Codex)

```bash
export SMARTCRAWLER_API_KEY=sck_xxx   # generate in the smart-crawler console

# Claude Code — the helper prints a `claude mcp add --scope user ...` command.
# --scope user makes the server visible in EVERY project. Without it,
# `claude mcp add` defaults to local scope (current directory only), which is a
# common "I added it but /mcp is empty" pitfall.
npx -y smart-crawler-mcp install --client claude

# Codex
npx -y smart-crawler-mcp install --client codex
```

Run the printed command, then check with `claude mcp list` / `/mcp` (Claude) or
`codex mcp list` (Codex).

## Commands

- `install`: prints Codex / Claude / Cursor MCP config.
- `doctor`: validates the resolved client, server name, endpoint, and env var.
- `dxt`: prints a Claude Desktop `.dxt` manifest draft.

## Local Development

Local mode points at `http://127.0.0.1:8077/mcp`, names the server
`smart-crawler-local`, and defaults the API key env var to
`SMARTCRAWLER_LOCAL_API_KEY`.

```bash
./scripts/local/start_mcp.sh
npx -y smart-crawler-mcp doctor --client codex --local
npx -y smart-crawler-mcp install --client codex --local
```

## Online Setup

```bash
export SMARTCRAWLER_API_KEY=sck_xxx
npx -y smart-crawler-mcp install --client codex
npx -y smart-crawler-mcp install --client claude --json
npx -y smart-crawler-mcp install --client cursor --json
```

Primary tools:

- `query_warehouse(intent, limit)` — warehouse-first, 0 credits.
- `scrape_url(url)` — one-URL scrape with 5-minute agent memory.
- `crawl_site(url, dry_run=true)` — validate a full crawl before spending credits.
