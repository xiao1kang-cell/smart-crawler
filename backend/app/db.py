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

from .envfile import load_env_file
from .config import DATA_DIR

load_env_file()

DB_PATH = DATA_DIR / "smart_crawler.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

_kwargs: dict = {"future": True, "pool_pre_ping": True}
if IS_SQLITE:
    # SQLite：同一连接跨线程复用（后台采集线程 + Web 线程）
    _kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL：连接池 + 回收，避免长连接被 DB / 网络中断。
    # 线上会同时启动多个 worker；默认池子必须克制，否则几十个进程
    # 的空闲连接就足以打满 Postgres max_connections。
    is_worker = bool(os.environ.get("WORKER_ID"))
    _kwargs["pool_size"] = int(os.environ.get(
        "DB_POOL_SIZE", "1" if is_worker else "3"))
    _kwargs["max_overflow"] = int(os.environ.get(
        "DB_MAX_OVERFLOW", "2" if is_worker else "3"))
    _kwargs["pool_timeout"] = int(os.environ.get("DB_POOL_TIMEOUT", "120"))
    _kwargs["pool_recycle"] = int(os.environ.get("DB_POOL_RECYCLE", "1800"))

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
            _rename_legacy_tables()
            Base.metadata.create_all(bind=engine)
            _migrate()
        else:
            _rename_legacy_tables(bind=conn)
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
        locked = False
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        conn.commit()
        locked = True
        try:
            yield conn
        finally:
            if locked:
                try:
                    if conn.in_transaction():
                        conn.rollback()
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                    conn.commit()
                except Exception:
                    conn.invalidate()


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


def _rename_legacy_tables(bind=None) -> None:
    """Rename tables whose first implementation used customer-specific names."""
    if bind is not None:
        _rename_legacy_tables_with_connection(bind)
        return

    with engine.begin() as conn:
        _rename_legacy_tables_with_connection(conn)


