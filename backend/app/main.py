"""FastAPI 入口 —— 挂载 REST API + 托管前端看板 + 启动调度。"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api.routes import public_router, router as api_router
from .api.output import router as v1_router
from .api.discovery import router as discovery_router
from .config import FRONTEND_DIR
from .db import init_db
from .mcp_server import mcp

# MCP 服务器 ASGI app —— 供 AI Agent 通过 MCP 协议发现/调用能力
_mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
            threading.Thread(target=run_loop, daemon=True,
                             name="sc-worker").start()
            print("[worker] 进程内 worker 线程已启动")
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
app.include_router(public_router)
app.include_router(api_router)
app.include_router(v1_router)
app.mount("/mcp", _mcp_app)              # AI Agent MCP 入口


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def home():
    """产品主页 / 落地页 —— 面向 AI Agent 的数据采集引擎。"""
    return FileResponse(FRONTEND_DIR / "home.html")


@app.get("/app")
def dashboard():
    """数据看板控制台。"""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/favicon.svg")
def favicon():
    return FileResponse(FRONTEND_DIR / "favicon.svg")
