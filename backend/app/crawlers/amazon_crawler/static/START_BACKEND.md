# 后台启动说明（Admin/API）

本文档说明如何启动当前项目的后台服务，并访问 `static` 下的管理页面。

## 1) 环境准备

- Python 3.10+（建议）
- 在项目根目录执行（即包含 `requirements.txt` 的目录）

安装依赖：

```bash
pip install -r requirements.txt
```

## 2) 管理员账号（登录页使用）

后台读取以下环境变量：

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`
- `ADMIN_SESSION_TTL_SECONDS`（可选）

示例（macOS/zsh）：

```bash
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=@admin
export ADMIN_SESSION_SECRET=change-me-session-secret
export ADMIN_SESSION_TTL_SECONDS=43200
```

> 说明：当前代码默认账号为 `admin`，默认密码为 `@admin`。生产环境务必改成强密码。

## 3) 启动后台 API

在项目根目录执行：

```bash
python -m amazon_crawler.task_api
```

默认监听：`0.0.0.0:8000`

Windows 也可以直接双击或执行：

```bat
start_api.bat
```

## 4) 访问地址

服务启动后，在浏览器打开：

- 登录页：`http://127.0.0.1:8000/static/admin_login.html`
- 任务导入页：`http://127.0.0.1:8000/task-import`
- 账号导入页：`http://127.0.0.1:8000/account-import`
- 仪表页：`http://127.0.0.1:8000/dashboard`

> `task-import` 和 `account-import` 未登录会自动跳转到登录页。

## 5) 常见问题

### Q1: 页面打不开 / 404

- 确认命令是在项目根目录执行。
- 确认启动的是 `python -m amazon_crawler.task_api`，不是根目录的 `main.py`。

### Q2: 登录 401

- 检查 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 是否和登录输入一致。
- 如果改过环境变量，重启 API 进程后再登录。

### Q3: 端口冲突

- 当前代码默认端口写死为 `8000`。
- 如需改端口，可在 `amazon_crawler/task_api.py` 的 `uvicorn.run(..., port=8000)` 调整。
