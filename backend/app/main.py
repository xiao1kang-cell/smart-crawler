"""FastAPI 入口 —— 挂载 REST API + 托管前端看板 + 启动调度。"""
from __future__ import annotations

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
    try:
        from .scheduler import start_scheduler
        start_scheduler()
    except Exception as exc:                       # 调度模块可选
        print(f"[scheduler] 未启动: {exc}")
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
def dashboard():
    """3-Tab 数据看板。"""
    return FileResponse(FRONTEND_DIR / "index.html")
