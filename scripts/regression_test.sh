#!/bin/bash
# 回归测试 · 自动跑所有关键端点，输出 pass/fail
# 用法：SMARTCRAWLER_API_KEY=sck_... bash scripts/regression_test.sh [TOKEN]

set -uo pipefail
KEY="${SMARTCRAWLER_API_KEY:-${API_KEY:-}}"
BASE="${SMARTCRAWLER_BASE_URL:-https://smartcrawler.io}"
TOKEN="${1:-${SMARTCRAWLER_TOKEN:-${TOKEN:-}}}"

if [ -z "$KEY" ]; then
  echo "Set SMARTCRAWLER_API_KEY or API_KEY before running regression tests."
  exit 2
fi

if [ -z "$TOKEN" ] && [ -n "${SMARTCRAWLER_ADMIN_USERNAME:-}" ] && [ -n "${SMARTCRAWLER_ADMIN_PASSWORD:-}" ]; then
  TOKEN=$(curl -s -X POST "$BASE/api/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"$SMARTCRAWLER_ADMIN_USERNAME\",\"password\":\"$SMARTCRAWLER_ADMIN_PASSWORD\"}" 2>/dev/null \
    | python3 -c "import json,sys;print(json.load(sys.stdin).get('token',''))")
fi

if [ -z "$TOKEN" ]; then
  echo "Pass a bearer token as argv[1], set SMARTCRAWLER_TOKEN, or set SMARTCRAWLER_ADMIN_USERNAME/SMARTCRAWLER_ADMIN_PASSWORD."
  exit 2
fi

PASS=0
FAIL=0
RESULTS=()

# 测试函数
test_endpoint() {
  local name="$1" url="$2" auth_type="$3" expect="${4:-200}"
  local code
  if [ "$auth_type" = "apikey" ]; then
    code=$(curl -s -o /tmp/regr_out -w '%{http_code}' -H "X-API-Key: $KEY" "$url" --max-time 30)
  elif [ "$auth_type" = "bearer" ]; then
    code=$(curl -s -o /tmp/regr_out -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$url" --max-time 30)
  else
    code=$(curl -sL -o /tmp/regr_out -w '%{http_code}' "$url" --max-time 30)
  fi
  if [ "$code" = "$expect" ]; then
    echo "  ✅ $name: HTTP $code"
    PASS=$((PASS+1))
    RESULTS+=("PASS|$name|$code")
  else
    echo "  ❌ $name: HTTP $code (期望 $expect)"
    FAIL=$((FAIL+1))
    RESULTS+=("FAIL|$name|$code")
  fi
}

echo "═══════════════════════════════════════════════"
echo "  smart-crawler 回归测试 · $(date '+%Y-%m-%d %H:%M')"
echo "═══════════════════════════════════════════════"

echo ""
echo "▎核心 API 端点"
test_endpoint "GET /api/coverage" "$BASE/api/coverage" apikey
test_endpoint "GET /api/sites" "$BASE/api/sites" apikey
test_endpoint "GET /api/categories/cross" "$BASE/api/categories/cross?sites=costway_us" bearer
test_endpoint "GET /api/proxy/status" "$BASE/api/proxy/status" apikey

echo ""
echo "▎计费端点（新增）"
test_endpoint "GET /api/billing/usage" "$BASE/api/billing/usage" bearer

echo ""
echo "▎导出端点（4 格式）"
test_endpoint "Export xlsx 单站" "$BASE/api/export/products?token=$TOKEN&site=costway_uk" none
test_endpoint "Export csv" "$BASE/api/export/products?token=$TOKEN&site=costway_de&format=csv" none
test_endpoint "Export json" "$BASE/api/export/products?token=$TOKEN&site=costway_de&format=json" none
test_endpoint "Export zip 多站" "$BASE/api/export/products?token=$TOKEN&sites=costway_uk%7Ccostway_de&format=zip" none
test_endpoint "Export 含 toggle" "$BASE/api/export/products?token=$TOKEN&site=costway_uk&include_price_history=true&include_voc=true&split_by_category=true" none

echo ""
echo "▎预览端点"
test_endpoint "Export preview" "$BASE/api/export/preview?token=$TOKEN&sites=costway_us&include_price_history=true" none

echo ""
echo "▎MCP endpoint"
test_endpoint "POST /mcp/ (401 unauthorized)" "$BASE/mcp/" none 401

echo ""
echo "▎前端入口"
test_endpoint "GET /app（dashboard）" "$BASE/app" none
test_endpoint "GET /（landing）" "$BASE/" none
test_endpoint "GET /favicon.svg" "$BASE/favicon.svg" none
test_endpoint "GET /llms.txt" "$BASE/llms.txt" none

echo ""
echo "▎可分享文档链接"
test_endpoint "战略 v2 抽卡" "https://cdn.statically.io/gh/mguozhen/smart-crawler/feature/customer-design-cards/deliverables/strategy_v2.html" none
test_endpoint "品牌 v3 设计" "https://cdn.statically.io/gh/mguozhen/smart-crawler/feature/customer-design-cards/deliverables/brand_v3_design.html" none
test_endpoint "Outreach hub" "https://cdn.statically.io/gh/mguozhen/smart-crawler/feature/customer-design-cards/deliverables/customer_outreach/index.html" none

echo ""
echo "▎数据合理性"
SKU=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/coverage" --max-time 8 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['summary']['total_current_sku'])")
echo "  📊 总 SKU: $SKU"
if [ "$SKU" -gt 90000 ]; then
  echo "  ✅ SKU > 90,000（健康）"
  PASS=$((PASS+1))
  RESULTS+=("PASS|总 SKU 数 ($SKU)|≥90k")
