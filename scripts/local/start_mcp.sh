#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/backend"
SESSION="${SMARTCRAWLER_SCREEN_SESSION:-smart-crawler-local}"
PORT="${SMARTCRAWLER_PORT:-8077}"
LOG="${SMARTCRAWLER_LOG:-/tmp/smart-crawler-local.screen.log}"
KEY_NAME="${SMARTCRAWLER_KEY_NAME:-codex-local}"
MCP_NAME="${SMARTCRAWLER_MCP_NAME:-smart-crawler-local}"

cd "$BACKEND"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing backend/.venv. Create it first:"
  echo "  cd $BACKEND && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt pytest"
  exit 1
fi

API_KEY="${SMARTCRAWLER_LOCAL_API_KEY:-}"
if [[ -z "$API_KEY" ]]; then
  API_KEY="$(launchctl getenv SMARTCRAWLER_LOCAL_API_KEY 2>/dev/null || true)"
fi
if [[ -z "$API_KEY" && -f "$HOME/.zshrc" ]]; then
  API_KEY="$(grep -E '^export SMARTCRAWLER_LOCAL_API_KEY=' "$HOME/.zshrc" | tail -1 | sed -E 's/^export SMARTCRAWLER_LOCAL_API_KEY=//; s/^'\''//; s/'\''$//; s/^"//; s/"$//' || true)"
fi
if [[ -z "$API_KEY" ]]; then
  API_KEY="$(".venv/bin/python" - <<PY
from app.db import init_db, SessionLocal
from app.access import DEFAULT_API_KEY_SCOPES
from app.apikey import generate, hash_key, short
from app.models import ApiKey

init_db()
with SessionLocal() as db:
    raw = generate()
    k = ApiKey(name="$KEY_NAME", key_prefix=short(raw), key_hash=hash_key(raw),
               scopes=DEFAULT_API_KEY_SCOPES)
    db.add(k)
    db.commit()
    print(raw)
PY
)"
  if ! grep -q '^export SMARTCRAWLER_LOCAL_API_KEY=' "$HOME/.zshrc" 2>/dev/null; then
    {
      echo ""
      echo "# smart-crawler local MCP API key"
      printf "export SMARTCRAWLER_LOCAL_API_KEY=%q\n" "$API_KEY"
    } >> "$HOME/.zshrc"
  fi
fi
launchctl setenv SMARTCRAWLER_LOCAL_API_KEY "$API_KEY" >/dev/null 2>&1 || true

if ! command -v screen >/dev/null 2>&1; then
  echo "screen is required on local macOS. Install or start uvicorn manually."
  exit 1
fi

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "smart-crawler local server already listening on port $PORT"
else
  screen -S "$SESSION" -X quit >/dev/null 2>&1 || true
  screen -dmS "$SESSION" zsh -lc "cd '$BACKEND' && RUN_SCHEDULER=0 ADMIN_USERNAME=admin .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port '$PORT' >> '$LOG' 2>&1"
fi

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null

if command -v codex >/dev/null 2>&1; then
  if codex mcp get "$MCP_NAME" >/dev/null 2>&1; then
    python3 - <<PY
from pathlib import Path
path = Path.home() / ".codex" / "config.toml"
text = path.read_text()
section = "[mcp_servers.$MCP_NAME]"
start = text.index(section)
next_start = text.find("\\n[", start + len(section))
block = text[start:] if next_start == -1 else text[start:next_start]
rest = "" if next_start == -1 else text[next_start:]
lines = []
for line in block.splitlines():
    if line.strip().startswith("bearer_token_env_var"):
        continue
    if line.strip().startswith("http_headers"):
        continue
    lines.append(line)
lines.append('http_headers = { Authorization = "Bearer $API_KEY" }')
path.write_text(text[:start] + "\\n".join(lines) + "\\n" + rest.lstrip("\\n"))
PY
  else
    codex mcp add "$MCP_NAME" --url "http://127.0.0.1:$PORT/mcp" --bearer-token-env-var SMARTCRAWLER_LOCAL_API_KEY >/dev/null
  fi
fi

echo "smart-crawler local MCP is ready"
echo "  health: http://127.0.0.1:$PORT/health"
echo "  mcp:    http://127.0.0.1:$PORT/mcp"
echo "  screen: $SESSION"
echo "  log:    $LOG"
