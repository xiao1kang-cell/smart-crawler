#!/bin/bash
# smart-crawler NAS 部署脚本 - 上线 Reddit + MCP 新工具
# 用法：
#   本地执行：bash scripts/deploy_to_nas.sh
#   或直接在 NAS 上：bash deploy_to_nas.sh

set -e

# ── 配置 ──────────────────────────────────────────────────────────────
IMAC_USER="siliconno3"
IMAC_HOST="192.168.1.87"
NAS_USER="solvea"
NAS_HOST="192.168.1.80"
NAS_PATH="/volume1/docker/smart-crawler/app"
BRANCH="feature/reddit-mcp"

# ── 远程执行的部署命令 ───────────────────────────────────────────────
DEPLOY_CMD="set -e
echo '[1/5] cd $NAS_PATH'
cd $NAS_PATH

echo '[2/5] 拉取最新代码（含 Reddit 分支）'
git fetch origin
git stash push -u -m 'auto-stash-before-redeploy-\$(date +%s)' || echo '  无未提交改动'

echo '[3/5] 切换到 $BRANCH 分支'
git checkout $BRANCH
git pull origin $BRANCH

echo '[4/5] 重建 Docker 容器'
docker compose up -d --build

echo '[5/5] 等服务起来 + 健康检查'
sleep 8
docker compose ps
echo '---'
echo 'MCP endpoint 测试：'
curl -s -o /dev/null -w 'HTTP %{http_code}\\n' \
  -X POST http://localhost:8077/mcp/ \
  -H 'Authorization: Bearer test' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}'

echo '✅ 部署完成。回滚：cd $NAS_PATH && git checkout main && docker compose up -d --build'
"

# ── 执行 ──────────────────────────────────────────────────────────────
echo "🚀 部署 feature/reddit-mcp 到 NAS Docker..."
echo "   SSH 链: $IMAC_USER@$IMAC_HOST → $NAS_USER@$NAS_HOST"
echo "   目标: $NAS_PATH"
echo "   分支: $BRANCH"
echo ""

# 通过 iMac 跳板到 NAS
ssh -t "$IMAC_USER@$IMAC_HOST" "ssh -t $NAS_USER@$NAS_HOST \"$DEPLOY_CMD\""
