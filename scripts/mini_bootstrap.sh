#!/usr/bin/env bash
# Prepare a Mac mini for native smart-crawler workers. Run on the mini.
set -euo pipefail

USER_HOME="/Users/solvea"
APP="$USER_HOME/smart-crawler"
BREW="/opt/homebrew/bin/brew"
PY312="/opt/homebrew/bin/python3.12"

if [[ ! -x "$BREW" ]]; then
  echo "Homebrew not found at $BREW" >&2
  exit 1
fi

if [[ ! -x "$PY312" ]]; then
  "$BREW" install python@3.12
fi

"$BREW" list expat >/dev/null 2>&1 || "$BREW" install expat
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

mkdir -p "$APP/backend" "$APP/scripts" "$APP/deploy" "$APP/logs"

if [[ ! -d "$APP/.venv" ]]; then
  "$PY312" -m venv "$APP/.venv"
fi

source "$APP/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$APP/backend/requirements.txt"
python -m playwright install chromium

echo "bootstrap complete: $APP"
