#!/bin/bash
# 自动在 mini1-4 上启动 SOCKS5 代理 + 配 proxies.txt + 重启容器
# 用法：bash setup_proxy_pool.sh <mini_ssh_user>
set -e

MINI_USER="${1:-mguozhen}"

MINIS=(
  "100.95.220.89:mini1"
  "100.65.2.60:mini2"
  "100.85.173.119:mini3"
  "100.72.33.57:mini4"
)
PROXY_PORT=18443  # SOCKS5 监听端口（避开 18080 LLM 网关）

echo "═══════════════════════════════════════════════"
echo "  smart-crawler 静态代理池一键部署"
echo "  目标：mini1-4 × SOCKS5 on :$PROXY_PORT"
echo "═══════════════════════════════════════════════"

# Step 1: 从 NAS 视角，SSH 到每台 mini 启动 go-socks5-proxy docker
for entry in "${MINIS[@]}"; do
  ip="${entry%:*}"
  name="${entry##*:}"
  echo ""
  echo "─── $name ($ip) ───"
  ssh -o BatchMode=yes solvea@192.168.1.80 \
    "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new $MINI_USER@$ip 'bash -s' <<EOF
# 杀掉旧 socks5 容器（如有）
docker rm -f sc-socks5 2>/dev/null || true
# 启动 go-socks5-proxy
docker run -d --name sc-socks5 -p $PROXY_PORT:1080 --restart=unless-stopped serjs/go-socks5-proxy
echo '✅ socks5 on port $PROXY_PORT'
docker ps | grep sc-socks5
EOF
" 2>&1 | tail -5
done

# Step 2: 写 proxies.txt
echo ""
echo "─── 更新 backend/proxies.txt ───"
cat > /tmp/proxies_new.txt <<EOF
# 代理池配置 —— app/proxy.py 读取，按 tier 轮换。
# 每行一个代理 URL；用 [residential] / [datacenter] 分段。

[residential]
# Mini 静态代理池（AT&T 静态块 108.95.61.128/26）
socks5://100.95.220.89:$PROXY_PORT     # mini1
socks5://100.65.2.60:$PROXY_PORT       # mini2
socks5://100.85.173.119:$PROXY_PORT    # mini3
socks5://100.72.33.57:$PROXY_PORT      # mini4

[datacenter]
# http://dc-proxy-host:3128
EOF

cp /tmp/proxies_new.txt /Users/guozhen/MailOutbound/smart-crawler/backend/proxies.txt
cat /Users/guozhen/MailOutbound/smart-crawler/backend/proxies.txt

# Step 3: 推到 NAS
echo ""
echo "─── 同步 proxies.txt 到 NAS ───"
tar czf - backend/proxies.txt | ssh -o BatchMode=yes solvea@192.168.1.80 \
  "cd /volume1/docker/smart-crawler/app && tar xzf - && echo '✅ 同步完成'"

# Step 4: 把所有 vidaxl 站的 proxy_tier 改成 residential
echo ""
echo "─── 把 Vidaxl 全 12 站 proxy_tier 改 residential ───"
sed -i.bak 's/, brand: Vidaxl, country: \([A-Z][A-Z]\), url: "\([^"]*\)", platform: vidaxl, proxy_tier: none/, brand: Vidaxl, country: \1, url: "\2", platform: vidaxl, proxy_tier: residential/g' \
  /Users/guozhen/MailOutbound/smart-crawler/backend/sites.yaml
rm -f /Users/guozhen/MailOutbound/smart-crawler/backend/sites.yaml.bak
grep -c "vidaxl.*residential" /Users/guozhen/MailOutbound/smart-crawler/backend/sites.yaml | xargs -I {} echo "  Vidaxl residential 站数: {}"

# Sync
tar czf - backend/sites.yaml | ssh -o BatchMode=yes solvea@192.168.1.80 \
  "cd /volume1/docker/smart-crawler/app && tar xzf - && echo '✅ sites.yaml 同步'"

# Step 5: 重启容器
echo ""
echo "─── 重启 smart-crawler 容器 ───"
ssh -o BatchMode=yes solvea@192.168.1.80 \
  "cd /volume1/docker/smart-crawler/app && docker compose up -d --build smart-crawler 2>&1 | tail -3"

# Step 6: 验证容器能通过代理出去
echo ""
echo "─── 等容器 ready ───"
until ssh -o BatchMode=yes -o LogLevel=ERROR solvea@192.168.1.80 \
  "docker exec smart-crawler curl -s --max-time 3 http://localhost:8077/health" 2>/dev/null | grep -q ok; do
  sleep 3
done
echo "✅ 服务起来"

echo ""
echo "─── 验证代理（容器内 curl 看出口 IP）───"
for entry in "${MINIS[@]}"; do
  ip="${entry%:*}"
  name="${entry##*:}"
  EXIT=$(ssh -o BatchMode=yes -o LogLevel=ERROR solvea@192.168.1.80 \
    "docker exec smart-crawler curl -s -x socks5h://$ip:$PROXY_PORT --max-time 10 https://ifconfig.me 2>/dev/null")
  echo "  $name ($ip) → 出口 IP: ${EXIT:-FAIL}"
done

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ 部署完成 · 代理池就位 · Vidaxl 12 站已切到 residential tier"
echo "  现在 trigger Vidaxl 重采就会走代理池轮换"
echo "═══════════════════════════════════════════════"
