#!/usr/bin/env bash
# Sync backend code to Mac minis and restart any installed launchd workers.
set -euo pipefail

MINIS=("solvea@100.75.94.90" "solvea@100.72.33.57")

for MINI in "${MINIS[@]}"; do
  echo "==> deploy to $MINI"
  ssh "$MINI" 'mkdir -p ~/smart-crawler/backend ~/smart-crawler/scripts ~/smart-crawler/deploy ~/smart-crawler/logs'
  rsync -az --delete \
    --exclude='.venv' --exclude='data' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='*.env' --exclude='proxies*.txt' \
    --exclude='logs' \
    backend/ "$MINI":~/smart-crawler/backend/
  rsync -az scripts/ "$MINI":~/smart-crawler/scripts/
  rsync -az deploy/ "$MINI":~/smart-crawler/deploy/

  ssh "$MINI" 'set -e; export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"; if [ -d ~/smart-crawler/.venv ]; then \
    cd ~/smart-crawler && . .venv/bin/activate && \
    python -m pip install -q -r backend/requirements.txt && \
    for n in 1 2; do \
      launchctl kickstart -k gui/$(id -u)/io.smartcrawler.worker-$n 2>/dev/null || true; \
    done; \
  fi'
  echo "==> done $MINI"
done
