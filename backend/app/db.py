"""SQLAlchemy engine / session。

由环境变量 DATABASE_URL 驱动：
  - 未设置        → SQLite（本地开发 / 单机，data/smart_crawler.db）
  - postgresql://… → PostgreSQL（服务化部署，见 docs/架构设计.md）
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATA_DIR

DB_PATH = DATA_DIR / "smart_crawler.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

_kwargs: dict = {"future": True, "pool_pre_ping": True}
if IS_SQLITE:
    # SQLite：同一连接跨线程复用（后台采集线程 + Web 线程）
    _kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL：连接池 + 回收，避免长连接被 DB / 网络中断
    _kwargs["pool_size"] = 5
    _kwargs["max_overflow"] = 10
    _kwargs["pool_recycle"] = 1800

engine = create_engine(DATABASE_URL, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        """WAL 模式 —— 后台采集写入与看板读取并发不互锁。"""
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def init_db() -> None:
    """建表 + 轻量迁移 + 用 sites.yaml 初始化站点。"""
    from . import models  # noqa: F401  保证模型已注册

    Base.metadata.create_all(engine)
    _migrate()
    _seed_sites()
    _seed_users()


def _migrate() -> None:
    """幂等迁移：给已存在的表补上模型新增的列。

    用 ANSI `ALTER TABLE ADD COLUMN`，SQLite / PostgreSQL 均兼容；
    靠 inspect 判重避免重复加列。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                conn.execute(text(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"))


def _seed_sites() -> None:
    from .config import get_sites
    from .models import Site

    with session_scope() as s:
        rows = {row.site: row for row in s.query(Site).all()}
        for cfg in get_sites():
            row = rows.get(cfg["site"])
            if row is None:                       # 新站点 —— 插入
                s.add(Site(
                    site=cfg["site"], brand=cfg["brand"],
                    country=cfg["country"], url=cfg["url"],
                    platform=cfg["platform"],
                    proxy_tier=cfg.get("proxy_tier", "none"),
                ))
            else:                                 # 已存在 —— 同步 yaml 配置
                row.brand = cfg["brand"]
                row.country = cfg["country"]
                row.url = cfg["url"]
                row.platform = cfg["platform"]
                row.proxy_tier = cfg.get("proxy_tier", "none")


def _seed_users() -> None:
    """初始化默认账号：aosen / admin（首次运行时创建）。"""
    from .auth import hash_password
    from .models import User

    with session_scope() as s:
        if s.query(User).filter(User.username == "aosen").first():
            return
        s.add(User(
            username="aosen",
            password_hash=hash_password("admin"),
            role="admin",
            display_name="Aosom 管理员",
        ))


@contextmanager
def session_scope():
    """事务上下文：正常提交，异常回滚。"""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    """FastAPI 依赖注入用。"""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
