#!/usr/bin/env bash
# Install launchd worker plists and per-worker env files on a Mac mini.
#
# Run on the mini after deploy_mini.sh has synced backend/, deploy/, and scripts/.
# Defaults install files only; pass --load after DATABASE_URL/WORKSPACE_ALLOWLIST are
# configured to bootstrap the launchd jobs.
set -euo pipefail

APP="${APP:-/Users/solvea/smart-crawler}"
USER_HOME="${USER_HOME:-/Users/solvea}"
NODE_ID="${NODE_ID:?set NODE_ID, e.g. US-macmini1}"
WORKER_PREFIX="${WORKER_PREFIX:-$NODE_ID}"
WORKER_COUNT="${WORKER_COUNT:-4}"
LOAD=0

if [[ "${1:-}" == "--load" ]]; then
  LOAD=1
fi

TEMPLATE="$APP/deploy/io.smartcrawler.worker.plist"
LAUNCH_AGENTS="$USER_HOME/Library/LaunchAgents"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "missing launchd template: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$APP/logs" "$LAUNCH_AGENTS"

for n in $(seq 1 "$WORKER_COUNT"); do
  env_file="$USER_HOME/.smart-crawler-$n.env"
  plist="$LAUNCH_AGENTS/io.smartcrawler.worker-$n.plist"

  if [[ ! -f "$env_file" ]]; then
    umask 077
    cat > "$env_file" <<EOF
# Filled before starting workers, after NAS PostgreSQL is exposed to Tailscale:
# DATABASE_URL=postgresql+psycopg://sc_worker:<password>@100.116.163.64:5432/smart_crawler
# WORKSPACE_ALLOWLIST=<xiaokang_workspace_id>

RUN_SCHEDULER=0
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
WORKER_ID=$WORKER_PREFIX-$n
NODE_ID=$NODE_ID
WORKER_ASSIGNED_ONLY=1
SNAPSHOT_ENABLED=0
PROXY_LEASE_TTL_SEC=300
LLM_BASE_URL=https://api.flatkey.ai
LLM_MODEL=claude-haiku-4-5
# ANTHROPIC_API_KEY=<key>
EOF
  fi
  chmod 600 "$env_file"

  sed -e "s/__N__/$n/g" \
      -e "s#__ENV_FILE__#$env_file#g" \
      "$TEMPLATE" > "$plist"
  chmod 644 "$plist"
  xattr -c "$plist" >/dev/null 2>&1 || true

  if [[ "$LOAD" == "1" ]]; then
    launchctl bootout "gui/$(id -u)/io.smartcrawler.worker-$n" >/dev/null 2>&1 || true
    launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    launchctl kickstart -k "gui/$(id -u)/io.smartcrawler.worker-$n"
  fi
done

echo "installed $WORKER_COUNT smart-crawler launchd worker plist(s) for $NODE_ID"
if [[ "$LOAD" != "1" ]]; then
  echo "not loaded. Fill DATABASE_URL and WORKSPACE_ALLOWLIST in $USER_HOME/.smart-crawler-*.env, then rerun with --load."
fi
