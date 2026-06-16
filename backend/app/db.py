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

    with _init_lock() as conn:
        if conn is None:
            Base.metadata.create_all(bind=engine)
            _migrate()
        else:
            Base.metadata.create_all(bind=conn)
            _migrate(bind=conn)
            conn.commit()
        _seed_sites()
        _seed_workspaces()
        _seed_users()
        _seed_workspace_sites()
        _backfill_workspace_links()
        _seed_proxy_config()


@contextmanager
def _init_lock():
    """Serialize startup DDL across web and worker containers.

    PostgreSQL can still race on concurrent CREATE TABLE IF NOT EXISTS for new
    tables because the underlying type/index rows are created separately. A
    session-scoped advisory lock keeps deployments boring when multiple
    processes start at once. DDL is committed before seed steps so their normal
    sessions can see newly-created tables. SQLite runs in-process for local dev/tests.
    """
    if IS_SQLITE:
        yield None
        return

    from sqlalchemy import text

    lock_key = 740731551
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        conn.commit()
        try:
            yield conn
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
            conn.commit()


def _migrate(bind=None) -> None:
    """幂等迁移：给已存在的表补上模型新增的列。

    用 ANSI `ALTER TABLE ADD COLUMN`，SQLite / PostgreSQL 均兼容；
    靠 inspect 判重避免重复加列。
    """
    if bind is not None:
        _migrate_with_connection(bind)
        return

    with engine.begin() as conn:
        _migrate_with_connection(conn)


def _migrate_with_connection(conn) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(conn)
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
    if insp.has_table("users"):
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_unique "
            "ON users (email) WHERE email IS NOT NULL"))
    if insp.has_table("user_sessions"):
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sessions_session_hash_unique "
            "ON user_sessions (session_hash)"))
    if insp.has_table("invite_codes"):
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_invite_codes_code_hash_unique "
            "ON invite_codes (code_hash)"))


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
    """初始化管理员账号 —— 用户名/密码由环境变量驱动，杜绝弱口令。

    ADMIN_USERNAME（默认 admin）、ADMIN_PASSWORD。ADMIN_PASSWORD 只在
    *首次创建* 账号（或账号意外缺失 password_hash）时生效；账号已存在后，
    每次启动都重置密码会把用户在控制台里改过的密码覆盖掉，所以默认不再同步。
    需要找回/重置密码时，设 ADMIN_PASSWORD_FORCE_RESET=1 显式触发一次重置。
    未设 ADMIN_PASSWORD 则首次随机生成并打日志。
    """
    import os
    import secrets

    from .auth import hash_password
    from .models import User, Workspace, WorkspaceMember

    username = os.environ.get("ADMIN_USERNAME", "admin")
    email = os.environ.get("ADMIN_EMAIL", f"{username}@local.smartcrawler").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD")
    force_reset = os.environ.get("ADMIN_PASSWORD_FORCE_RESET", "").strip().lower() in (
        "1", "true", "yes", "on")

    with session_scope() as s:
        workspace = s.query(Workspace).filter(Workspace.slug == "internal").first()
        u = s.query(User).filter(User.username == username).first()
        if u:
            if not u.email:
                u.email = email
            if not u.status:
                u.status = "active"
            if not u.role:
                u.role = "admin"
            if not u.global_role:
                u.global_role = "super_admin"
            if workspace and not u.default_workspace_id:
                u.default_workspace_id = workspace.id
            # 只在「账号缺密码」或「显式要求重置」时才用环境变量改密，
            # 否则保留用户在控制台里改过的密码，避免每次部署被还原。
            if password and (not u.password_hash or force_reset):
                u.password_hash = hash_password(password)
                if force_reset:
                    print(f"[seed] 已按 ADMIN_PASSWORD_FORCE_RESET 重置管理员密码：{username}")
            if workspace and not s.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id == u.id).first():
                s.add(WorkspaceMember(workspace_id=workspace.id,
                                      user_id=u.id, role="owner"))
            return
        if not password:
            password = secrets.token_urlsafe(12)
            print(f"[seed] 已生成管理员密码：{username} / {password} —— 请记录")
        u = User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            role="admin",
            global_role="super_admin",
            default_workspace_id=workspace.id if workspace else None,
            status="active",
            email_verified=True,
            display_name="管理员",
        )
        s.add(u)
        s.flush()
        if workspace:
            s.add(WorkspaceMember(workspace_id=workspace.id,
                                  user_id=u.id, role="owner"))


def _seed_workspaces() -> None:
    from .models import Workspace

    with session_scope() as s:
        row = s.query(Workspace).filter(Workspace.slug == "internal").first()
        if row:
            if not row.status:
                row.status = "active"
            if not row.type:
                row.type = "internal"
            return
        s.add(Workspace(name="Internal Workspace", slug="internal",
                        type="internal", status="active"))


def _seed_workspace_sites() -> None:
    from .models import Site, Workspace, WorkspaceSite

    with session_scope() as s:
        workspace = s.query(Workspace).filter(Workspace.slug == "internal").first()
        if not workspace:
            return
        existing = {row.site for row in s.query(WorkspaceSite)
                    .filter(WorkspaceSite.workspace_id == workspace.id).all()}
        for idx, site in enumerate(s.query(Site).order_by(Site.id).all()):
            if site.site in existing:
                continue
            s.add(WorkspaceSite(
                workspace_id=workspace.id,
                site=site.site,
                display_name=f"{site.brand} · {site.country}",
                enabled=True,
                hidden=False,
                sort_order=idx,
            ))


def _backfill_workspace_links() -> None:
    from .models import ApiKey, InviteCode, Usage, Workspace

    with session_scope() as s:
        workspace = s.query(Workspace).filter(Workspace.slug == "internal").first()
        if not workspace:
            return
        for k in s.query(ApiKey).filter(ApiKey.workspace_id.is_(None)).all():
            k.workspace_id = workspace.id
        for invite in (s.query(InviteCode)
                       .filter(InviteCode.workspace_id.is_(None),
                               (InviteCode.target_type.is_(None)) |
                               (InviteCode.target_type != "new_workspace"))
                       .all()):
            invite.workspace_id = workspace.id
        for usage in s.query(Usage).filter(Usage.workspace_id.is_(None)).all():
            key = s.get(ApiKey, usage.api_key_id) if usage.api_key_id else None
            usage.workspace_id = key.workspace_id if key and key.workspace_id else workspace.id


def _seed_proxy_config() -> None:
    """首次启动把私有代理文件导入 DB；失败不阻断服务启动。"""
    try:
        from .proxy_config import bootstrap_proxy_config

        with session_scope() as s:
            bootstrap_proxy_config(s)
    except Exception as exc:
        print(f"[seed] 代理配置初始化跳过：{exc}")


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
