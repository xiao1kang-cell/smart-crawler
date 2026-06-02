# Agent-first crawler implementation notes

## Implemented MVP

This branch turns the crawler surface into a shared Agent-first service layer:

- `backend/app/agent_crawler.py`
  - warehouse-first `scrape_url`
  - `map_site`
  - `crawl_site`
  - `get_crawl_job`
  - `query_warehouse`
  - `extract_structured_data`
  - lightweight live fetch + JSON-LD/metadata/links/markdown extraction

- `backend/app/api/v2.py`
  - `POST /api/v2/scrape`
  - `POST /api/v2/map`
  - `POST /api/v2/crawl`
  - `GET /api/v2/crawl/{job_id}`
  - `POST /api/v2/batch/scrape`
  - `POST /api/v2/extract`
  - `POST /api/v2/query`
  - `GET /api/v2/sources`
  - DB-backed per-path rate limit, with `V2_RATE_LIMIT_BACKEND=memory` for local throwaway runs
  - API-key usage metering into `usage_records`
  - API-key scope enforcement for `crawler:read`, `crawler:scrape`, and `crawler:crawl`

- `backend/app/mcp_server.py`
  - `scrape_url`
  - `map_site`
  - `crawl_site`
  - `get_crawl_job`
  - `extract_structured_data`
  - `query_crawler_warehouse`
  - scope checks and MCP usage metering into `usage_records`

- `scripts/local/start_mcp.sh`
- `scripts/local/stop_mcp.sh`
- `scripts/local/status_mcp.sh`
  - local Codex MCP startup, shutdown, and health checks
  - runs uvicorn in a detached `screen` session
  - updates Codex MCP config for `smart-crawler-local`

## Response contract

Agent-facing responses include:

- `success`
- `usage.credits_used`
- `usage.cache_hit`
- `usage.source`
- `usage.records`
- `warnings[].message`
- `warnings[].next_step`

This lets Claude/Codex decide whether to trust cached data, retry, poll a job,
or ask the user for a supported source.

`crawl_site` defaults to `dry_run=true`. A dry run validates the site and returns
estimated cost, but does not queue a job. Set `dry_run=false` only when the user
explicitly asks to start a full crawl.

## Development tests

Run unit/golden tests from the backend folder:

```bash
cd backend
pytest tests/test_agent_crawler.py
```

Run syntax import smoke:

```bash
python3 -m compileall app
```

Run API smoke after the service is up:

```bash
curl -X POST http://127.0.0.1:8077/api/v2/scrape \
  -H 'Authorization: Bearer sck_xxx' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.songmics.com/","formats":["markdown","structured","links"]}'
```

Run MCP tool listing:

```bash
curl -X POST http://127.0.0.1:8077/mcp \
  -H 'Authorization: Bearer sck_xxx' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Start local Codex MCP:

```bash
./scripts/local/start_mcp.sh
./scripts/local/status_mcp.sh
```

`scripts/local/*` is local-dev only. NAS/production deployment continues to use
the deployment scripts and service config. Full setup notes are in
`docs/mcp-local-and-production.md`.

Stop local Codex MCP:

```bash
./scripts/local/stop_mcp.sh
```

## Claude Code

```bash
claude mcp add --transport http smart-crawler https://smartcrawler.io/mcp \
  --header "Authorization: Bearer sck_xxx" \
  --scope user
```

Prompt:

```text
Use smart-crawler to scrape this product URL and extract title, price, images.
```

## Codex

```bash
export SMARTCRAWLER_API_KEY=sck_xxx
codex mcp add smart-crawler \
  --url https://smartcrawler.io/mcp \
  --bearer-token-env-var SMARTCRAWLER_API_KEY
```

Prompt:

```text
Use smart-crawler to query the warehouse for patio storage products under $100.
```

## Next iteration

- Add LLM schema extraction fallback after JSON-LD/metadata extraction.
- Add per-key monthly quotas on top of the new scopes.
- Add Redis rate limiting if DB-backed one-minute windows become too write-heavy.

## Security close-out

- API keys now carry scopes. Default external scopes are `crawler:read` and `crawler:scrape`.
- Full-site crawl execution requires `crawler:crawl`; `dry_run=true` remains available with `crawler:read`.
- MCP tool calls are metered into `usage_records` with `/mcp/{tool_name}` endpoints.
- `backend/proxies.txt` is now a safe template. Real proxy credentials should live outside the repo and be supplied through `PROXIES_FILE`.
