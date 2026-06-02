#!/usr/bin/env bash
set -euo pipefail

SESSION="${SMARTCRAWLER_SCREEN_SESSION:-smart-crawler-local}"
PORT="${SMARTCRAWLER_PORT:-8077}"
MCP_NAME="${SMARTCRAWLER_MCP_NAME:-smart-crawler-local}"

echo "== smart-crawler local MCP status =="

screen_output="$(screen -ls 2>/dev/null || true)"
if [[ "$screen_output" == *"$SESSION"* ]]; then
  echo "screen: running ($SESSION)"
else
  echo "screen: not running ($SESSION)"
fi

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "port:   listening ($PORT)"
else
  echo "port:   not listening ($PORT)"
fi

if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "health: ok"
else
  echo "health: failed"
fi

if command -v codex >/dev/null 2>&1; then
  echo ""
  echo "== Codex MCP =="
  codex mcp get "$MCP_NAME" || true
else
  echo "codex:  command not found"
fi
