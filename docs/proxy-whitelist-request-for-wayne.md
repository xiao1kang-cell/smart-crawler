# 代理白名单放行申请 - smart-crawler NAS 生产机

**致 Wayne（杨文汉夫）**  
**申请人：** 王晓康 / smart-crawler 团队  
**日期：** 2026-06-17

## 一句话需求

请把 smart-crawler NAS 生产机的出口 IP **`99.0.84.158`** 加入美国办公室住宅代理的来源白名单，或提供一个不依赖公网来源白名单的强认证访问入口。

当前生产机访问 `108.95.61.130-184` 的 `1080`/`3128` 端口会超时，应用内 55 个住宅代理全部被标记为 `network_timeout`，系统只能降级使用普通 IP 池。

## 当前线上状态

后台代理池诊断（2026-06-17）：

| 池 | 总数 | 可用 | 阻断 | 当前策略 |
|---|---:|---:|---:|---|
| residential | 55 | 0 | 55 | fallback 到 datacenter |
| datacenter | 38 | 38 | 0 | 正常可用 |

住宅池诊断：

- failure code：`network_timeout`
- sample endpoints：`108.95.61.130-134:1080`
- latest detail：线上容器探测 `socks5h` 住宅代理超时，等待代理侧放通后复检

## 问题现象

同一批代理出口 IP：`108.95.61.130-184`

| 测试机器 | 出口 IP | 结果 |
|---|---|---|
| 本地开发机 | `38.70.72.230` | 可以通过代理返回所连出口 IP |
| NAS 生产机 / Docker 容器 | `99.0.84.158` | 连接代理端口超时 |

NAS 直连公网正常，只有连美国办公室代理端口失败。

## 已做诊断

NAS 直连公网：

```bash
curl https://api.ipify.org
# 预期/实测：99.0.84.158
```

NAS 访问代理端口：

```bash
bash -c 'cat </dev/null >/dev/tcp/108.95.61.161/3128'
bash -c 'cat </dev/null >/dev/tcp/108.95.61.161/1080'
bash -c 'cat </dev/null >/dev/tcp/108.95.61.130/3128'
# 实测：连接失败/超时
```

应用内批量探测结果：

```text
residential: total=55 available=0 blocked=55
failure_counts: network_timeout=55
fallback_pool_slug: datacenter
fallback_available_count: 38
```

判断：生产机到代理服务端的 TCP 链路在公网来源侧被拒绝或丢弃，更像来源 IP 白名单、防火墙、路由或办公室代理设备的上游访问控制问题，不像账号密码错误。账号密码错误通常会表现为 `proxy_auth_failed`，目前不是这个码。

## 需要放行的信息

| 项 | 值 |
|---|---|
| 需放行来源 IP | `99.0.84.158` |
| 代理出口 IP | `108.95.61.130-184` |
| SOCKS5 端口 | `1080` |
| HTTP 端口 | `3128` |
| 代理账号 | `proxyuser` |
| 代理密码 | 通过内部安全渠道确认，不写入仓库文档 |
| 使用方 | smart-crawler NAS 生产容器 |

## 放行后的验收方法

宿主机验证：

```bash
export PROXY_USER='proxyuser'
export PROXY_PASS='<内部安全渠道获取>'

curl -x "http://${PROXY_USER}:${PROXY_PASS}@108.95.61.161:3128" \
  --max-time 20 \
  https://api.ipify.org
# 预期：返回 108.95.61.161

curl -x "socks5h://${PROXY_USER}:${PROXY_PASS}@108.95.61.161:1080" \
  --max-time 20 \
  https://api.ipify.org
# 预期：返回 108.95.61.161
```

容器内验证：

```bash
docker exec \
  -e PROXY_USER="$PROXY_USER" \
  -e PROXY_PASS="$PROXY_PASS" \
  smart-crawler python - <<'PY'
import os
from curl_cffi import requests

user = os.environ["PROXY_USER"]
password = os.environ["PROXY_PASS"]
proxy = f"socks5h://{user}:{password}@108.95.61.161:1080"
session = requests.Session(impersonate="chrome")
session.proxies = {"http": proxy, "https": proxy}
print(session.get("https://api.ipify.org", timeout=20).text)
PY
# 预期：返回 108.95.61.161
```

smart-crawler 后台验证：

1. 打开后台 `代理池`。
2. 点击 `检测住宅` 或 `复检不可用`。
3. 预期 residential 池从 `0/55 available` 恢复为至少部分可用。
4. 失败码不再是 `network_timeout`。

## 长期方案建议

`99.0.84.158` 是家宽公网 IP，可能变化。代理设备不在 NAS 的 Tailscale 网络内，因此走 Tailscale 内网入口不可行。建议优先选下面一种长期方案：

1. **公网强认证入口（推荐）**  
   代理端只依赖账号密码/强认证，不再依赖来源 IP 白名单。任何机器配上账号即可用，适合后续更多 worker 或云机器接入，一劳永逸。

2. **放行稳定网段**  
   如果 NAS 的出口 IP 在可预测小网段内，放行网段，减少动态 IP 变化带来的反复中断。

3. **单 IP 白名单（临时）**  
   先放行 `99.0.84.158` 快速打通；但家宽 IP 漂移后会再次中断，仅作过渡。

## 背景

smart-crawler 生产采集需要住宅出口分散请求。近期强反爬站点在单一 NAS 出口上容易触发 403/429。系统已经实现代理池、住宅池 fallback、后台批量导入和健康诊断；当前唯一卡点是生产机无法连通这 55 个住宅代理出口。

放通后，后台会自动把住宅池从 `down/network_timeout` 恢复为可用，相关站点可以按规则优先使用住宅池，普通 IP 池继续作为 fallback。
