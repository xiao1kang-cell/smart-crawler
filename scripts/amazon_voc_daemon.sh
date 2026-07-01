#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
APP_ENV="${APP_ENV:-production}"
API_URL="${API_URL:-http://127.0.0.1:8077}"
LOG_DIR="${LOG_DIR:-$ROOT/logs/amazon_voc}"
PID_FILE="${PID_FILE:-$LOG_DIR/daemon.pid}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/daemon.out.log}"
ACTION="${1:-status}"

mkdir -p "$LOG_DIR"

pid_value() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d '[:space:]' < "$PID_FILE"
  fi
}

is_running() {
  local pid
  pid="$(pid_value)"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

start_daemon() {
  if is_running; then
    echo "amazon voc daemon already running pid=$(pid_value)"
    echo "log=$LOG_FILE"
    exit 0
  fi

  if [[ -f "$PID_FILE" ]]; then
    echo "removing stale pid file: $PID_FILE"
    rm -f "$PID_FILE"
  fi

  cd "$BACKEND"
  nohup env \
    APP_ENV="$APP_ENV" \
    "$PYTHON" -m app.crawlers.amazon_daemon --api-url "$API_URL" >> "$LOG_FILE" 2>&1 &

  echo "$!" > "$PID_FILE"
  sleep 1

  if is_running; then
    echo "started amazon voc daemon pid=$(pid_value) env=$APP_ENV api_url=$API_URL"
    echo "pid_file=$PID_FILE"
    echo "log=$LOG_FILE"
  else
    echo "amazon voc daemon failed to start; recent log:"
    tail -n 80 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
  fi
}

stop_daemon() {
  if ! is_running; then
    echo "amazon voc daemon is not running"
    rm -f "$PID_FILE"
    exit 0
  fi

  local pid
  pid="$(pid_value)"
  echo "stopping amazon voc daemon pid=$pid"
  kill -TERM "$pid" >/dev/null 2>&1 || true

  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "stopped amazon voc daemon"
      exit 0
    fi
    sleep 1
  done

  echo "daemon did not stop after 20s; sending KILL pid=$pid"
  kill -KILL "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "stopped amazon voc daemon"
}

status_daemon() {
  if is_running; then
    echo "amazon voc daemon running pid=$(pid_value)"
    echo "env=$APP_ENV api_url=$API_URL"
    echo "pid_file=$PID_FILE"
    echo "log=$LOG_FILE"
  else
    echo "amazon voc daemon not running"
    if [[ -f "$PID_FILE" ]]; then
      echo "stale pid_file=$PID_FILE pid=$(pid_value)"
    fi
    exit 1
  fi
}

show_logs() {
  local lines="${2:-120}"
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "log file does not exist: $LOG_FILE"
    exit 1
  fi
  tail -n "$lines" "$LOG_FILE"
}

tail_logs() {
  touch "$LOG_FILE"
  tail -f "$LOG_FILE"
}

run_foreground() {
  cd "$BACKEND"
  exec env APP_ENV="$APP_ENV" "$PYTHON" -m app.crawlers.amazon_daemon --api-url "$API_URL"
}

usage() {
  cat <<EOF
Usage:
  $0 start
  $0 stop
  $0 restart
  $0 status
  $0 logs [lines]
  $0 tail
  $0 run

Environment:
  APP_ENV=production|test      default: production
  API_URL=http://host:8077     default: http://127.0.0.1:8077
  PYTHON=/path/to/python       default: $ROOT/.venv/bin/python
  LOG_DIR=/path/to/logs        default: $ROOT/logs/amazon_voc
  PID_FILE=/path/to/pid        default: $ROOT/logs/amazon_voc/daemon.pid
  LOG_FILE=/path/to/log        default: $ROOT/logs/amazon_voc/daemon.out.log
EOF
}

case "$ACTION" in
  start)
    start_daemon
    ;;
  stop)
    stop_daemon
    ;;
  restart)
    stop_daemon
    start_daemon
    ;;
  status)
    status_daemon
    ;;
  logs)
    show_logs "$@"
    ;;
  tail)
    tail_logs
    ;;
  run)
    run_foreground
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
