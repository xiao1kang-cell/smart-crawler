#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"

export APP_ENV="${APP_ENV:-production}"
export API_URL="${API_URL:-http://127.0.0.1:8077}"
exec "$ROOT/.venv/bin/python" -m app.crawlers.amazon_worker