def _rename_legacy_tables_with_connection(conn) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if insp.has_table("anker_voc_jobs") and not insp.has_table("amazon_voc_jobs"):
        conn.execute(text("ALTER TABLE anker_voc_jobs RENAME TO amazon_voc_jobs"))


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
    if insp.has_table("proxy_leases"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_proxy_leases_active_endpoint "
            "ON proxy_leases (endpoint_id, expires_at, released_at)"))
        if not IS_SQLITE:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_proxy_leases_open_endpoint_expiry "
                "ON proxy_leases (endpoint_id, expires_at) "
                "WHERE released_at IS NULL"))
    if insp.has_table("crawl_urls"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_urls_failed_product_lookup "
            "ON crawl_urls (site, kind, status, failure_code)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_urls_site_last_seen "
            "ON crawl_urls (site, last_seen_at)"))
        if not IS_SQLITE:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_crawl_urls_site_seen_progress "
                "ON crawl_urls (site, last_seen_at) INCLUDE (status, attempts)"))
    if not IS_SQLITE and insp.has_table("products"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_products_site_listing_key "
            "ON products (site, (COALESCE(spu, sku)))"))
    if not IS_SQLITE and insp.has_table("crawl_urls"):
        product_site_url_idx = conn.execute(text(
            "SELECT to_regclass('public.ix_crawl_urls_product_site_url')"
        )).scalar()
        if not product_site_url_idx:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_crawl_urls_product_site_url "
                "ON crawl_urls (site, url) "
                "WHERE kind = 'product' AND url IS NOT NULL"))
    if not IS_SQLITE and insp.has_table("crawl_failures"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_failures_site_code_id "
            "ON crawl_failures (site, code, id DESC)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_failures_site_id "
            "ON crawl_failures (site, id DESC)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_failures_site_occurred_code "
            "ON crawl_failures (site, occurred_at DESC, code)"))
    if not IS_SQLITE and insp.has_table("price_history"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_price_history_site_sku_date "
            "ON price_history (site, sku, date)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_price_history_site_sku_review_date "
            "ON price_history (site, sku, date) "
            "WHERE review_count IS NOT NULL"))
    if not IS_SQLITE and insp.has_table("crawl_jobs"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_jobs_created_at_id "
            "ON crawl_jobs (created_at, id)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_jobs_status_created_at "
            "ON crawl_jobs (status, created_at)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_crawl_jobs_assigned_node_status "
            "ON crawl_jobs (assigned_node, status, id)"))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION guard_legacy_30min_crawl_cancel()
            RETURNS trigger AS $$
            BEGIN
              IF OLD.status = 'running'
                 AND NEW.status = 'failed'
                 AND COALESCE(NEW.error, '') LIKE '%stuck running%'
                 AND OLD.site LIKE 'vidaxl_%'
                 AND OLD.started_at IS NOT NULL
                 AND OLD.started_at > (
                     (now() AT TIME ZONE 'UTC') - interval '72 hours'
                 ) THEN
                RETURN OLD;
              END IF;

              IF OLD.status = 'running'
                 AND NEW.status = 'failed'
                 AND COALESCE(NEW.error, '') =
                     'auto-canceled: stuck running >30min'
                 AND OLD.started_at IS NOT NULL
                 AND OLD.started_at > (
                     (now() AT TIME ZONE 'UTC') - interval '12 hours'
                 ) THEN
                RETURN OLD;
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        conn.execute(text(
            "DROP TRIGGER IF EXISTS trg_guard_legacy_30min_crawl_cancel "
            "ON crawl_jobs"))
        conn.execute(text("""
            CREATE TRIGGER trg_guard_legacy_30min_crawl_cancel
            BEFORE UPDATE ON crawl_jobs
            FOR EACH ROW
            EXECUTE FUNCTION guard_legacy_30min_crawl_cancel()
        """))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION normalize_crawl_job_total_count()
            RETURNS trigger AS $$
            BEGIN
              IF NEW.products_count IS NOT NULL
                 AND NEW.products_count > 0
                 AND (
                   NEW.total_product_count IS NULL
                   OR NEW.total_product_count < NEW.products_count
                 ) THEN
                NEW.total_product_count = NEW.products_count;
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        conn.execute(text(
            "DROP TRIGGER IF EXISTS trg_normalize_crawl_job_total_count "
            "ON crawl_jobs"))
        conn.execute(text("""
            CREATE TRIGGER trg_normalize_crawl_job_total_count
            BEFORE INSERT OR UPDATE ON crawl_jobs
            FOR EACH ROW
            EXECUTE FUNCTION normalize_crawl_job_total_count()
        """))
    if insp.has_table("amazon_job_index"):
        if IS_SQLITE:
            indexes = conn.execute(text("PRAGMA index_list('amazon_job_index')")).fetchall()
            needs_rebuild = False
            for idx in indexes:
                idx_name = idx[1]
                if not idx[2]:
                    continue
                cols = [
                    row[2]
                    for row in conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
                ]
                if cols == ["tenant_id", "app_id", "req_ssn"]:
                    needs_rebuild = True
                    break
            if needs_rebuild:
                conn.execute(text("ALTER TABLE amazon_job_index RENAME TO amazon_job_index_old"))
                Base.metadata.tables["amazon_job_index"].create(bind=conn)
                conn.execute(text("""
                    INSERT OR IGNORE INTO amazon_job_index (
                        id, task_id, tenant_id, app_id, req_ssn, job_type, job_pk,
                        table_name, created_at, updated_at
                    )
                    SELECT
                        id, task_id, tenant_id, app_id, req_ssn, job_type, job_pk,
                        table_name, created_at, updated_at
                    FROM amazon_job_index_old
                """))
                conn.execute(text("DROP TABLE amazon_job_index_old"))
        else:
            conn.execute(text("ALTER TABLE amazon_job_index DROP CONSTRAINT IF EXISTS uq_amazon_job_index_req"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_amazon_job_index_req_type "
                "ON amazon_job_index (tenant_id, app_id, req_ssn, job_type)"))
        for table_name in ("amazon_review_jobs", "amazon_listing_jobs", "amazon_voc_jobs"):
            if not insp.has_table(table_name):
                continue
            conn.execute(text(f"""
                INSERT INTO amazon_job_index (
                    task_id, tenant_id, app_id, req_ssn, job_type, job_pk,
                    table_name, created_at, updated_at
                )
                SELECT
                    j.task_id, j.tenant_id, j.app_id, j.req_ssn, j.job_type, j.id,
                    :table_name, j.created_at, j.updated_at
                FROM {table_name} j
                WHERE j.task_id IS NOT NULL
                  AND j.tenant_id IS NOT NULL
                  AND j.app_id IS NOT NULL
                  AND j.req_ssn IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM amazon_job_index i
                      WHERE i.task_id = j.task_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM amazon_job_index i
                      WHERE i.tenant_id = j.tenant_id
                        AND i.app_id = j.app_id
                        AND i.req_ssn = j.req_ssn
                        AND i.job_type = j.job_type
                  )
            """), {"table_name": table_name})
    if insp.has_table("amazon_crawler_accounts"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_amazon_account_platform_state_country "
            "ON amazon_crawler_accounts (platform, state, country)"))
        if not IS_SQLITE:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_amazon_account_platform_username "
                "ON amazon_crawler_accounts (platform, username)"))


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
                    source="yaml",
                ))
            else:                                 # 已存在 —— 同步 yaml 配置
                row.brand = cfg["brand"]
                row.country = cfg["country"]
                row.url = cfg["url"]
                row.platform = cfg["platform"]
                row.proxy_tier = cfg.get("proxy_tier", "none")
                if not row.source:
                    row.source = "yaml"


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
    from .config import get_settings
    from .models import Site, Workspace, WorkspaceSite

    hidden_sites = set(get_settings().get("hidden_sites") or [])
    with session_scope() as s:
        workspace = s.query(Workspace).filter(Workspace.slug == "internal").first()
        if not workspace:
            return
        existing = {row.site: row for row in s.query(WorkspaceSite)
                    .filter(WorkspaceSite.workspace_id == workspace.id).all()}
        for idx, site in enumerate(s.query(Site).order_by(Site.id).all()):
            link = existing.get(site.site)
            if link:
                link.hidden = site.site in hidden_sites
                if not link.display_name:
                    link.display_name = f"{site.brand} · {site.country}"
                continue
            s.add(WorkspaceSite(
                workspace_id=workspace.id,
                site=site.site,
                display_name=f"{site.brand} · {site.country}",
                enabled=True,
                hidden=site.site in hidden_sites,
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
