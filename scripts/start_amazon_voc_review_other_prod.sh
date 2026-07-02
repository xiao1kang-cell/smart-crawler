#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"

APP_ENV="${APP_ENV:-production}"
API_URL="${API_URL:-http://127.0.0.1:8077}"
COUNT="${COUNT:-3}"
NODE="${AMAZON_VOC_WORKER_NODE:-$(hostname -s 2>/dev/null || hostname)}"
LOG_DIR="$ROOT/logs/amazon_voc"
PID_FILE="$LOG_DIR/review-other.pid"
LOG_FILE="$LOG_DIR/review-other.out.log"

if [[ -d /opt/homebrew/opt/expat/lib ]]; then
  export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:+$DYLD_LIBRARY_PATH:}/opt/homebrew/opt/expat/lib"
fi

mkdir -p "$LOG_DIR"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "amazon voc review-other already running pid=$(cat "$PID_FILE")"
  exit 0
fi

nohup env \
  APP_ENV="$APP_ENV" \
  API_URL="$API_URL" \
  AMAZON_VOC_INIT_DB="${AMAZON_VOC_INIT_DB:-0}" \
  AMAZON_VOC_WORKER_NODE="$NODE" \
  AMAZON_VOC_LISTING_WORKER_PROCESSES=0 \
  AMAZON_VOC_REVIEW_US_WORKER_PROCESSES=0 \
  AMAZON_VOC_REVIEW_NON_US_WORKER_PROCESSES="$COUNT" \
  "$ROOT/.venv/bin/python" -m app.crawlers.amazon_worker >> "$LOG_FILE" 2>&1 &

echo "$!" > "$PID_FILE"
echo "started amazon voc review-other env=$APP_ENV api_url=$API_URL count=$COUNT node=$NODE pid=$(cat "$PID_FILE") log=$LOG_FILE"
