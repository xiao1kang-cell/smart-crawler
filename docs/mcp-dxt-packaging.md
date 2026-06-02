# Claude Desktop .dxt Packaging Notes

The first public release should ship a Claude Desktop `.dxt` package alongside
the HTTP MCP endpoint.

## Target behavior

- User installs the package.
- Package asks for a `SMARTCRAWLER_API_KEY`.
- Package registers `https://smartcrawler.io/mcp`.
- Tool descriptions emphasize `query_warehouse(intent)` before `scrape_url(url)`.

## Manifest draft

```json
{
  "name": "smart-crawler",
  "display_name": "smart-crawler",
  "version": "0.1.0",
  "description": "Agent-first ecommerce crawler with warehouse-first search, memory, and cost-aware MCP tools.",
  "server": {
    "type": "http",
    "url": "https://smartcrawler.io/mcp",
    "headers": {
      "Authorization": "Bearer ${SMARTCRAWLER_API_KEY}"
    }
  }
}
```

## Release checklist

- Rotate any historical API/proxy credentials before public distribution.
- Verify `query_warehouse`, `scrape_url`, and `crawl_site dry_run=true` in Claude.
- Include the 50-task benchmark summary in the package gallery/readme.
