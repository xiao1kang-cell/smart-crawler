"""FastAPI 入口 —— 挂载 REST API + 托管前端看板 + 启动调度。"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import public_router, router as api_router
from .api.output import router as v1_router
from .api.v2 import router as v2_router
from .api.discovery import router as discovery_router
from .api.influencer_discover import router as influencer_discover_router
from .api.tracking import router as tracking_router
from .api.admin_spine import router as admin_spine_router
from .config import FRONTEND_DIR, PROJECT_DIR
from .db import init_db
from .mcp_server import mcp

FRONTEND_APP_DIST = PROJECT_DIR / "frontend-app" / "dist"
FRONTEND_APP_INDEX = FRONTEND_APP_DIST / "index.html"
ADMIN_APP_DIST = PROJECT_DIR / "admin-app" / "dist"
ADMIN_APP_INDEX = ADMIN_APP_DIST / "index.html"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# MCP 服务器 ASGI app —— 供 AI Agent 通过 MCP 协议发现/调用能力
_mcp_app = mcp.http_app(path="/")


# MCP 端点 API Key 鉴权 —— 调用需带 `Authorization: Bearer sck_...`（或 X-API-Key）。
# 复用看板「API 接入」生成的 sck_ 密钥体系。发现层（llms.txt / .well-known）不受影响。
async def _mcp_auth(request, call_next):
    from starlette.responses import JSONResponse as _JSON
    from .access import api_key_scopes, find_api_key, raw_key_from_headers
    from .db import SessionLocal
    from .mcp_context import McpApiKeyContext, reset_current_api_key, set_current_api_key
    auth = request.headers.get("authorization", "")
    key = raw_key_from_headers(auth, request.headers.get("x-api-key", ""))
    if key:
        db = SessionLocal()
        try:
            k = find_api_key(db, key)
            if k:
                from datetime import datetime
                k.last_used = datetime.utcnow()
                k.request_count = (k.request_count or 0) + 1
                scopes = api_key_scopes(k)
                ctx = McpApiKeyContext(
                    api_key_id=k.id,
                    name=k.name or k.key_prefix or "api-key",
                    scopes=scopes,
                )
                db.commit()
                token = set_current_api_key(ctx)
                try:
                    return await call_next(request)
                finally:
                    reset_current_api_key(token)
        finally:
            db.close()
    return _JSON({
        "error": "unauthorized",
        "message": "smart-crawler MCP 需要 API Key。请在请求头加 "
                   "`Authorization: Bearer sck_...`，密钥在 "
                   "https://smartcrawler.io/app 的「API 接入」生成。",
    }, status_code=401)


from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
_mcp_app.add_middleware(BaseHTTPMiddleware, dispatch=_mcp_auth)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 进程重启后,把残留 queued/running 的按需抓取 job 重新入队(内存队列已丢)
    try:
        from .ondemand.queue import requeue_pending
        n = requeue_pending()
        if n:
            print(f"[ondemand] 重新入队 {n} 条未完成任务")
    except Exception as exc:
        print(f"[ondemand] requeue 跳过: {exc}")
    # 单机模式（RUN_SCHEDULER!=0）：进程内起调度 + worker 线程，开箱即用。
    # 服务化部署时 web 容器设 RUN_SCHEDULER=0，调度/worker 由独立容器承担。
    if os.environ.get("RUN_SCHEDULER", "1") != "0":
        try:
            from .scheduler import start_scheduler
            start_scheduler()
        except Exception as exc:
            print(f"[scheduler] 未启动: {exc}")
        try:
            from .worker import run_loop
            n_workers = int(os.environ.get("WORKER_THREADS", "1"))
            for i in range(n_workers):
                threading.Thread(target=run_loop, daemon=True,
                                 name=f"sc-worker-{i+1}").start()
            print(f"[worker] 进程内 {n_workers} 个 worker 线程已启动")
        except Exception as exc:
            print(f"[worker] 未启动: {exc}")
    # MCP 服务器生命周期（嵌套）
    async with _mcp_app.lifespan(app):
        yield


app = FastAPI(
    title="smart-crawler — 为 AI Agent 打造的竞品数据采集引擎",
    version="0.1.0",
    description=(
        "跨境电商竞品数据采集引擎，覆盖 9 大家居品牌 46 个独立站 + 21 个评论渠道。\n\n"
        "**AI Agent 推荐用 MCP 接入**：`/mcp`（streamable-http，7 个工具）。\n"
        "REST API 用 `X-API-Key: sck_...` 鉴权。能力总览见 `/agents.json`，"
        "站点简介见 `/llms.txt`。"
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(discovery_router)     # Agent 发现层 (llms.txt / .well-known)
app.include_router(influencer_discover_router)  # 红人发现 /discover/runs · /discover/datasets
app.include_router(public_router)
app.include_router(api_router)
app.include_router(tracking_router)
app.include_router(v1_router)
app.include_router(v2_router)
app.include_router(admin_spine_router)   # 超管后台 · spine 管理端点
app.mount("/mcp", _mcp_app)              # AI Agent MCP 入口
if (FRONTEND_APP_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_APP_DIST / "assets"), name="frontend-assets")
if (ADMIN_APP_DIST / "assets").exists():
    app.mount("/admin/assets",
              StaticFiles(directory=ADMIN_APP_DIST / "assets"),
              name="admin-assets")


@app.get("/admin")
@app.get("/admin/{path:path}")
def _admin_spa(path: str = ""):
    """超管后台 SPA。No-cache 确保改 UI 后立即生效。"""
    if ADMIN_APP_INDEX.exists():
        return FileResponse(ADMIN_APP_INDEX, headers=NO_CACHE_HEADERS)
    raise HTTPException(404, "admin-app not built")


def _spa_or_legacy(legacy_file):
    target = FRONTEND_APP_INDEX if FRONTEND_APP_INDEX.exists() else legacy_file
    return FileResponse(target, headers=NO_CACHE_HEADERS)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def home():
    """产品主页 / 落地页 —— 面向 AI Agent 的数据采集引擎。"""
    return FileResponse(FRONTEND_DIR / "home.html")


@app.get("/app")
@app.get("/app/{path:path}")
def dashboard(path: str = ""):
    """数据看板控制台。No-cache 确保改 UI 后立即生效。"""
    return _spa_or_legacy(FRONTEND_DIR / "index.html")


@app.get("/favicon.svg")
def favicon():
    return FileResponse(FRONTEND_DIR / "favicon.svg")


@app.get("/report")
def report():
    """站点报表（还原 PDF report 完整内容）。"""
    return _spa_or_legacy(FRONTEND_DIR / "report.html")


@app.get("/d/{path:path}")
def deliverables(path: str):
    from pathlib import Path
    from fastapi import HTTPException
    safe = Path(path)
    if ".." in safe.parts or safe.is_absolute():
        raise HTTPException(400, "invalid path")
    base = (PROJECT_DIR / "deliverables").resolve()
    target = (base / safe).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "path escape")
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    media_type = {
        ".html": "text/html; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json",
        ".csv": "text/csv",
        ".txt": "text/plain; charset=utf-8",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type)