else
  echo "  ❌ SKU < 90,000（异常）"
  FAIL=$((FAIL+1))
  RESULTS+=("FAIL|总 SKU 数 ($SKU)|<90k")
fi

VIDAXL=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/sites" --max-time 8 2>/dev/null | python3 -c "
import json,sys;t=sum(s.get('sku_count',0) for s in json.load(sys.stdin) if s['site'].startswith('vidaxl'));print(t)")
echo "  📊 Vidaxl SKU: $VIDAXL"
if [ "$VIDAXL" -gt 5000 ]; then
  echo "  ✅ Vidaxl SKU > 5,000"
  PASS=$((PASS+1))
  RESULTS+=("PASS|Vidaxl SKU ($VIDAXL)|≥5k")
else
  echo "  ⚠️ Vidaxl SKU < 5,000"
fi

PROXIES=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/proxy/status" --max-time 8 2>/dev/null | python3 -c "
import json,sys;d=json.load(sys.stdin);print(sum(1 for p in d['details'] if p['fail_count']+p['success_count']>0))")
echo "  📊 代理使用数: $PROXIES/10"
if [ "$PROXIES" -ge 5 ]; then
  echo "  ✅ 代理池均衡使用（≥5/10）"
  PASS=$((PASS+1))
  RESULTS+=("PASS|代理使用 ($PROXIES/10)|≥5")
else
  echo "  ⚠️ 代理使用 < 5（粘性 bug?）"
fi

# Worker 健康度（综合：有 running job 或 SKU 增长 = 健康）
# 注：重抓走 dedup，可能 SKU 不增但 jobs 在跑，不能只看 SKU 增量
QUEUE=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/jobs?limit=300" --max-time 8 2>/dev/null)
RUNNING=$(echo "$QUEUE" | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin); items=d if isinstance(d,list) else d.get('items',[])
  print(sum(1 for j in items if j.get('status')=='running'))
except Exception:
  print(0)")
SUCCESS=$(echo "$QUEUE" | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin); items=d if isinstance(d,list) else d.get('items',[])
  print(sum(1 for j in items if j.get('status')=='success'))
except Exception:
  print(0)")
SKU1=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/coverage" --max-time 8 2>/dev/null | python3 -c "
import json,sys;print(json.load(sys.stdin)['summary']['total_current_sku'])")
sleep 30
SKU2=$(curl -s -H "X-API-Key: $KEY" "$BASE/api/coverage" --max-time 8 2>/dev/null | python3 -c "
import json,sys;print(json.load(sys.stdin)['summary']['total_current_sku'])")
DELTA=$((SKU2-SKU1))
echo "  📊 Worker: running=$RUNNING success=$SUCCESS 30s 增量=+$DELTA SKU"
# 健康 = (有 running) OR (SKU 增长) OR (历史 success > 50)
if [ "$RUNNING" -ge 1 ] || [ "$DELTA" -ge 10 ] || [ "$SUCCESS" -ge 50 ]; then
  echo "  ✅ Worker 健康"
  PASS=$((PASS+1))
  RESULTS+=("PASS|Worker (running=$RUNNING success=$SUCCESS Δ=+$DELTA)|健康")
else
  echo "  ⚠️ Worker 可能挂了（无 running，无 SKU 增长，无最近 success）"
  FAIL=$((FAIL+1))
  RESULTS+=("FAIL|Worker (running=0 Δ=+$DELTA)|挂?")
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  结果汇总"
echo "═══════════════════════════════════════════════"
TOTAL=$((PASS+FAIL))
echo "  ✅ PASS: $PASS / $TOTAL"
echo "  ❌ FAIL: $FAIL / $TOTAL"
if [ $FAIL -eq 0 ]; then
  echo "  🎉 全部通过！"
  exit 0
else
  echo "  ⚠️ 有失败项，详见上方"
  exit 1
fi
