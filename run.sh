#!/usr/bin/env bash
# smart-crawler 一键启动脚本
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "→ 创建 Python 虚拟环境..."
  python3.12 -m venv .venv
  .venv/bin/pip install -q -r backend/requirements.txt
  .venv/bin/playwright install chromium
fi

cd backend
echo "→ 初始化数据库..."
../.venv/bin/python -m app.cli init

PORT="${PORT:-8077}"
echo "→ 启动服务 http://localhost:${PORT} ..."
exec ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
