"""FastAPI 入口 —— 挂载 REST API + 托管前端看板 + 启动调度。"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api.routes import public_router, router as api_router
from .config import FRONTEND_DIR
from .db import init_db


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
    yield


app = FastAPI(
    title="smart-crawler — 遨森标杆数据采集平台",
    version="0.1.0",
    description="P0 三品牌（SONGMICS / Homary / Costway）商品采集 MVP",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(public_router)
app.include_router(api_router)


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
