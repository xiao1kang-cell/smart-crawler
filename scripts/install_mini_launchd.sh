#!/usr/bin/env bash
# Install launchd worker plists and per-worker env files on a Mac mini.
#
# Run on the mini after deploy_mini.sh has synced backend/, deploy/, and scripts/.
# Defaults install files only. Pass --load after DATABASE_URL/WORKSPACE_ALLOWLIST
# are configured to bootstrap missing launchd jobs. --load is non-disruptive for
# already-loaded workers; pass --restart explicitly after draining when plist/env
# changes must take effect.
set -euo pipefail

APP="${APP:-/Users/solvea/smart-crawler}"
USER_HOME="${USER_HOME:-/Users/solvea}"
NODE_ID="${NODE_ID:?set NODE_ID, e.g. US-macmini1}"
WORKER_PREFIX="${WORKER_PREFIX:-$NODE_ID}"
WORKER_COUNT="${WORKER_COUNT:-4}"
LOAD=0
RESTART=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --load)
      LOAD=1
      ;;
    --restart|--force-restart)
      LOAD=1
      RESTART=1
      ;;
    -h|--help)
      cat <<EOF
Usage: NODE_ID=US-macmini1 WORKER_COUNT=6 $0 [--load] [--restart]

  --load      Bootstrap only missing launchd workers; keep running workers alive.
  --restart   Bootout/bootstrap/kickstart workers. Drain jobs before using this.
EOF
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

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
    domain="gui/$(id -u)"
    service="$domain/io.smartcrawler.worker-$n"
    if launchctl print "$service" >/dev/null 2>&1; then
      if [[ "$RESTART" == "1" ]]; then
        echo "restarting loaded worker io.smartcrawler.worker-$n"
        launchctl bootout "$service" >/dev/null 2>&1 || true
        launchctl bootout "$domain" "$plist" >/dev/null 2>&1 || true
        launchctl bootstrap "$domain" "$plist"
        launchctl kickstart -k "$service"
      else
        echo "worker io.smartcrawler.worker-$n already loaded; preserving running process. Use --restart after draining to apply plist/env changes."
      fi
    else
      echo "bootstrapping missing worker io.smartcrawler.worker-$n"
      launchctl bootstrap "$domain" "$plist"
      launchctl kickstart "$service"
    fi
  fi
done

echo "installed $WORKER_COUNT smart-crawler launchd worker plist(s) for $NODE_ID"
if [[ "$LOAD" != "1" ]]; then
  echo "not loaded. Fill DATABASE_URL and WORKSPACE_ALLOWLIST in $USER_HOME/.smart-crawler-*.env, then rerun with --load."
elif [[ "$RESTART" != "1" ]]; then
  echo "--load completed without restarting already-loaded workers. Use --restart only after draining active jobs."
fi
