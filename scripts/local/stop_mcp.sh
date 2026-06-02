#!/usr/bin/env bash
set -euo pipefail

SESSION="${SMARTCRAWLER_SCREEN_SESSION:-smart-crawler-local}"
PORT="${SMARTCRAWLER_PORT:-8077}"

screen -S "$SESSION" -X quit >/dev/null 2>&1 || true

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN -n -P || true)"
  if [[ -n "$pids" ]]; then
    echo "Stopping process(es) on port $PORT: $pids"
    kill $pids >/dev/null 2>&1 || true
    sleep 1
  fi
fi

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN -n -P || true)"
  if [[ -n "$pids" ]]; then
    echo "Force stopping process(es) on port $PORT: $pids"
    kill -9 $pids >/dev/null 2>&1 || true
    sleep 0.5
  fi
fi

echo "smart-crawler local MCP stopped"
