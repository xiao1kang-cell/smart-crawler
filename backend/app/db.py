"""SQLAlchemy engine / session。MVP 用 SQLite，模型设计预留切 PostgreSQL。"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATA_DIR

DB_PATH = DATA_DIR / "smart_crawler.db"
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(conn, _):
    """WAL 模式 —— 后台采集写入与看板读取并发不互锁。"""
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db() -> None:
    """建表 + 用 sites.yaml 初始化站点。"""
    from . import models  # noqa: F401  保证模型已注册

    Base.metadata.create_all(engine)
    _seed_sites()
    _seed_users()


def _seed_sites() -> None:
    from .config import get_sites
    from .models import Site

    with session_scope() as s:
        existing = {row.site for row in s.query(Site).all()}
        for cfg in get_sites():
            if cfg["site"] in existing:
                continue
            s.add(
                Site(
                    site=cfg["site"],
                    brand=cfg["brand"],
                    country=cfg["country"],
                    url=cfg["url"],
                    platform=cfg["platform"],
                    proxy_tier=cfg.get("proxy_tier", "none"),
                )
            )


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
