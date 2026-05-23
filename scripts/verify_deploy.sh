#!/bin/bash
# 部署后验证脚本 - 确认 Reddit 工具已上线
set -e

KEY="sck_UYCUvxoUcmtkzNJB6hbUdHtaiFy1Dn9dHJkruvHwR50"
ENDPOINT="https://smartcrawler.io"

echo "═══════════════════════════════════════════════"
echo "  smart-crawler 部署后健康检查"
echo "═══════════════════════════════════════════════"
echo ""

# Test 1: REST API alive
echo "[1/4] REST API 健康"
SITES=$(curl -s -H "X-API-Key: $KEY" "$ENDPOINT/api/sites" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "      ✅ /api/sites returns $SITES sites"
echo ""

# Test 2: MCP discovery
echo "[2/4] MCP 发现层"
curl -s "$ENDPOINT/llms.txt" | head -3
echo ""

# Test 3: MCP tools/list via initialize handshake
echo "[3/4] MCP tools/list (full handshake)"
SESSION_FILE=$(mktemp)

# Initialize session
INIT_RESPONSE=$(curl -s -i -X POST "$ENDPOINT/mcp/" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}')

SESSION_ID=$(echo "$INIT_RESPONSE" | grep -i "^mcp-session-id:" | tr -d '\r' | awk '{print $2}')

if [ -z "$SESSION_ID" ]; then
  echo "      ⚠️  no session ID returned, server may be old"
  echo "$INIT_RESPONSE" | head -20
  exit 1
fi

# Send initialized notification
curl -s -o /dev/null -X POST "$ENDPOINT/mcp/" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'

# List tools
TOOLS_RESPONSE=$(curl -s -X POST "$ENDPOINT/mcp/" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')

# Parse SSE response
TOOL_COUNT=$(echo "$TOOLS_RESPONSE" | grep -oE 'data: \{.*\}' | head -1 | sed 's/^data: //' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tools = d.get('result',{}).get('tools',[])
    names = [t['name'] for t in tools]
    print(len(tools))
    for n in names: print(' • ' + n, file=sys.stderr)
except Exception as e:
    print('parse error', file=sys.stderr)
    print(0)
")

echo "      共 $TOOL_COUNT 个工具"

# Test 4: Specifically check Reddit tools present
echo ""
echo "[4/4] Reddit 工具确认"
if echo "$TOOLS_RESPONSE" | grep -q "reddit_top_contributors"; then
  echo "      ✅ reddit_top_contributors"
else
  echo "      ❌ reddit_top_contributors MISSING"
fi
if echo "$TOOLS_RESPONSE" | grep -q "reddit_user_activity"; then
  echo "      ✅ reddit_user_activity"
else
  echo "      ❌ reddit_user_activity MISSING"
fi
if echo "$TOOLS_RESPONSE" | grep -q "reddit_subreddit_playbook"; then
  echo "      ✅ reddit_subreddit_playbook"
else
  echo "      ❌ reddit_subreddit_playbook MISSING"
fi

echo ""
echo "═══════════════════════════════════════════════"
if [ "$TOOL_COUNT" = "12" ]; then
  echo "  ✅ 部署成功！同事可以直接用 Reddit 工具了"
else
  echo "  ⚠️  工具数 $TOOL_COUNT，预期 12 — 可能需要等容器完全启动"
fi
echo "═══════════════════════════════════════════════"
