# smart-crawler 部署到 NAS + 绑定 smartcrawler.io

> 目标：把 smart-crawler 跑在内网 NAS（192.168.1.80）上，通过 Cloudflare Tunnel
> 用 `smartcrawler.io` 对外访问。域名已注册（Cloudflare）。

---

## 0. 网络现状（实测 2026-05-16）

| 主机 | 地址 | 端口 | 说明 |
|------|------|------|------|
| 采集机（本机） | 108.95.61.129（公网静态） | — | AT&T 静态块；**公网 IP，受 UGOS SSH 白名单 DROP，无法直连 NAS SSH** |
| NAS | 192.168.1.80 | :22 关 / :23 telnet 开 / :80 / :443 / :5000 管理面板 | UGREEN UGOS |
| iMac | 192.168.1.87 | :22 SSH 开 / :5900 VNC 开 | RFC1918，**可达 NAS** |

> ⚠ UGOS 的 SSH 仅允许 RFC1918 源地址，本采集机是公网 IP 会被静默 DROP。
> 部署操作须从一台内网机（iMac 192.168.1.87）发起，或直接用 NAS :5000 管理面板。

---

## 1. 部署方式 A —— NAS Docker 面板（推荐，零命令行）

1. 浏览器打开 `http://192.168.1.80:5000` 登录 UGOS
2. 进入「Docker」应用 →「项目 / Compose」→ 新建项目
3. 上传本仓库（或 `git clone`），选择 `docker-compose.yml`
4. 启动 → 容器 `smart-crawler` 跑在 NAS 的 `:8077`
5. 验证：`http://192.168.1.80:8077` 出现登录页，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录

## 2. 部署方式 B —— 命令行（从 iMac 操作）

```bash
# 在 iMac（192.168.1.87）上：
ssh <imac-user>@192.168.1.87
git clone git@github.com:mguozhen/smart-crawler.git
cd smart-crawler
docker compose up -d --build          # NAS 若装了 Docker，也可 scp 过去再起
# → http://<host>:8077
```

镜像不含 Playwright 浏览器（采集主力 curl_cffi）；如后续要采强反爬站，
在容器内 `playwright install chromium` 即可。

---

## 3. 绑定 smartcrawler.io（Cloudflare Tunnel）

域名在 Cloudflare，用 **Cloudflare Tunnel** 把内网服务暴露出去，无需公网端口映射。

1. Cloudflare Zero Trust 控制台 → Networks → Tunnels → Create tunnel
2. 命名 `smart-crawler`，复制 **Tunnel Token**
3. 把 token 写入仓库根目录 `.env`：
   ```
   TUNNEL_TOKEN=eyJh...（粘贴）
   SC_SECRET=<改一个强随机串>
   ```
4. 启用 compose 里的 tunnel 段：
   ```bash
   docker compose --profile tunnel up -d
   ```
5. 在 Tunnel 的 Public Hostname 配置：
   - Hostname: `smartcrawler.io`（及 `www.smartcrawler.io`）
   - Service: `http://smart-crawler:8077`
6. Cloudflare 自动加好 DNS CNAME → 访问 `https://smartcrawler.io` 即看板登录页

> 复用现有 flatkey Cloudflare Tunnel 基础设施亦可：在现有 tunnel 加一条
> Public Hostname `smartcrawler.io → http://192.168.1.80:8077` 即可。

---

## 4. 管理员账号

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ADMIN_USERNAME` | `admin` | 初始管理员用户名 |
| `ADMIN_EMAIL` | `admin@local.smartcrawler` | 初始管理员邮箱 |
| `ADMIN_PASSWORD` | 无 | 初始管理员密码；未设置时首次启动随机生成并打印到日志 |

首次启动 `init_db()` 自动创建管理员。生产部署请显式设置 `ADMIN_PASSWORD`
和强随机 `SC_SECRET`，不要依赖随机日志密码。

改密码（在容器内）：
```bash
docker exec -it smart-crawler python -c "
from app.db import session_scope; from app.models import User
from app.auth import hash_password
with session_scope() as s:
    u=s.query(User).filter(User.username=='admin').first()
    u.password_hash=hash_password('新密码')
"
```

---

## 5. 上线后检查清单

- [ ] `https://smartcrawler.io` 出现登录页，HTTPS 证书正常（Cloudflare 自动签）
- [ ] 管理员登录成功，46 站点列表可见
- [ ] 已设置强随机 `ADMIN_PASSWORD` 和 `SC_SECRET`
- [ ] `data/` 卷已挂载（SQLite 持久化，容器重建不丢数据）
- [ ] 定时调度生效（容器内 APScheduler 自动起）
- [ ] 如需采 Vidaxl：配置 `backend/proxies.txt` 住宅代理（见风控评估报告）
