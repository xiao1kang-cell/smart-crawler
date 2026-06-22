"""REST API —— 满足规格 §10 数据接口需求（API-001 ~ API-008）。"""
from __future__ import annotations

import io
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import String, and_, cast, exists, func, or_, text
from sqlalchemy.orm import Session

from ..access import DEFAULT_API_KEY_SCOPES, api_key_scopes, normalize_scopes
from ..audit import record_audit
from ..apikey import generate as gen_key, hash_key, short as key_short
from ..auth import (TOKEN_TTL, generate_session_id, hash_secret, hash_password,
                    make_token, normalize_email, parse_token, validate_email,
                    validate_password_strength, validate_username,
                    verify_password)
from ..currency import (currency_for_site as _currency_for_site,
                        normalize_currency_for_site as _display_currency)
from ..db import get_db
from ..export import export_workbook
from ..models import (ApiKey, Category, CrawlFailure, CrawlJob, CrawlUrl,
                      Keyword, PriceHistory, ProxyHealth,
                      Product, Promotion, Review, ShoppingResult, Site, Trend,
                      User, UserSession, InviteCode, Workspace,
                      WorkspaceMember, WorkspaceSite, ReportConfig)
from ..proxy import pool_status
from ..runner import enqueue


# 新品判定窗口：created_time 落在最近 N 天即视为新品。
# 不要用 Product.is_new 列——它在 pipeline 首次插入时置 True 后从不复位，
# 各站近期全量首采会让 96%+ 商品被误标为新品（2026-06 线上实测）。
NEW_PRODUCT_DAYS = 30


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


CRAWL_JOB_STUCK_SEC = _env_int("CRAWL_JOB_STUCK_SEC", 14400)
CRAWL_JOB_PENDING_STALE_SEC = 7200
CRAWL_JOB_RETRY_STATUSES = {"failed", "blocked", "partial", "skipped"}


def _is_crawl_job_retryable(job: CrawlJob, *, now: datetime | None = None) -> bool:
    status = (job.status or "").lower()
    if status in CRAWL_JOB_RETRY_STATUSES:
        return True
    now = now or datetime.utcnow()
    if status == "running" and job.started_at is not None:
        cutoff = now - timedelta(seconds=CRAWL_JOB_STUCK_SEC)
        return job.started_at < cutoff and (
            job.heartbeat_at is None or job.heartbeat_at < cutoff
        )
    if status == "pending" and job.created_at is not None:
        return job.created_at < now - timedelta(seconds=CRAWL_JOB_PENDING_STALE_SEC)
    return False


# ---------- 鉴权依赖：接受 Bearer Token 或 X-API-Key ----------
def require_user(authorization: str = Header(default=""),
                 x_api_key: str = Header(default="", alias="X-API-Key"),
                 db: Session = Depends(get_db)) -> str:
    """校验登录 Token 或 API 密钥，返回调用者标识；失败 401。"""
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    token_info = parse_token(token)
    if token_info:
        user = _find_user_by_identifier(db, token_info.username)
        if not user or (user.status or "active") != "active":
            raise HTTPException(401, "账号已停用或不存在")
        if token_info.session_id:
            session = (db.query(UserSession)
                       .filter(UserSession.session_hash == hash_secret(token_info.session_id),
                               UserSession.user_id == user.id)
                       .first())
            if (not session or session.revoked_at is not None or
                    (session.expires_at and session.expires_at < datetime.utcnow())):
                raise HTTPException(401, "登录已失效，请重新登录")
        return user.username
    # Firecrawl-compat: Authorization: Bearer sck_... 也走 API key 路径
    candidate_key = x_api_key or token
    if candidate_key:
        k = (db.query(ApiKey)
             .filter(ApiKey.key_hash == hash_key(candidate_key),
                     ApiKey.active.is_(True)).first())
        if k:
            k.last_used = datetime.utcnow()
            k.request_count = (k.request_count or 0) + 1
            db.commit()
            return f"apikey:{k.id}:{k.name}"
    raise HTTPException(401, "未登录或 API 密钥无效")


# 公开路由（登录，不需鉴权）
public_router = APIRouter(prefix="/api")
# 数据路由（全部需登录）
router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _find_user_by_identifier(db: Session, identifier: str | None) -> User | None:
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    email = normalize_email(identifier)
    return (db.query(User)
            .filter(or_(User.username == identifier, User.email == email))
            .first())


def _public_user(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role or "user",
        "global_role": u.global_role,
        "default_workspace_id": u.default_workspace_id,
        "status": u.status or "active",
    }


def _is_admin_user(u: User | None) -> bool:
    return bool(u and (u.role == "admin" or u.role == "owner" or
                       u.global_role == "super_admin"))


def _is_super_admin(u: User | None) -> bool:
    return bool(u and (u.global_role == "super_admin" or
                       (u.username == "admin" and u.role == "admin")))


def _current_user(user: str, db: Session) -> User | None:
    if not user or user.startswith("apikey:"):
        return None
    return _find_user_by_identifier(db, user)


def _require_dashboard_user(user: str, db: Session) -> User:
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能管理账号资源")
    u = _current_user(user, db)
    if not u:
        # Unit tests and old internal scripts sometimes pass a freshly signed
        # admin token before a seeded row exists. Keep that compatibility.
        if user == "admin":
            return User(username="admin", role="admin", status="active",
                        global_role="super_admin",
                        display_name="管理员")
        raise HTTPException(401, "未登录")
    if (u.status or "active") != "active":
        raise HTTPException(403, "账号已停用")
    return u


def _require_admin(user: str, db: Session) -> User:
    u = _require_dashboard_user(user, db)
    if not _is_admin_user(u):
        raise HTTPException(403, "需要管理员权限")
    return u


def _api_key_from_principal(user: str, db: Session) -> ApiKey | None:
    if not user.startswith("apikey:"):
        return None
    try:
        key_id = int(user.split(":", 2)[1])
    except Exception:
        return None
    return db.get(ApiKey, key_id)


def _workspace_response(ws: Workspace) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "slug": ws.slug,
        "type": ws.type or "customer",
        "status": ws.status or "active",
    }


def _slugify_workspace(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "workspace"


def _unique_workspace_slug(db: Session, seed: str) -> str:
    base = _slugify_workspace(seed)
    slug = base
    i = 2
    while db.query(Workspace).filter(Workspace.slug == slug).first():
        slug = f"{base}-{i}"
        i += 1
    return slug


def _unique_workspace_name(db: Session, seed: str) -> str:
    base = (seed or "Customer Workspace").strip() or "Customer Workspace"
    name = base
    i = 2
    while db.query(Workspace).filter(Workspace.name == name).first():
        name = f"{base} {i}"
        i += 1
    return name


def _user_workspaces(db: Session, u: User) -> list[Workspace]:
    rows = (db.query(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .filter(WorkspaceMember.user_id == u.id,
                    WorkspaceMember.status == "active",
                    Workspace.status == "active")
            .order_by(Workspace.id).all())
    if not rows and u.default_workspace_id:
        has_memberships = (db.query(WorkspaceMember)
                           .filter(WorkspaceMember.user_id == u.id)
                           .first() is not None)
        ws = db.get(Workspace, u.default_workspace_id)
        if ws and (ws.status or "active") == "active" and not has_memberships:
            rows = [ws]
    return rows


def _user_has_workspace_access(db: Session, u: User, workspace_id: int) -> bool:
    if _is_super_admin(u):
        return True
    active_member = (db.query(WorkspaceMember)
                     .filter(WorkspaceMember.workspace_id == workspace_id,
                             WorkspaceMember.user_id == u.id,
                             WorkspaceMember.status == "active")
                     .first())
    if active_member:
        return True
    has_memberships = (db.query(WorkspaceMember)
                       .filter(WorkspaceMember.user_id == u.id)
                       .first() is not None)
    return (not has_memberships and
            (u.default_workspace_id is None or u.default_workspace_id == workspace_id))


def _default_workspace(db: Session) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.slug == "internal").first()
    if ws:
        return ws
    ws = Workspace(name="Internal Workspace", slug="internal",
                   type="internal", status="active")
    db.add(ws)
    db.flush()
    return ws


def _current_workspace(
    user: str,
    db: Session,
    x_workspace_id: str | None = None,
) -> Workspace:
    if x_workspace_id is not None and not isinstance(x_workspace_id, (str, int)):
        x_workspace_id = None
    key = _api_key_from_principal(user, db)
    if key:
        ws = db.get(Workspace, key.workspace_id) if key.workspace_id else None
        if ws and (ws.status or "active") == "active":
            return ws
        raise HTTPException(403, "API Key 未绑定可用 workspace")
    u = _require_dashboard_user(user, db)
    if x_workspace_id:
        try:
            requested_workspace_id = int(x_workspace_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "workspace_id 无效")
        ws = db.get(Workspace, requested_workspace_id)
        if not ws:
            raise HTTPException(404, "workspace 不存在")
        if (ws.status or "active") != "active":
            raise HTTPException(403, "workspace 已停用")
        if _is_super_admin(u):
            return ws
        member = (db.query(WorkspaceMember)
                  .filter(WorkspaceMember.workspace_id == requested_workspace_id,
                          WorkspaceMember.user_id == u.id,
                          WorkspaceMember.status == "active")
                  .first())
        if not member:
            raise HTTPException(403, "无权访问该 workspace")
        return ws
    if u.default_workspace_id:
        ws = db.get(Workspace, u.default_workspace_id)
        if (ws and (ws.status or "active") == "active" and
                _user_has_workspace_access(db, u, ws.id)):
            return ws
    rows = _user_workspaces(db, u)
    if rows:
        return rows[0]
    return _default_workspace(db)


def _workspace_role_for_user(db: Session, u: User | None,
                             workspace_id: int | None) -> str | None:
    if not u or not workspace_id:
        return None
    member = (db.query(WorkspaceMember)
              .filter(WorkspaceMember.workspace_id == workspace_id,
                      WorkspaceMember.user_id == u.id,
                      WorkspaceMember.status == "active")
              .first())
    return member.role if member else None


def _can_edit_workspace_report(db: Session, u: User | None,
                               workspace_id: int | None) -> bool:
    if _is_super_admin(u):
        return True
    if not u or (u.status or "active") != "active":
        return False
    if u.role in {"admin", "owner"}:
        return True
    return _workspace_role_for_user(db, u, workspace_id) in {
        "admin", "owner", "operator",
    }


def _require_report_editor(user: str, db: Session, ws: Workspace) -> User:
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能执行报表编辑/导出操作")
    u = _require_dashboard_user(user, db)
    if not _can_edit_workspace_report(db, u, ws.id):
        raise HTTPException(403, "需要报表编辑权限")
    return u


def _workspace_site_names(
    db: Session,
    workspace_id: int,
    include_hidden: bool = False,
) -> list[str]:
    q = db.query(WorkspaceSite).filter(
        WorkspaceSite.workspace_id == workspace_id,
        WorkspaceSite.enabled.is_(True),
    )
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))
    return [row.site for row in q.order_by(WorkspaceSite.sort_order,
                                           WorkspaceSite.id).all()]


def _workspace_site_targets(
    db: Session,
    workspace_id: int,
    include_hidden: bool = False,
) -> dict[str, int]:
    q = db.query(WorkspaceSite).filter(
        WorkspaceSite.workspace_id == workspace_id,
        WorkspaceSite.enabled.is_(True),
    )
    if not include_hidden:
        q = q.filter(WorkspaceSite.hidden.is_(False))
    return {
        row.site: int(row.target_sku_count or 0)
        for row in q.all()
        if row.target_sku_count
    }


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _require_site_in_workspace(site: str, allowed_sites: list[str]) -> None:
    if site not in set(allowed_sites):
        raise HTTPException(404, "当前 workspace 未启用该站点")


def _scoped_sites_from_params(site: str | None, sites: str | None,
                              allowed_sites: list[str]) -> list[str]:
    if sites:
        requested = [s.strip() for s in sites.replace(",", "|").split("|") if s.strip()]
    elif site:
        requested = [site]
    else:
        requested = list(allowed_sites)
    allowed = set(allowed_sites)
    return [s for s in requested if s in allowed]


def _user_from_token(db: Session, token: str) -> User:
    info = parse_token(token)
    if not info:
        raise HTTPException(401, "未登录或登录已过期")
    u = _find_user_by_identifier(db, info.username)
    if not u or (u.status or "active") != "active":
        raise HTTPException(401, "未登录或登录已过期")
    if info.session_id:
        session = (db.query(UserSession)
                   .filter(UserSession.session_hash == hash_secret(info.session_id),
                           UserSession.user_id == u.id)
                   .first())
        if (not session or session.revoked_at is not None or
                (session.expires_at and session.expires_at < datetime.utcnow())):
            raise HTTPException(401, "未登录或登录已过期")
    return u


def _issue_login_token(u: User, db: Session,
                       request: Request | None = None) -> dict:
    session_id = generate_session_id()
    session = UserSession(
        user_id=u.id,
        session_hash=hash_secret(session_id),
        expires_at=datetime.utcnow() + timedelta(seconds=TOKEN_TTL),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500] if request else None,
    )
    u.last_login = datetime.utcnow()
    u.last_login_ip = _client_ip(request)
    u.failed_login_count = 0
    u.locked_until = None
    db.add(session)
    db.commit()
    return {"token": make_token(u.username, session_id), **_public_user(u)}


def _enforce_production_secret() -> None:
    env = (os.environ.get("SC_ENV") or os.environ.get("APP_ENV") or
           os.environ.get("ENV") or "").lower()
    if env in {"prod", "production"} and not os.environ.get("SC_SECRET"):
        raise HTTPException(500, "生产环境必须设置 SC_SECRET")


def _login_with_identifier(payload: dict, db: Session,
                           request: Request | None = None) -> dict:
    _enforce_production_secret()
    identifier = ((payload or {}).get("identifier") or
                  (payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password", "")
    user = _find_user_by_identifier(db, identifier)
    now = datetime.utcnow()
    if user and user.locked_until and user.locked_until > now:
        raise HTTPException(429, "登录失败次数过多，请稍后重试")
    if not user or not verify_password(password, user.password_hash):
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= 5:
                user.locked_until = now + timedelta(minutes=10)
            db.commit()
        raise HTTPException(401, "账号或密码错误")
    if (user.status or "active") != "active":
        raise HTTPException(403, "账号已停用")
    return _issue_login_token(user, db, request)


def _validate_invite(db: Session, raw_code: str) -> InviteCode:
    code_hash = hash_secret((raw_code or "").strip())
    invite = db.query(InviteCode).filter(InviteCode.code_hash == code_hash).first()
    now = datetime.utcnow()
    if (not invite or not invite.active or
            (invite.expires_at and invite.expires_at < now) or
            (invite.used_count or 0) >= (invite.max_uses or 1)):
        raise HTTPException(400, "邀请码无效或已过期")
    return invite


def _generate_invite_code() -> str:
    return "sci_" + secrets.token_urlsafe(18)


@public_router.post("/login")
def login(payload: dict, request: Request, db: Session = Depends(get_db)):
    """账号登录 —— 返回 Token。"""
    return _login_with_identifier(payload, db, request)


@public_router.post("/auth/login")
def auth_login(payload: dict, request: Request, db: Session = Depends(get_db)):
    """邮箱或用户名登录。"""
    return _login_with_identifier(payload, db, request)


@public_router.post("/auth/register")
def auth_register(payload: dict, request: Request, db: Session = Depends(get_db)):
    """邀请码注册 —— 邀请码仅内部/admin 生成。"""
    _enforce_production_secret()
    payload = payload or {}
    try:
        username = validate_username(payload.get("username", ""))
        email = validate_email(payload.get("email", ""))
        password = payload.get("password", "")
        validate_password_strength(password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if password != payload.get("confirm_password", password):
        raise HTTPException(400, "两次输入的密码不一致")
    invite = _validate_invite(db, payload.get("invite_code", ""))
    exists = (db.query(User)
              .filter(or_(User.username == username, User.email == email))
              .first())
    if exists:
        raise HTTPException(409, "用户名或邮箱已存在")
    target_type = invite.target_type or "workspace"
    workspace_id = invite.workspace_id
    if target_type == "new_workspace":
        workspace_name = _unique_workspace_name(
            db, f"{(payload.get('display_name') or username).strip()} Workspace")
        workspace = Workspace(
            name=workspace_name,
            slug=_unique_workspace_slug(db, username),
            type="customer",
            status="active",
        )
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=invite.default_role or "user",
        default_workspace_id=workspace_id,
        status="active",
        display_name=(payload.get("display_name") or username).strip(),
        email_verified=False,
        password_changed_at=datetime.utcnow(),
    )
    invite.used_count = (invite.used_count or 0) + 1
    invite.last_used_at = datetime.utcnow()
    db.add(user)
    db.flush()
    if workspace_id:
        db.add(WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user.id,
            role="owner" if target_type == "new_workspace" else "member",
        ))
    return _issue_login_token(user, db, request)


@router.post("/auth/logout")
def auth_logout(authorization: str = Header(default=""),
                db: Session = Depends(get_db)):
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    info = parse_token(token)
    if info and info.session_id:
        session = (db.query(UserSession)
                   .filter(UserSession.session_hash == hash_secret(info.session_id))
                   .first())
        if session and not session.revoked_at:
            session.revoked_at = datetime.utcnow()
            db.commit()
    return {"status": "logged_out"}


@router.post("/auth/change-password")
def change_password(payload: dict, user: str = Depends(require_user),
                    authorization: str = Header(default=""),
                    db: Session = Depends(get_db)):
    u = _require_dashboard_user(user, db)
    old_password = (payload or {}).get("old_password", "")
    new_password = (payload or {}).get("new_password", "")
    if not verify_password(old_password, u.password_hash):
        raise HTTPException(400, "旧密码不正确")
    try:
        validate_password_strength(new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if new_password != (payload or {}).get("confirm_password", new_password):
        raise HTTPException(400, "两次输入的密码不一致")
    u.password_hash = hash_password(new_password)
    u.password_changed_at = datetime.utcnow()
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    info = parse_token(token)
    q = db.query(UserSession).filter(UserSession.user_id == u.id,
                                     UserSession.revoked_at.is_(None))
    for session in q.all():
        if not info or not info.session_id or session.session_hash != hash_secret(info.session_id):
            session.revoked_at = datetime.utcnow()
    db.commit()
    return {"status": "password_changed"}


@router.get("/me")
def me(user: str = Depends(require_user),
       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
       db: Session = Depends(get_db)):
    u = _current_user(user, db)
    if not u:
        return {"username": user, "role": "viewer"}
    workspaces = [_workspace_response(ws) for ws in _user_workspaces(db, u)]
    current_ws = _current_workspace(user, db, x_workspace_id)
    current_ws_id = current_ws.id if current_ws else (
        u.default_workspace_id or (workspaces[0]["id"] if workspaces else None)
    )
    # 当前工作区内的成员角色(owner/admin/member/viewer),供前端按租户角色门控
    workspace_role = None
    if current_ws_id:
        from .. import models as _m
        mem = (db.query(_m.WorkspaceMember)
               .filter(_m.WorkspaceMember.workspace_id == current_ws_id,
                       _m.WorkspaceMember.user_id == u.id,
                       _m.WorkspaceMember.status == "active").first())
        workspace_role = mem.role if mem else None
    return _public_user(u) | {
        "workspaces": workspaces,
        "current_workspace_id": current_ws_id,
        "workspace_role": workspace_role,
    }


@router.patch("/me")
def update_me(payload: dict, user: str = Depends(require_user),
              db: Session = Depends(get_db)):
    u = _require_dashboard_user(user, db)
    display_name = str((payload or {}).get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(400, "display_name 不能为空")
    u.display_name = display_name[:80]
    # 邮箱可选更新(唯一约束:不能与他人重复)
    if "email" in (payload or {}):
        email = str(payload.get("email") or "").strip().lower()
        if email and email != (u.email or ""):
            clash = (db.query(User)
                     .filter(User.email == email, User.id != u.id).first())
            if clash:
                raise HTTPException(400, "该邮箱已被占用")
            u.email = email
    db.commit()
    return _public_user(u)


@router.get("/workspaces")
def list_my_workspaces(user: str = Depends(require_user),
                       db: Session = Depends(get_db)):
    u = _require_dashboard_user(user, db)
    if _is_super_admin(u):
        rows = db.query(Workspace).order_by(Workspace.id).all()
    else:
        rows = _user_workspaces(db, u)
    return [_workspace_response(ws) for ws in rows]


@router.get("/workspaces/current")
def current_workspace(user: str = Depends(require_user),
                      x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                      db: Session = Depends(get_db)):
    return _workspace_response(_current_workspace(user, db, x_workspace_id))


# ---------- 序列化 ----------
def site_dict(s: Site) -> dict:
    return {"site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform, "proxy_tier": s.proxy_tier,
            "currency": _currency_for_site(s.site),
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None}


def product_dict(p: Product) -> dict:
    currency = _display_currency(p.currency, p.site)
    return {
        "id": p.id, "sku": p.sku, "spu": p.spu, "variant_id": p.variant_id,
        "title": p.title,
        "image": (p.image_urls or [None])[0], "image_urls": p.image_urls,
        "category_path": p.category_path, "sale_price": p.sale_price,
        "original_price": p.original_price, "currency": currency,
        "attributes": p.attributes, "ratings": p.ratings,
        "review_count": p.review_count, "thirty_day_sales": p.thirty_day_sales,
        "thirty_day_revenue": p.thirty_day_revenue, "status": p.status,
        "inventory": p.inventory, "has_video": p.has_video,
        "has_free_shipping": p.has_free_shipping, "label": p.label,
        "tags": p.tags, "product_url": p.product_url,
        "product_type": p.product_type, "is_new": p.is_new,
        "is_bestseller": p.is_bestseller,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "created_time": p.created_time.isoformat() if p.created_time else None,
        "updated_time": p.updated_time.isoformat() if p.updated_time else None,
        "site": p.site, "brand": p.brand,
    }


def _report_money(value, currency: str | None) -> str:
    if value is None:
        return ""
    amount = f"{value:g}" if isinstance(value, (int, float)) else str(value)
    return f"{currency} {amount}" if currency else amount


def _report_time(value: str | datetime | None) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return str(value)


def _report_promo_type(value: str | None) -> str:
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"coupon", "coupons"}:
        return "Coupons"
    if key in {"price", "price_promotion", "sale", "discount"}:
        return "Price Promotion"
    if key in {"bundle", "bundle_promotion"}:
        return "Bundle"
    return value or ""


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_iso_datetime_end(value: str | None) -> datetime | None:
    parsed = _parse_iso_datetime(value)
    if parsed and value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _filtered_products_query(
    db: Session,
    allowed_sites: list[str],
    *,
    site: str | None = None,
    tab: str = "all",
    search: str | None = None,
    status: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    category: str | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    min_sales: int | None = None,
    max_sales: int | None = None,
    min_revenue: float | None = None,
    max_revenue: float | None = None,
    min_variants: int | None = None,
    max_variants: int | None = None,
    has_video: bool | None = None,
    free_shipping: bool | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
):
    q = db.query(Product)
    if site:
        _require_site_in_workspace(site, allowed_sites)
        q = q.filter(Product.site == site)
    else:
        q = q.filter(Product.site.in_(allowed_sites))
    if tab == "bestseller":
        q = q.filter(Product.is_bestseller.is_(True))
    elif tab == "new":
        # 验收口径的“最新产品”应展示站点最近采集到的新品列表。
        # 有些历史站点只有 updated_time，不能因此把 tab 变成空表。
        q = q.filter(or_(Product.created_time.isnot(None),
                         Product.published_at.isnot(None),
                         Product.updated_time.isnot(None)))
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Product.title.ilike(like), Product.sku.ilike(like),
                         Product.spu.ilike(like),
                         Product.product_url.ilike(like),
                         cast(Product.attributes, String).ilike(like),
                         cast(Product.tags, String).ilike(like)))
    if status:
        status_values = _product_status_values(status)
        q = q.filter(func.lower(Product.status).in_([v.lower() for v in status_values]))
    effective_price = func.coalesce(Product.sale_price, Product.original_price)
    if min_price is not None:
        q = q.filter(effective_price >= min_price)
    if max_price is not None:
        q = q.filter(effective_price <= max_price)
    if category:
        q = q.filter(Product.category_path.ilike(f"%{category}%"))
    if min_rating is not None:
        q = q.filter(Product.ratings >= min_rating)
    if max_rating is not None:
        q = q.filter(Product.ratings <= max_rating)
    if min_reviews is not None:
        q = q.filter(Product.review_count >= min_reviews)
    if max_reviews is not None:
        q = q.filter(Product.review_count <= max_reviews)
    if min_sales is not None:
        q = q.filter(Product.thirty_day_sales >= min_sales)
    if max_sales is not None:
        q = q.filter(Product.thirty_day_sales <= max_sales)
    if min_revenue is not None:
        q = q.filter(Product.thirty_day_revenue >= min_revenue)
    if max_revenue is not None:
        q = q.filter(Product.thirty_day_revenue <= max_revenue)
    if min_variants is not None or max_variants is not None:
        variant_key = func.coalesce(Product.spu, Product.sku)
        variant_counts = (
            db.query(
                Product.site.label("variant_site"),
                variant_key.label("variant_key"),
                func.count(Product.id).label("variant_count"),
            )
            .group_by(Product.site, variant_key)
            .subquery()
        )
        q = q.join(
            variant_counts,
            and_(
                Product.site == variant_counts.c.variant_site,
                variant_key == variant_counts.c.variant_key,
            ),
        )
        if min_variants is not None:
            q = q.filter(variant_counts.c.variant_count >= min_variants)
        if max_variants is not None:
            q = q.filter(variant_counts.c.variant_count <= max_variants)
    if has_video is not None:
        q = q.filter(Product.has_video.is_(has_video))
    if free_shipping is not None:
        q = q.filter(Product.has_free_shipping.is_(free_shipping))
    created_at_expr = func.coalesce(Product.published_at, Product.created_time,
                                    Product.updated_time)
    parsed_from = _parse_iso_datetime(created_from)
    if parsed_from:
        q = q.filter(created_at_expr >= parsed_from)
    parsed_to = _parse_iso_datetime_end(created_to)
    if parsed_to:
        q = q.filter(created_at_expr <= parsed_to)
    return q


def _variant_counts_for_products(db: Session, products: list[Product]) -> dict[int, int]:
    keys = {(p.site, p.spu or p.sku) for p in products if p.site and (p.spu or p.sku)}
    if not keys:
        return {}
    sites = sorted({site for site, _key in keys})
    variant_key = func.coalesce(Product.spu, Product.sku)
    rows = (
        db.query(Product.site, variant_key.label("variant_key"),
                 func.count(Product.id))
        .filter(Product.site.in_(sites))
        .group_by(Product.site, variant_key)
        .all()
    )
    by_key = {(site, key): int(count or 0) for site, key, count in rows}
    return {p.id: by_key.get((p.site, p.spu or p.sku), 1) for p in products}


def _promotion_labels_for_products(db: Session,
                                   products: list[Product]) -> dict[int, list[str]]:
    keys = [(p.site, p.sku) for p in products if p.site and p.sku]
    if not keys:
        return {}
    sites = sorted({site for site, _sku in keys})
    skus = sorted({sku for _site, sku in keys})
    rows = (
        db.query(Promotion.site, Promotion.sku, Promotion.promotion_name,
                 Promotion.promotion_type)
        .filter(Promotion.site.in_(sites), Promotion.sku.in_(skus))
        .order_by(Promotion.detected_time.desc().nullslast(),
                  Promotion.id.desc())
        .all()
    )
    wanted = set(keys)
    by_key: dict[tuple[str, str], list[str]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for site, sku, name, promo_type in rows:
        key = (site, sku)
        if key not in wanted:
            continue
        label = str(name or promo_type or "").strip()
        if not label:
            continue
        bucket = by_key.setdefault(key, [])
        used = seen.setdefault(key, set())
        if label in used or len(bucket) >= 3:
            continue
        bucket.append(label[:80])
        used.add(label)
    return {p.id: by_key.get((p.site, p.sku), []) for p in products}


def _variant_rows_for_product(db: Session, p: Product) -> list[dict]:
    key = p.spu or p.sku
    if not p.site or not key:
        return []
    variant_key = func.coalesce(Product.spu, Product.sku)
    rows = (
        db.query(Product)
        .filter(Product.site == p.site, variant_key == key)
        .order_by(Product.id)
        .limit(200)
        .all()
    )
    return [{
        "id": row.id,
        "sku": row.sku,
        "spu": row.spu,
        "variant_id": row.variant_id,
        "title": row.title,
        "attributes": row.attributes,
        "sale_price": row.sale_price,
        "original_price": row.original_price,
        "currency": _display_currency(row.currency, row.site),
        "product_url": row.product_url,
        "status": row.status,
    } for row in rows]


def _variant_context_for_products(
    db: Session,
    products: list[Product],
) -> dict[int, dict]:
    keys = {(p.site, p.spu or p.sku) for p in products if p.site and (p.spu or p.sku)}
    if not keys:
        return {}
    sites = sorted({site for site, _key in keys})
    variant_keys = sorted({key for _site, key in keys})
    rows = (
        db.query(Product)
        .filter(Product.site.in_(sites))
        .filter(or_(Product.spu.in_(variant_keys), Product.sku.in_(variant_keys)))
        .order_by(Product.id)
        .all()
    )
    grouped: dict[tuple[str, str], list[Product]] = {}
    for row in rows:
        key = (row.site, row.spu or row.sku)
        if key in keys:
            grouped.setdefault(key, []).append(row)
    out: dict[int, dict] = {}
    for product in products:
        key = (product.site, product.spu or product.sku)
        variants = grouped.get(key) or [product]
        skus = [v.sku for v in variants if v.sku]
        out[product.id] = {
            "listing_sku": skus[0] if skus else product.sku,
            "variant_skus": skus,
            "variant_count": len(skus) or 1,
        }
    return out


def _product_status_values(status: str | None) -> list[str]:
    normalized = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "on_sale": ["on_sale", "on sale", "sale", "active", "available"],
        "out_of_stock": [
            "out_of_stock", "out of stock", "sold_out", "sold out",
            "unavailable", "oos",
        ],
        "discontinued": ["discontinued", "offline", "removed", "inactive"],
    }
    return aliases.get(normalized, [status.strip()] if status else [])


def _product_order_cols(tab: str):
    if tab == "new":
        return (
            func.coalesce(Product.published_at, Product.created_time,
                          Product.updated_time).desc().nullslast(),
            Product.id.desc(),
        )
    return (
        func.coalesce(Product.updated_time, Product.created_time,
                      Product.published_at).desc().nullslast(),
        Product.id.desc(),
    )


def _filtered_promotions_query(
    db: Session,
    allowed_sites: list[str],
    *,
    site: str | None = None,
    search: str | None = None,
    type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    q = db.query(Promotion)
    if site:
        _require_site_in_workspace(site, allowed_sites)
        q = q.filter(Promotion.site == site)
    else:
        q = q.filter(Promotion.site.in_(allowed_sites))
    if search:
        like = f"%{search}%"
        product_match = exists().where(
            Product.site == Promotion.site,
            Product.sku == Promotion.sku,
            or_(Product.product_url.ilike(like),
                Product.title.ilike(like),
                cast(Product.attributes, String).ilike(like)),
        )
        q = q.filter(or_(Promotion.sku.ilike(like),
                         Promotion.promotion_name.ilike(like),
                         Promotion.product_title.ilike(like),
                         product_match))
    if type:
        values = _promotion_type_values(type)
        q = q.filter(or_(*[
            Promotion.promotion_type.ilike(f"%{value}%")
            for value in values
        ]))
    parsed_from = _parse_iso_datetime(date_from)
    if parsed_from:
        q = q.filter(Promotion.detected_time >= parsed_from)
    parsed_to = _parse_iso_datetime_end(date_to)
    if parsed_to:
        q = q.filter(Promotion.detected_time <= parsed_to)
    return q


def _promotion_type_values(value: str | None) -> list[str]:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "price": ["price", "price_promotion"],
        "price_promotion": ["price", "price_promotion"],
        "价格促销": ["price", "price_promotion"],
        "coupon": ["coupon"],
        "coupons": ["coupon"],
        "优惠券": ["coupon"],
        "bundle": ["bundle"],
        "组合": ["bundle"],
        "clearance": ["clearance"],
        "site_promotion": ["site_promotion"],
    }
    return aliases.get(normalized, [value.strip()] if value else [])


# ---------- 站点概览（R-001 / R-002 / §14.2）----------
@router.get("/sites")
def list_sites(
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
    include_hidden: bool = Query(default=False, description="是否包含 hidden_sites（默认排除）"),
):
    # N+1 修复 + 60s 缓存 · 之前 11s · spu distinct 在 3M+ rows 表上是元凶
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id, include_hidden=include_hidden)
    cache_key = f"sites:{ws.id}:{include_hidden}"
    cached = _coverage_cache_get(cache_key)
    if cached is not None:
        return cached
    from sqlalchemy import func
    hidden = _load_hidden_sites() if not include_hidden else set()
    sku_counts = dict(db.query(Product.site, func.count(Product.id))
                        .group_by(Product.site).all())
    # spu_count: distinct(coalesce(spu, sku)) · 变体合并、无 spu 行按 sku 各算一款
    # 大表 cache-miss 时约 7-8s · 由 30s _COVERAGE_CACHE 兜底
    spu_counts = dict(
        db.query(Product.site,
                 func.count(func.distinct(func.coalesce(Product.spu, Product.sku))))
          .group_by(Product.site).all())
    cat_counts = dict(db.query(Category.site, func.count(Category.id))
                        .group_by(Category.site).all())
    promo_counts = dict(db.query(Promotion.site, func.count(Promotion.id))
                          .group_by(Promotion.site).all())
    out = []
    for s in db.query(Site).filter(Site.site.in_(allowed_sites)).all():
        if s.site in hidden or s.site not in allowed_sites:
            continue
        d = site_dict(s)
        d["sku_count"] = sku_counts.get(s.site, 0)
        d["spu_count"] = spu_counts.get(s.site, 0)
        d["category_count"] = cat_counts.get(s.site, 0)
        d["promotion_count"] = promo_counts.get(s.site, 0)
        out.append(d)
    _coverage_cache_set(cache_key, out)
    return out


@router.get("/sites/{site}/overview")
def site_overview(site: str, user: str = Depends(require_user),
                  x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                  granularity: str = Query("month", pattern="^(day|week|month)$"),
                  date_from: str | None = None,
                  date_to: str | None = None,
                  db: Session = Depends(get_db)):
    """6 个指标卡 + 趋势序列。"""
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(site, _workspace_site_names(db, ws.id))
    site_row = db.query(Site).filter(Site.site == site).first()
    if not site_row:
        raise HTTPException(404, "站点不存在")
    currency = _currency_for_site(site)
    sku_count = db.query(Product).filter(Product.site == site).count()
    product_count = (
        db.query(func.count(func.distinct(func.coalesce(Product.spu, Product.sku))))
        .filter(Product.site == site)
        .scalar() or 0
    )
    _new_cutoff = datetime.utcnow() - timedelta(days=NEW_PRODUCT_DAYS)
    new_count = db.query(Product).filter(
        Product.site == site,
        or_(Product.created_time >= _new_cutoff,
            Product.published_at >= _new_cutoff),
    ).count()
    latest_product_count = db.query(Product).filter(
        Product.site == site,
        or_(Product.created_time.isnot(None),
            Product.published_at.isnot(None),
            Product.updated_time.isnot(None)),
    ).count()
    bestseller_count = db.query(Product).filter(
        Product.site == site, Product.is_bestseller.is_(True)).count()
    category_count = (db.query(func.count(func.distinct(Product.category_path)))
                      .filter(Product.site == site,
                              Product.category_path.isnot(None)).scalar() or 0)
    sales, revenue = db.query(
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
    ).filter(Product.site == site).first()
    last_product_updated = (db.query(func.max(Product.updated_time))
                            .filter(Product.site == site).scalar())
    updated_candidates = [
        site_row.last_crawled,
        last_product_updated,
        site_row.updated_at,
        site_row.created_at,
    ]
    updated_at = next((value for value in updated_candidates if value), None)
    trend_q = db.query(Trend).filter(Trend.site == site)
    parsed_from = _parse_iso_datetime(date_from)
    parsed_to = _parse_iso_datetime_end(date_to)
    if parsed_from:
        trend_q = trend_q.filter(Trend.date >= parsed_from.date())
    if parsed_to:
        trend_q = trend_q.filter(Trend.date <= parsed_to.date())
    raw_trends = trend_q.order_by(Trend.date).all()
    trend_buckets: dict[str, dict] = {}
    for t in raw_trends:
        key = _trend_bucket_key(t.date, granularity)
        # 站点趋势是日快照/30天滚动指标，聚合到周/月时用桶内最后一天代表。
        trend_buckets[key] = {
            "date": key, "source_date": t.date.isoformat(),
            "sku_count": t.sku_count,
            "new_product_count": t.new_product_count,
            "estimated_sales": t.estimated_sales,
            "estimated_revenue": t.estimated_revenue,
            "traffic": t.traffic,
            "conversion_rate": t.conversion_rate,
            "avg_rating": t.avg_rating,
            "review_total": t.review_total,
        }
    trends = list(trend_buckets.values())
    used_snapshot_fallback = False
    if not trends:
        snapshot_dt = updated_at or datetime.utcnow()
        snapshot_date = snapshot_dt.date()
        if (not parsed_from or snapshot_date >= parsed_from.date()) and (
            not parsed_to or snapshot_date <= parsed_to.date()
        ):
            avg_rating, review_total = db.query(
                func.avg(Product.ratings),
                func.coalesce(func.sum(Product.review_count), 0),
            ).filter(Product.site == site).first()
            trends = [{
                "date": _trend_bucket_key(snapshot_date, granularity),
                "source_date": snapshot_date.isoformat(),
                "sku_count": sku_count,
                "product_count": int(product_count),
                "spu_count": int(product_count),
                "new_product_count": new_count,
                "estimated_sales": int(sales or 0),
                "estimated_revenue": round(revenue or 0, 2),
                "traffic": None,
                "conversion_rate": None,
                "avg_rating": round(float(avg_rating), 2) if avg_rating is not None else None,
                "review_total": int(review_total or 0),
                "snapshot": True,
            }]
            used_snapshot_fallback = True
    current_period = trends[-1] if trends else {
        "date": None,
        "sku_count": sku_count,
        "product_count": int(product_count),
        "spu_count": int(product_count),
        "new_product_count": new_count,
        "estimated_sales": int(sales or 0),
        "estimated_revenue": round(revenue or 0, 2),
        "traffic": None,
        "conversion_rate": None,
        "avg_rating": None,
        "review_total": None,
    }
    previous_period = trends[-2] if len(trends) > 1 else None
    return {
        "cards": {
            "sku_count": sku_count, "product_count": int(product_count),
            "spu_count": int(product_count),
            "new_product_count": new_count,
            "latest_product_count": latest_product_count,
            "bestseller_count": bestseller_count,
            "category_count": int(category_count),
            "thirty_day_sales": int(sales or 0),
            "thirty_day_revenue": round(revenue or 0, 2),
            "currency": currency,
            "traffic": current_period.get("traffic"),
            "conversion_rate": current_period.get("conversion_rate"),
        },
        "trends": trends,
        "currency": currency,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "trend_summary": {
            "granularity": granularity,
            "visible_points": len(trends),
            "current_period": current_period,
            "previous_period": previous_period,
            "snapshot_fallback": used_snapshot_fallback,
        },
    }


# ---------- 商品分析（R-010 / §14.3 / API-002）----------
@router.get("/products")
def list_products(
    site: str | None = None,
    tab: str = Query("all", pattern="^(all|bestseller|new)$"),
    search: str | None = None,
    status: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    category: str | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_reviews: int | None = None,
    max_reviews: int | None = None,
    min_sales: int | None = None,
    max_sales: int | None = None,
    min_revenue: float | None = None,
    max_revenue: float | None = None,
    min_variants: int | None = None,
    max_variants: int | None = None,
    has_video: bool | None = None,
    free_shipping: bool | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
):
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    q = _filtered_products_query(
        db, allowed_sites, site=site, tab=tab, search=search, status=status,
        min_price=min_price, max_price=max_price, category=category,
        min_rating=min_rating, max_rating=max_rating,
        min_reviews=min_reviews, max_reviews=max_reviews,
        min_sales=min_sales, max_sales=max_sales,
        min_revenue=min_revenue, max_revenue=max_revenue,
        min_variants=min_variants, max_variants=max_variants,
        has_video=has_video, free_shipping=free_shipping,
        created_from=created_from, created_to=created_to,
    )
    total = q.count()
    rows = (q.order_by(*_product_order_cols(tab))
            .offset((page - 1) * page_size).limit(page_size).all())
    variant_counts = _variant_counts_for_products(db, rows)
    promotion_labels = _promotion_labels_for_products(db, rows)
    items = []
    for row in rows:
        item = product_dict(row)
        item["variant_count"] = variant_counts.get(row.id, 1)
        item["promotion_labels"] = promotion_labels.get(row.id, [])
        items.append(item)
    return {"total": total, "page": page, "page_size": page_size,
            "items": items}


@router.post("/ondemand/fetch")
def ondemand_fetch(payload: dict, user: str = Depends(require_user),
                   x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                   db: Session = Depends(get_db)):
    """按需抓取(单条):建 queued job + 入队,立即返回(异步串行抓取)。

    与 /ondemand/batch 同一套队列,仅 urls 为单元素。
    payload: {"url": "...", "max_items"?: int, "review_limit"?: int}
    """
    from .ondemand_jobs import flush_enqueue, submit_batch

    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 必填")
    max_items = int(payload.get("max_items", 100))
    review_limit = int(payload.get("review_limit", 100))
    ws = _current_workspace(user, db, x_workspace_id)
    u = _current_user(user, db)
    try:
        out = submit_batch(db, ws_id=ws.id,
                           username=(u.username if u else user), urls=[url],
                           max_items=max_items, review_limit=review_limit)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return flush_enqueue(out)   # commit 之后才入队,避免竞态


@router.post("/ondemand/batch")
def ondemand_batch(payload: dict, user: str = Depends(require_user),
                   x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                   db: Session = Depends(get_db)):
    """批量提交 URL → 建 queued job + 入队,立即返回(异步串行抓取)。

    payload: {"urls": [...], "max_items"?: int, "review_limit"?: int}
    """
    from .ondemand_jobs import flush_enqueue, submit_batch

    urls = (payload or {}).get("urls") or []
    if not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="urls 必须是数组")
    max_items = int(payload.get("max_items", 20))
    review_limit = int(payload.get("review_limit", 100))
    ws = _current_workspace(user, db, x_workspace_id)
    u = _current_user(user, db)
    try:
        out = submit_batch(db, ws_id=ws.id,
                           username=(u.username if u else user), urls=urls,
                           max_items=max_items, review_limit=review_limit)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return flush_enqueue(out)   # commit 之后才入队,避免竞态


@router.post("/ondemand/jobs/{job_id}/retry")
def ondemand_job_retry(job_id: int, user: str = Depends(require_user),
                       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                       db: Session = Depends(get_db)):
    from .ondemand_jobs import NotRetryableError, flush_enqueue, retry_job
    from ..models import OnDemandJob
    ws = _current_workspace(user, db, x_workspace_id)
    try:
        out = retry_job(db, ws_id=ws.id, job_id=job_id)
    except NotRetryableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if out is None:
        exists = db.get(OnDemandJob, job_id)
        raise HTTPException(status_code=403 if exists else 404,
                            detail="无权操作" if exists else "记录不存在")
    db.commit()
    return flush_enqueue(out)


@router.post("/ondemand/batch/{batch_id}/retry-failed")
def ondemand_batch_retry_failed(batch_id: str, user: str = Depends(require_user),
                                x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                                db: Session = Depends(get_db)):
    from .ondemand_jobs import flush_enqueue, retry_failed_batch
    ws = _current_workspace(user, db, x_workspace_id)
    out = retry_failed_batch(db, ws_id=ws.id, batch_id=batch_id)
    db.commit()
    return flush_enqueue(out)


@router.get("/ondemand/jobs")
def ondemand_jobs_list(platform: str | None = None, page: int = 1,
                       page_size: int = 20, batch_id: str | None = None,
                       status: str | None = None,
                       user: str = Depends(require_user),
                       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                       db: Session = Depends(get_db)):
    from .ondemand_jobs import list_jobs_logic
    ws = _current_workspace(user, db, x_workspace_id)
    return list_jobs_logic(db, ws_id=ws.id, platform=platform,
                           page=page, page_size=page_size,
                           batch_id=batch_id, status=status)


@router.get("/ondemand/jobs/{job_id}")
def ondemand_job_detail(job_id: int, user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import job_detail_logic
    ws = _current_workspace(user, db, x_workspace_id)
    detail = job_detail_logic(db, ws_id=ws.id, job_id=job_id)
    if detail is None:
        # 区分 404(不存在)与 403(越权):存在但不属于本 ws → 403
        from ..models import OnDemandJob
        exists = db.get(OnDemandJob, job_id)
        raise HTTPException(status_code=403 if exists else 404,
                            detail="无权访问" if exists else "记录不存在")
    return detail


@router.delete("/ondemand/jobs/{job_id}")
def ondemand_job_delete(job_id: int, user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import delete_job_logic
    from ..models import OnDemandJob
    ws = _current_workspace(user, db, x_workspace_id)
    ok = delete_job_logic(db, ws_id=ws.id, job_id=job_id)
    if not ok:
        exists = db.get(OnDemandJob, job_id)
        raise HTTPException(status_code=403 if exists else 404,
                            detail="无权删除" if exists else "记录不存在")
    db.commit()
    return {"deleted": True, "id": job_id}


@router.delete("/ondemand/jobs")
def ondemand_jobs_clear(user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import clear_jobs_logic
    ws = _current_workspace(user, db, x_workspace_id)
    n = clear_jobs_logic(db, ws_id=ws.id)
    db.commit()
    return {"deleted": n}


@router.get("/products/{pid}")
def get_product(pid: int, user: str = Depends(require_user),
                x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(p.site, _workspace_site_names(db, ws.id))
    return product_dict(p)


@router.get("/products/{pid}/price-history")
def price_history(pid: int, user: str = Depends(require_user),
                  x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                  db: Session = Depends(get_db)):
    """单 SKU 价格曲线 —— R-012。"""
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(p.site, _workspace_site_names(db, ws.id))
    rows = (db.query(PriceHistory)
            .filter(PriceHistory.site == p.site, PriceHistory.sku == p.sku)
            .order_by(PriceHistory.date).all())
    return [{"date": r.date.isoformat(), "sale_price": r.sale_price,
             "original_price": r.original_price,
             "review_count": r.review_count} for r in rows]


def _trend_bucket_key(value, granularity: str) -> str:
    if not value:
        return ""
    if granularity == "month":
        return value.isoformat()[:7]
    if granularity == "week":
        year, week, _weekday = value.isocalendar()
        return f"{year}-W{week:02d}"
    return value.isoformat()[:10]


def _build_product_trend_payload(
    db: Session,
    p: Product,
    *,
    granularity: str = "day",
    date_from: str | None = None,
    date_to: str | None = None,
    promo_search: str | None = None,
    promo_type: str | None = None,
    promo_sku: str | None = None,
    promo_page: int | None = 1,
    promo_page_size: int | None = 20,
) -> dict:
    granularity = granularity if granularity in {"day", "week", "month"} else "day"
    parsed_from = _parse_iso_datetime(date_from)
    parsed_to = _parse_iso_datetime_end(date_to)
    from_date = parsed_from.date() if parsed_from else None
    to_date = parsed_to.date() if parsed_to else None
    rows = (db.query(PriceHistory)
            .filter(PriceHistory.site == p.site, PriceHistory.sku == p.sku)
            .order_by(PriceHistory.date).all())
    site_row = db.query(Site).filter(Site.site == p.site).first()
    rate = float(site_row.review_rate or 0.025) if site_row else 0.025
    rate = rate if rate > 0 else 0.025

    raw_rows = []
    prev_reviews: int | None = None
    for h in rows:
        sales = 0
        if h.review_count is not None and prev_reviews is not None:
            delta = int(h.review_count or 0) - int(prev_reviews or 0)
            sales = max(0, round(delta / rate)) if delta > 0 else 0
        if h.review_count is not None:
            prev_reviews = int(h.review_count or 0)
        price = h.sale_price if h.sale_price is not None else h.original_price
        if from_date and h.date and h.date < from_date:
            continue
        if to_date and h.date and h.date > to_date:
            continue
        raw_rows.append({
            "date": h.date.isoformat(),
            "sale_price": h.sale_price,
            "original_price": h.original_price,
            "review_total": h.review_count,
            "estimated_sales": sales,
            "estimated_revenue": round(sales * (price or 0), 2),
            "avg_rating": p.ratings,
        })
    used_snapshot_fallback = False
    if not raw_rows:
        snapshot_dt = p.updated_time or p.created_time or datetime.utcnow()
        snapshot_date = snapshot_dt.date()
        if (not from_date or snapshot_date >= from_date) and (
            not to_date or snapshot_date <= to_date
        ):
            price = p.sale_price if p.sale_price is not None else p.original_price
            revenue = p.thirty_day_revenue
            if revenue is None and price is not None and p.thirty_day_sales is not None:
                revenue = round((p.thirty_day_sales or 0) * price, 2)
            raw_rows.append({
                "date": snapshot_date.isoformat(),
                "sale_price": p.sale_price,
                "original_price": p.original_price,
                "review_total": p.review_count,
                "estimated_sales": p.thirty_day_sales or 0,
                "estimated_revenue": revenue or 0,
                "avg_rating": p.ratings,
                "snapshot": True,
            })
            used_snapshot_fallback = True

    if granularity == "day":
        trend_rows = raw_rows
    else:
        buckets: dict[str, dict] = {}
        for row in raw_rows:
            key = _trend_bucket_key(datetime.fromisoformat(row["date"]).date(),
                                    granularity)
            current = buckets.setdefault(key, {
                "date": key,
                "sale_price": row["sale_price"],
                "original_price": row["original_price"],
                "review_total": row["review_total"],
                "estimated_sales": 0,
                "estimated_revenue": 0,
                "avg_rating": row["avg_rating"],
                "points": 0,
                "snapshot": bool(row.get("snapshot")),
            })
            current["estimated_sales"] += row["estimated_sales"] or 0
            current["estimated_revenue"] = round(
                (current["estimated_revenue"] or 0) + (row["estimated_revenue"] or 0), 2)
            current["sale_price"] = row["sale_price"]
            current["original_price"] = row["original_price"]
            current["review_total"] = row["review_total"]
            current["avg_rating"] = row["avg_rating"]
            current["snapshot"] = bool(current.get("snapshot") or row.get("snapshot"))
            current["points"] += 1
        trend_rows = [buckets[k] for k in sorted(buckets.keys())]

    variant_rows = _variant_rows_for_product(db, p)
    variant_skus = [row["sku"] for row in variant_rows if row.get("sku")]
    if not variant_skus and p.sku:
        variant_skus = [p.sku]
    promo_sku = (promo_sku or "").strip()
    promo_q = db.query(Promotion).filter(Promotion.site == p.site)
    if variant_skus:
        promo_q = promo_q.filter(Promotion.sku.in_(variant_skus))
    else:
        promo_q = promo_q.filter(Promotion.sku == p.sku)
    if promo_sku:
        promo_q = promo_q.filter(Promotion.sku == promo_sku)
    if promo_search:
        like = f"%{promo_search}%"
        product_match = exists().where(
            Product.site == Promotion.site,
            Product.sku == Promotion.sku,
            or_(Product.product_url.ilike(like),
                Product.title.ilike(like),
                cast(Product.attributes, String).ilike(like)),
        )
        promo_q = promo_q.filter(or_(Promotion.sku.ilike(like),
                                     Promotion.promotion_name.ilike(like),
                                     Promotion.product_title.ilike(like),
                                     product_match))
    if promo_type:
        promo_q = promo_q.filter(or_(*[
            Promotion.promotion_type.ilike(f"%{value}%")
            for value in _promotion_type_values(promo_type)
        ]))
    if parsed_from:
        promo_q = promo_q.filter(Promotion.detected_time >= parsed_from)
    if parsed_to:
        promo_q = promo_q.filter(Promotion.detected_time <= parsed_to)
    promo_total = promo_q.count()
    promo_ordered = promo_q.order_by(Promotion.detected_time.desc().nullslast(),
                                     Promotion.id.desc())
    if promo_page_size is None:
        promos = promo_ordered.limit(5000).all()
        promo_page_value = 1
        promo_page_size_value = promo_total
    else:
        try:
            promo_page_value = max(1, int(promo_page or 1))
        except (TypeError, ValueError):
            promo_page_value = 1
        try:
            promo_page_size_value = max(1, min(500, int(promo_page_size or 20)))
        except (TypeError, ValueError):
            promo_page_size_value = 20
        promos = (promo_ordered
                  .offset((promo_page_value - 1) * promo_page_size_value)
                  .limit(promo_page_size_value)
                  .all())
    promo_products: dict[tuple[str | None, str | None], Product] = {}
    if promos:
        promo_skus = sorted({r.sku for r in promos if r.sku})
        if promo_skus:
            promo_products = {
                (row.site, row.sku): row for row in (
                    db.query(Product)
                    .filter(Product.site == p.site, Product.sku.in_(promo_skus))
                    .all()
                )
            }
    promo_rows = []
    listing_sku = variant_skus[0] if variant_skus else p.sku
    listing_variant_count = len(variant_skus) or (1 if p.sku else 0)
    for r in promos:
        promo_product = promo_products.get((r.site, r.sku))
        promo_rows.append({
            "id": r.id,
            "sku": r.sku,
            "listing_sku": listing_sku,
            "variant_skus": variant_skus,
            "variant_count": listing_variant_count,
            "site": r.site,
            "promotion_type": r.promotion_type,
            "promotion_name": r.promotion_name,
            "original_price": r.original_price,
            "promotion_price": r.promotion_price,
            "currency": _display_currency(
                promo_product.currency if promo_product else p.currency, p.site),
            "discount_percent": r.discount_percent,
            "threshold": r.threshold,
            "product_title": r.product_title or (
                promo_product.title if promo_product else p.title),
            "product_image": r.product_image or (
                (promo_product.image_urls or [None])[0]
                if promo_product else (p.image_urls or [None])[0]
            ),
            "product_label": promo_product.label if promo_product else p.label,
            "product_tags": promo_product.tags if promo_product else p.tags,
            "is_new": bool(promo_product.is_new) if promo_product else bool(p.is_new),
            "is_bestseller": (
                bool(promo_product.is_bestseller)
                if promo_product else bool(p.is_bestseller)
            ),
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "detected_time": r.detected_time.isoformat() if r.detected_time else None,
        })

    has_review_signal = sum(1 for h in rows if h.review_count is not None) >= 2
    current_period = trend_rows[-1] if trend_rows else None
    previous_period = trend_rows[-2] if len(trend_rows) >= 2 else None
    data_notes = []
    if used_snapshot_fallback:
        data_notes.append("暂无价格历史，已展示当前商品快照")
    data_notes.append("销量/收入由评论增量按留评率估算" if has_review_signal else "评论历史不足，销量/收入优先展示当前商品字段")
    data_notes.append("流量/转化率需要接入第三方数据源")
    return {
        "product": product_dict(p),
        "variants": variant_rows,
        "summary": {
            "thirty_day_sales": p.thirty_day_sales or 0,
            "thirty_day_revenue": p.thirty_day_revenue or 0,
            "price": p.sale_price if p.sale_price is not None else p.original_price,
            "original_price": p.original_price,
            "currency": _display_currency(p.currency, p.site),
            "ratings": p.ratings,
            "review_count": p.review_count or 0,
            "review_rate": rate,
            "history_points": len(rows),
            "visible_points": len(trend_rows),
            "promotion_count": int(promo_total or 0),
            "promotion_page": promo_page_value,
            "promotion_page_size": promo_page_size_value,
            "promotion_total": int(promo_total or 0),
            "granularity": granularity,
            "date_from": date_from,
            "date_to": date_to,
            "promo_sku": promo_sku,
            "current_period": current_period,
            "previous_period": previous_period,
            "snapshot_fallback": used_snapshot_fallback,
            "has_review_signal": has_review_signal,
            "traffic": None,
            "conversion_rate": None,
            "data_notes": data_notes,
        },
        "trend": trend_rows,
        "promotions": promo_rows,
    }


@router.get("/products/{pid}/trend")
def product_trend(pid: int, user: str = Depends(require_user),
                  granularity: str = Query("day", pattern="^(day|week|month)$"),
                  date_from: str | None = None,
                  date_to: str | None = None,
                  promo_search: str | None = None,
                  promo_type: str | None = None,
                  promo_sku: str | None = None,
                  promo_page: int = Query(1, ge=1),
                  promo_page_size: int = Query(20, ge=1, le=500),
                  x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                  db: Session = Depends(get_db)):
    """单品趋势分析 —— 销售/收入/价格/评分/评论 + SKU 促销明细。"""
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(p.site, _workspace_site_names(db, ws.id))
    return _build_product_trend_payload(
        db, p, granularity=granularity, date_from=date_from, date_to=date_to,
        promo_search=promo_search, promo_type=promo_type, promo_sku=promo_sku,
        promo_page=promo_page, promo_page_size=promo_page_size,
    )


# ---------- 促销分析（§14.4 / API-005）----------
@router.get("/promotions")
def list_promotions(site: str | None = None, page: int = 1,
                    page_size: int = 50,
                    search: str | None = None,
                    type: str | None = None,
                    date_from: str | None = None,
                    date_to: str | None = None,
                    user: str = Depends(require_user),
                    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                    db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    q = _filtered_promotions_query(
        db, allowed_sites, site=site, search=search, type=type,
        date_from=date_from, date_to=date_to,
    )
    total = q.count()
    rows = (q.order_by(Promotion.detected_time.desc().nullslast(), Promotion.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    product_lookup: dict[tuple[str | None, str | None], Product] = {}
    variant_context: dict[int, dict] = {}
    if rows:
        row_sites = sorted({r.site for r in rows if r.site})
        row_skus = sorted({r.sku for r in rows if r.sku})
        if row_sites and row_skus:
            product_lookup = {
                (p.site, p.sku): p for p in (
                    db.query(Product)
                    .filter(Product.site.in_(row_sites), Product.sku.in_(row_skus))
                    .all()
                )
            }
            variant_context = _variant_context_for_products(
                db, list(product_lookup.values())
            )
    items = []
    for r in rows:
        product = product_lookup.get((r.site, r.sku))
        variants = variant_context.get(product.id, {}) if product else {}
        items.append({
            "id": r.id, "sku": r.sku, "site": r.site,
            "listing_sku": variants.get("listing_sku") or r.sku,
            "variant_skus": variants.get("variant_skus") or ([r.sku] if r.sku else []),
            "variant_count": variants.get("variant_count") or (1 if r.sku else 0),
            "promotion_type": r.promotion_type, "promotion_name": r.promotion_name,
            "original_price": r.original_price, "promotion_price": r.promotion_price,
            "currency": _currency_for_site(r.site),
            "discount_percent": r.discount_percent, "threshold": r.threshold,
            "product_title": r.product_title or (product.title if product else None),
            "product_image": r.product_image or (
                (product.image_urls or [None])[0] if product else None
            ),
            "product_url": product.product_url if product else None,
            "product_label": product.label if product else None,
            "product_tags": product.tags if product else None,
            "is_new": bool(product.is_new) if product else False,
            "is_bestseller": bool(product.is_bestseller) if product else False,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "detected_time": r.detected_time.isoformat() if r.detected_time else None,
        })
    return {"total": total, "page": page, "page_size": page_size,
            "items": items}


# ---------- 趋势 / 分类（API-004）----------
@router.get("/trends")
def list_trends(site: str, user: str = Depends(require_user),
                x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(site, _workspace_site_names(db, ws.id))
    return [{"date": t.date.isoformat(), "sku_count": t.sku_count,
             "new_product_count": t.new_product_count,
             "estimated_sales": t.estimated_sales,
             "estimated_revenue": t.estimated_revenue,
             # Daily delta 字段（2026-05-24）
             "price_change_count": getattr(t, "price_change_count", 0),
             "stock_change_count": getattr(t, "stock_change_count", 0),
             "new_promo_count": getattr(t, "new_promo_count", 0),
             "new_review_count": getattr(t, "new_review_count", 0),
             "avg_sentiment": getattr(t, "avg_sentiment", None),
             "delta_summary": getattr(t, "delta_summary", None)}
            for t in db.query(Trend).filter(Trend.site == site)
            .order_by(Trend.date).all()]


# ---------- Daily Delta（2026-05-24 加 · 遨森每日增量需求）----------
@router.post("/daily-delta/run")
def trigger_daily_delta(user: str = Depends(require_user),
                        db: Session = Depends(get_db)):
    """手动触发 daily delta 5 个 job。生产环境每天凌晨 2:00 自动跑。"""
    _require_admin(user, db)
    from ..daily_delta import run_all_daily_delta
    return run_all_daily_delta()


@router.get("/daily-delta/latest")
def latest_daily_delta(user: str = Depends(require_user),
                       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                       db: Session = Depends(get_db)):
    """看最近 1 天所有 site 的 delta 总结。"""
    from datetime import date
    today = date.today()
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    rows = (db.query(Trend)
            .filter(Trend.date == today)
            .filter(Trend.site.in_(allowed_sites))
            .order_by(Trend.site).all())
    return {
        "date": today.isoformat(),
        "site_count": len(rows),
        "sites": [{
            "site": t.site,
            "sku_count": t.sku_count,
            "new_skus": t.new_product_count,
            "price_changes": getattr(t, "price_change_count", 0),
            "new_promos": getattr(t, "new_promo_count", 0),
            "new_reviews": getattr(t, "new_review_count", 0),
            "avg_sentiment": getattr(t, "avg_sentiment", None),
            "summary": getattr(t, "delta_summary", None),
        } for t in rows]
    }


@router.get("/categories")
def list_categories(site: str, user: str = Depends(require_user),
                    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                    db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_site_in_workspace(site, _workspace_site_names(db, ws.id))
    rows = db.query(Category).filter(Category.site == site).all()
    return [{"category_id": c.category_id, "name": c.category_name,
             "url": c.category_url, "level": c.level,
             "product_count": c.product_count} for c in rows]


@router.get("/reports/configs")
def list_report_configs(user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    rows = (db.query(ReportConfig)
            .filter(ReportConfig.workspace_id == ws.id)
            .order_by(ReportConfig.id.desc()).all())
    return [{
        "id": r.id,
        "workspace_id": r.workspace_id,
        "name": r.name,
        "sites": r.sites or [],
        "categories": r.categories or [],
        "settings": r.settings or {},
        "active": r.active,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]


@router.post("/reports/configs")
def create_report_config(payload: dict, user: str = Depends(require_user),
                         x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                         db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_report_editor(user, db, ws)
    allowed = set(_workspace_site_names(db, ws.id, include_hidden=True))
    sites = [s for s in ((payload or {}).get("sites") or []) if s in allowed]
    if not sites:
        sites = list(allowed)
    row = ReportConfig(
        workspace_id=ws.id,
        name=(payload or {}).get("name") or "Default Report",
        sites=sites,
        categories=(payload or {}).get("categories") or [],
        settings=(payload or {}).get("settings") or {},
        active=bool((payload or {}).get("active", True)),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id, "workspace_id": row.workspace_id,
        "name": row.name, "sites": row.sites or [],
        "categories": row.categories or [], "settings": row.settings or {},
        "active": row.active,
    }


@router.patch("/reports/configs/{config_id}")
def update_report_config(config_id: int, payload: dict,
                         user: str = Depends(require_user),
                         x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                         db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_report_editor(user, db, ws)
    row = db.get(ReportConfig, config_id)
    if not row or row.workspace_id != ws.id:
        raise HTTPException(404, "报告配置不存在")
    payload = payload or {}
    if "name" in payload:
        row.name = str(payload.get("name") or "").strip() or row.name
    if "sites" in payload:
        allowed = set(_workspace_site_names(db, ws.id, include_hidden=True))
        row.sites = [s for s in (payload.get("sites") or []) if s in allowed]
    if "categories" in payload:
        row.categories = payload.get("categories") or []
    if "settings" in payload:
        row.settings = payload.get("settings") or {}
    if "active" in payload:
        row.active = bool(payload["active"])
    row.updated_at = datetime.utcnow()
    db.commit()
    return {
        "id": row.id, "workspace_id": row.workspace_id,
        "name": row.name, "sites": row.sites or [],
        "categories": row.categories or [], "settings": row.settings or {},
        "active": row.active,
    }


# ---------- 采集任务看板（C-030 / C-003）----------
@router.get("/jobs")
def list_jobs(limit: int = 30, page: int = 1, status: str | None = None,
              ids: str | None = None, user: str = Depends(require_user),
              x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
              db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    q = (db.query(CrawlJob)
         .filter(or_(CrawlJob.requested_by_workspace_id == ws.id,
                     CrawlJob.site.in_(allowed_sites))))
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            q = q.filter(CrawlJob.status.in_(statuses))
    if ids:
        job_ids = []
        for raw in ids.split(","):
            raw = raw.strip()
            if raw.isdigit():
                job_ids.append(int(raw))
        if job_ids:
            q = q.filter(CrawlJob.id.in_(job_ids))
        else:
            return {"total": 0, "page": page, "page_size": limit,
                    "items": [], "jobs": [], "summary": {}}
    limit = max(1, min(int(limit or 30), 500))
    page = max(1, int(page or 1))
    total = q.count()
    status_rows = (q.with_entities(CrawlJob.status, func.count(CrawlJob.id))
                   .group_by(CrawlJob.status).all())
    summary = {str(status or "unknown"): int(count or 0)
               for status, count in status_rows}
    summary["running"] = summary.get("running", 0)
    summary["queued"] = summary.get("queued", 0) + summary.get("pending", 0)
    summary["success"] = summary.get("success", 0) + summary.get("completed", 0)
    summary["failed"] = summary.get("failed", 0)
    summary["active"] = summary["running"] + summary["queued"]
    rows = (q.order_by(CrawlJob.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all())
    now = datetime.utcnow()
    def total_product_count(j: CrawlJob) -> tuple[int | None, str | None]:
        total = int(getattr(j, "total_product_count", None) or 0)
        if total > 0:
            return total, "crawl_stats_total"
        fetched = int(j.products_count or 0)
        success_rate = float(j.success_rate or 0)
        if fetched > 0 and success_rate > 0:
            return max(fetched, round(fetched * 100 / success_rate)), "crawl_success_rate"
        return None, None

    items = []
    for j in rows:
        total_count, total_source = total_product_count(j)
        items.append({
            "id": j.id, "site": j.site, "status": j.status,
            "products_count": j.products_count, "new_count": j.new_count,
            "total_product_count": total_count,
            "total_product_count_source": total_source,
            "promotion_count": j.promotion_count, "success_rate": j.success_rate,
            "duration_sec": round(j.duration_sec, 1) if j.duration_sec else None,
            "requested_by_workspace_id": j.requested_by_workspace_id,
            "requested_by_user_id": j.requested_by_user_id,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "error": j.error,
            "failure_code": j.failure_code,
            "failure_stage": j.failure_stage,
            "failure_detail": j.failure_detail,
            "retryable": _is_crawl_job_retryable(j, now=now),
            "suggested_action": j.suggested_action,
        })
    return {"total": total, "page": page, "page_size": limit,
            "items": items, "jobs": items, "summary": summary}


@router.post("/jobs/{job_id}/retry")
def retry_crawl_job(job_id: int, user: str = Depends(require_user),
                    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                    db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = set(_workspace_site_names(db, ws.id))
    job = db.get(CrawlJob, job_id)
    if not job or job.site not in allowed_sites:
        raise HTTPException(404, "任务不存在或不在当前工作区")
    if not _is_crawl_job_retryable(job):
        raise HTTPException(409, "当前任务仍在处理中或不需要重试")
    requester = _current_user(user, db)
    new_id = enqueue(job.site, trigger="admin_retry",
                     requested_by_workspace_id=ws.id,
                     requested_by_user_id=requester.id if requester else None)
    return {
        "status": "queued",
        "job_id": new_id,
        "retried_from": job_id,
        "site": job.site,
        "queued_at": datetime.utcnow().isoformat(),
    }


@router.get("/crawl/diagnostics")
def crawl_diagnostics(
    site: str | None = None,
    limit: int = 20,
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
):
    """抓取诊断：URL 漏斗、失败原因分布、最近失败事件。"""
    ws = _current_workspace(user, db, x_workspace_id)
    allowed = set(_workspace_site_names(db, ws.id, include_hidden=True))
    if site and site not in allowed:
        raise HTTPException(404, "站点不存在或不在当前工作区")
    sites = [site] if site else sorted(allowed)

    url_rows = (db.query(CrawlUrl.site, CrawlUrl.status, CrawlUrl.failure_code,
                         func.count(CrawlUrl.id))
                .filter(CrawlUrl.site.in_(sites))
                .group_by(CrawlUrl.site, CrawlUrl.status, CrawlUrl.failure_code)
                .all())
    by_site: dict[str, dict] = {
        s: {"site": s, "total": 0, "by_status": {}, "by_failure": {}}
        for s in sites
    }
    for site_name, status_value, failure_code, count in url_rows:
        row = by_site.setdefault(site_name, {
            "site": site_name, "total": 0, "by_status": {}, "by_failure": {}})
        row["total"] += int(count or 0)
        status_key = status_value or "unknown"
        row["by_status"][status_key] = row["by_status"].get(status_key, 0) + int(count or 0)
        if failure_code:
            row["by_failure"][failure_code] = row["by_failure"].get(failure_code, 0) + int(count or 0)

    failures = (db.query(CrawlFailure)
                .filter(CrawlFailure.site.in_(sites))
                .order_by(CrawlFailure.id.desc())
                .limit(max(1, min(limit, 100)))
                .all())
    failure_rows = [{
        "id": f.id,
        "site": f.site,
        "job_id": f.job_id,
        "url": f.url,
        "stage": f.stage,
        "code": f.code,
        "detail": f.detail,
        "retryable": f.retryable,
        "suggested_action": f.suggested_action,
        "http_status": f.http_status,
        "fetcher": f.fetcher,
        "proxy_tier": f.proxy_tier,
        "occurred_at": f.occurred_at.isoformat() if f.occurred_at else None,
    } for f in failures]

    failure_counts = (db.query(CrawlFailure.code, func.count(CrawlFailure.id))
                      .filter(CrawlFailure.site.in_(sites))
                      .group_by(CrawlFailure.code).all())
    return {
        "sites": list(by_site.values()),
        "failures": failure_rows,
        "failure_counts": {code or "unknown": int(count or 0)
                           for code, count in failure_counts},
    }


@router.post("/jobs/trigger")
def trigger(site: str | None = None, brand: str | None = None,
            user: str = Depends(require_user),
            x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
            db: Session = Depends(get_db)):
    """手动触发采集 —— C-003。入队任务，由 worker 执行。"""
    if not site and not brand:
        raise HTTPException(400, "需指定 site 或 brand")
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    requester = _current_user(user, db)
    if brand:
        names = [r.site for r in db.query(Site).filter(Site.brand == brand)]
        names = [n for n in names if n in set(allowed_sites)]
        if not names:
            raise HTTPException(404, "品牌不存在或当前 workspace 未启用")
    else:
        if not db.query(Site).filter(Site.site == site).first():
            raise HTTPException(404, "站点不存在")
        _require_site_in_workspace(site, allowed_sites)
        names = [site]
    jobs: list[int] = []
    reused: list[int] = []
    created: list[int] = []
    for name in names:
        active = (db.query(CrawlJob)
                  .filter(CrawlJob.site == name,
                          CrawlJob.status.in_(("pending", "running")))
                  .order_by(CrawlJob.id.desc())
                  .first())
        if active:
            jobs.append(active.id)
            reused.append(active.id)
            continue
        job_id = enqueue(name, trigger="manual",
                         requested_by_workspace_id=ws.id,
                         requested_by_user_id=requester.id if requester else None)
        jobs.append(job_id)
        created.append(job_id)
    status = "queued" if created and not reused else (
        "already_running" if reused and not created else "mixed")
    return {
        "status": status,
        "jobs": jobs,
        "created_jobs": created,
        "existing_jobs": reused,
        "count": len(jobs),
        "queued_at": datetime.utcnow().isoformat(),
    }


_PLATFORM_METHOD = {
    "shopify": "Shopify /products.json 接口直拉，无需浏览器",
    "vue_spa": "Vue SPA /api/* JSON 接口直连",
    "nuxt": "Nuxt SSR：sitemap + 商品页 JSON-LD 解析",
    "generic": "sitemap 发现 + JSON-LD/OpenGraph 多策略解析",
    "flexispot": "Playwright 取会话 token → /sapi 接口批量调",
    "vidaxl": "官方 Dropshipping API / sitemap + JSON-LD（欧洲站）",
    "vonhaus": "sitemap 扫描判别 + OpenGraph meta 解析",
}
_REVIEW_METHOD = {
    "trustpilot": "Scrapling 隐身浏览器突破 WAF + __NEXT_DATA__ 解析",
    "reviews_io": "Reviews.io 公开商家 API 直连",
    "google_map": "Scrapling 渲染商家页 + 滚动加载评论",
}


@router.get("/datasources")
def datasources(user: str = Depends(require_user),
                x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                db: Session = Depends(get_db)):
    """数据源总览 —— 每个源的平台/获取方式/状态/计数（看板「数据源」Tab）。"""
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id)
    out = []
    for s in db.query(Site).filter(Site.site.in_(allowed_sites)).all():
        sku = db.query(Product).filter(Product.site == s.site).count()
        out.append({
            "type": "product", "id": s.site,
            "name": f"{s.brand} · {s.country}", "platform": s.platform,
            "method": _PLATFORM_METHOD.get(s.platform, "—"),
            "count": sku, "unit": "SKU",
            "status": "online" if sku > 0 else "idle",
            "freq": "每日 02:00",
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
            "url": s.url,
    })
    for plat, method in _REVIEW_METHOD.items():
        n = (db.query(Review)
             .filter(Review.platform == plat,
                     Review.site.in_(allowed_sites))
             .count())
        out.append({
            "type": "review", "id": f"review_{plat}",
            "name": {"trustpilot": "Trustpilot", "reviews_io": "Reviews.io",
                     "google_map": "Google Maps"}[plat],
            "platform": plat, "method": method, "count": n, "unit": "评论",
            "status": "online" if n > 0 else "idle", "freq": "每周一",
            "last_crawled": None, "url": None,
        })
    current = _current_user(user, db)
    if _is_super_admin(current):
        sr = db.query(ShoppingResult).count()
        out.append({
            "type": "shopping", "id": "google_shopping", "name": "Google Shopping",
            "platform": "google_shopping",
            "method": "Scrapling 渲染 udm=28 购物结果页",
            "count": sr, "unit": "结果", "status": "online" if sr > 0 else "idle",
            "freq": "每周一", "last_crawled": None, "url": None,
        })
    return out


@router.get("/proxy-status")
def proxy_status():
    """代理池状态 —— C-010。"""
    return pool_status()


# ---------- API 密钥管理（仅登录用户，供 Agent 接入数据 API）----------
@router.get("/keys")
def list_keys(include_inactive: bool = False,
              user: str = Depends(require_user),
              x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
              db: Session = Depends(get_db)):
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    q = db.query(ApiKey)
    if _is_super_admin(u):
        q = q.filter(ApiKey.workspace_id == ws.id)
    else:
        q = q.filter(ApiKey.owner_user_id == u.id,
                     ApiKey.workspace_id == ws.id)
    if not include_inactive:
        q = q.filter(ApiKey.active.is_(True))
    rows = q.order_by(ApiKey.id.desc()).all()
    return [_key_response(k) for k in rows]


@router.post("/keys")
def create_key(payload: dict, user: str = Depends(require_user),
               x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
               db: Session = Depends(get_db)):
    """新建 API 密钥 —— 明文仅此一次返回。"""
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    is_admin = _is_admin_user(u)
    raw = gen_key()
    payload = payload or {}
    scopes = (normalize_scopes(payload.get("scopes") or DEFAULT_API_KEY_SCOPES)
              if is_admin else list(DEFAULT_API_KEY_SCOPES))
    quota = (_parse_monthly_credit_quota(payload.get("monthly_credit_quota"))
             if is_admin else None)
    owner_user_id = payload.get("owner_user_id") if is_admin else u.id
    workspace_id = int(payload.get("workspace_id") or ws.id) if _is_super_admin(u) else ws.id
    if not db.get(Workspace, workspace_id):
        raise HTTPException(400, "workspace_id 不存在")
    if owner_user_id is not None:
        owner = db.get(User, int(owner_user_id))
        if not owner:
            raise HTTPException(400, "owner_user_id 不存在")
        if not _user_has_workspace_access(db, owner, workspace_id):
            raise HTTPException(400, "owner_user_id 不属于该 workspace")
    k = ApiKey(name=(payload or {}).get("name") or "未命名",
               key_prefix=key_short(raw), key_hash=hash_key(raw),
               scopes=scopes,
               monthly_credit_quota=quota,
               owner_user_id=owner_user_id,
               workspace_id=workspace_id)
    db.add(k)
    db.commit()
    return {**_key_response(k), "key": raw,
            "note": "请立即保存，密钥明文不再展示"}


@router.patch("/keys/{key_id}")
def update_key(key_id: int, payload: dict, user: str = Depends(require_user),
               x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
               db: Session = Depends(get_db)):
    """更新 API key 元数据、scope、quota 或启停状态；不返回明文 key。"""
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    is_admin = _is_admin_user(u)
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "密钥不存在")
    if k.workspace_id != ws.id and not _is_super_admin(u):
        raise HTTPException(404, "密钥不存在")
    if not is_admin and (k.owner_user_id != u.id or k.workspace_id != ws.id):
        raise HTTPException(404, "密钥不存在")
    payload = payload or {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name 不能为空")
        k.name = name
    if "scopes" in payload and is_admin:
        k.scopes = normalize_scopes(payload.get("scopes"))
    if "monthly_credit_quota" in payload and is_admin:
        k.monthly_credit_quota = _parse_monthly_credit_quota(
            payload.get("monthly_credit_quota"))
    if "owner_user_id" in payload and is_admin:
        owner_id = payload.get("owner_user_id")
        if owner_id is not None:
            owner = db.get(User, int(owner_id))
            if not owner:
                raise HTTPException(400, "owner_user_id 不存在")
            target_workspace_id = int(payload.get("workspace_id") or k.workspace_id or ws.id)
            if not _user_has_workspace_access(db, owner, target_workspace_id):
                raise HTTPException(400, "owner_user_id 不属于该 workspace")
        k.owner_user_id = owner_id
    if "workspace_id" in payload and _is_super_admin(u):
        workspace_id = int(payload.get("workspace_id"))
        if not db.get(Workspace, workspace_id):
            raise HTTPException(400, "workspace_id 不存在")
        if k.owner_user_id:
            owner = db.get(User, k.owner_user_id)
            if owner and not _user_has_workspace_access(db, owner, workspace_id):
                raise HTTPException(400, "owner_user_id 不属于目标 workspace")
        k.workspace_id = workspace_id
    if "active" in payload:
        if not isinstance(payload.get("active"), bool):
            raise HTTPException(400, "active 必须是 boolean")
        k.active = payload["active"]
    db.commit()
    db.refresh(k)
    return _key_response(k)


def _key_response(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "key_prefix": (k.key_prefix or "") + "…",
        "active": k.active,
        "request_count": k.request_count,
        "scopes": api_key_scopes(k),
        "monthly_credit_quota": k.monthly_credit_quota,
        "owner_user_id": k.owner_user_id,
        "workspace_id": k.workspace_id,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used": k.last_used.isoformat() if k.last_used else None,
    }


def _parse_monthly_credit_quota(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HTTPException(400, "monthly_credit_quota 必须是非负整数")
    try:
        quota = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "monthly_credit_quota 必须是非负整数")
    if quota < 0:
        raise HTTPException(400, "monthly_credit_quota 必须是非负整数")
    return quota


@router.delete("/keys/{key_id}")
def revoke_key(key_id: int, user: str = Depends(require_user),
               x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
               db: Session = Depends(get_db)):
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "密钥不存在")
    if k.workspace_id != ws.id and not _is_super_admin(u):
        raise HTTPException(404, "密钥不存在")
    if not _is_admin_user(u) and (k.owner_user_id != u.id or k.workspace_id != ws.id):
        raise HTTPException(404, "密钥不存在")
    k.active = False
    db.commit()
    return {"status": "revoked", "id": key_id}


# ---------- Admin：用户与内部邀请码 ----------
def _require_super_admin(user: str, db: Session) -> User:
    u = _require_dashboard_user(user, db)
    if not _is_super_admin(u):
        raise HTTPException(403, "需要 super_admin 权限")
    return u


@router.post("/admin/workspaces")
def admin_create_workspace(payload: dict, user: str = Depends(require_user),
                           db: Session = Depends(get_db)):
    actor = _require_super_admin(user, db)
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    slug = str(payload.get("slug") or "").strip().lower()
    if not name or not slug:
        raise HTTPException(400, "name/slug 不能为空")
    if db.query(Workspace).filter(or_(Workspace.name == name,
                                      Workspace.slug == slug)).first():
        raise HTTPException(409, "workspace 已存在")
    row = Workspace(name=name, slug=slug,
                    type=payload.get("type") or "customer",
                    status=payload.get("status") or "active")
    db.add(row)
    db.flush()
    record_audit(db, actor_user_id=getattr(actor, "id", None),
                 actor_name=getattr(actor, "username", user),
                 action="workspace.create", target_type="workspace",
                 target_id=str(row.id) if row.id is not None else None,
                 detail={"name": name, "slug": slug})
    db.commit()
    db.refresh(row)
    return _workspace_response(row)


@router.patch("/admin/workspaces/{workspace_id}")
def admin_update_workspace(workspace_id: int, payload: dict,
                           user: str = Depends(require_user),
                           db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    row = db.get(Workspace, workspace_id)
    if not row:
        raise HTTPException(404, "workspace 不存在")
    payload = payload or {}
    if "name" in payload:
        row.name = str(payload.get("name") or "").strip() or row.name
    if "status" in payload:
        if payload["status"] not in {"active", "disabled"}:
            raise HTTPException(400, "status 必须是 active/disabled")
        row.status = payload["status"]
    db.commit()
    return _workspace_response(row)


def _workspace_site_response(row: WorkspaceSite, site: Site | None = None) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "site": row.site,
        "display_name": row.display_name,
        "enabled": row.enabled,
        "hidden": row.hidden,
        "sort_order": row.sort_order,
        "target_coverage_pct": row.target_coverage_pct,
        "target_sku_count": row.target_sku_count,
        "report_config": row.report_config,
        "brand": site.brand if site else None,
        "country": site.country if site else None,
        "url": site.url if site else None,
        "platform": site.platform if site else None,
    }


@router.get("/admin/workspaces/{workspace_id}/sites")
def admin_list_workspace_sites(workspace_id: int,
                               user: str = Depends(require_user),
                               db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    rows = (db.query(WorkspaceSite)
            .filter(WorkspaceSite.workspace_id == workspace_id)
            .order_by(WorkspaceSite.sort_order, WorkspaceSite.id).all())
    sites = {s.site: s for s in db.query(Site).all()}
    return [_workspace_site_response(row, sites.get(row.site)) for row in rows]


@router.post("/admin/workspaces/{workspace_id}/sites")
def admin_add_workspace_site(workspace_id: int, payload: dict,
                             user: str = Depends(require_user),
                             db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    if not db.get(Workspace, workspace_id):
        raise HTTPException(404, "workspace 不存在")
    payload = payload or {}
    site_code = str(payload.get("site") or "").strip()
    site = db.query(Site).filter(Site.site == site_code).first()
    if not site:
        raise HTTPException(404, "全局站点不存在")
    row = (db.query(WorkspaceSite)
           .filter(WorkspaceSite.workspace_id == workspace_id,
                   WorkspaceSite.site == site_code).first())
    if row:
        row.enabled = True
        row.hidden = bool(payload.get("hidden", row.hidden))
        if "target_sku_count" in payload:
            row.target_sku_count = _optional_int(payload.get("target_sku_count"))
    else:
        row = WorkspaceSite(
            workspace_id=workspace_id,
            site=site_code,
            display_name=payload.get("display_name") or f"{site.brand} · {site.country}",
            enabled=bool(payload.get("enabled", True)),
            hidden=bool(payload.get("hidden", False)),
            sort_order=int(payload.get("sort_order") or 0),
            target_coverage_pct=payload.get("target_coverage_pct"),
            target_sku_count=_optional_int(payload.get("target_sku_count")),
            report_config=payload.get("report_config"),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return _workspace_site_response(row, site)


@router.patch("/admin/workspaces/{workspace_id}/sites/{workspace_site_id}")
def admin_update_workspace_site(workspace_id: int, workspace_site_id: int,
                                payload: dict, user: str = Depends(require_user),
                                db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    row = db.get(WorkspaceSite, workspace_site_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(404, "workspace site 不存在")
    payload = payload or {}
    for field in ("display_name", "report_config"):
        if field in payload:
            setattr(row, field, payload[field])
    for field in ("enabled", "hidden"):
        if field in payload:
            setattr(row, field, bool(payload[field]))
    if "sort_order" in payload:
        row.sort_order = int(payload["sort_order"])
    if "target_coverage_pct" in payload:
        row.target_coverage_pct = payload["target_coverage_pct"]
    if "target_sku_count" in payload:
        row.target_sku_count = _optional_int(payload.get("target_sku_count"))
    db.commit()
    return _workspace_site_response(row, db.query(Site).filter(Site.site == row.site).first())


@router.delete("/admin/workspaces/{workspace_id}/sites/{workspace_site_id}")
def admin_delete_workspace_site(workspace_id: int, workspace_site_id: int,
                                user: str = Depends(require_user),
                                db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    row = db.get(WorkspaceSite, workspace_site_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(404, "workspace site 不存在")
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": workspace_site_id}


@router.get("/admin/workspaces/{workspace_id}/members")
def admin_list_workspace_members(workspace_id: int,
                                 user: str = Depends(require_user),
                                 db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    rows = (db.query(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .filter(WorkspaceMember.workspace_id == workspace_id)
            .order_by(WorkspaceMember.id).all())
    return [{
        "id": m.id,
        "workspace_id": m.workspace_id,
        "user_id": u.id,
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "role": m.role,
        "status": m.status,
    } for m, u in rows]


@router.post("/admin/workspaces/{workspace_id}/members")
def admin_add_workspace_member(workspace_id: int, payload: dict,
                               user: str = Depends(require_user),
                               db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    member_user = db.get(User, int((payload or {}).get("user_id") or 0))
    if not member_user:
        raise HTTPException(404, "用户不存在")
    row = (db.query(WorkspaceMember)
           .filter(WorkspaceMember.workspace_id == workspace_id,
                   WorkspaceMember.user_id == member_user.id).first())
    if not row:
        row = WorkspaceMember(workspace_id=workspace_id,
                              user_id=member_user.id)
        db.add(row)
    row.role = (payload or {}).get("role") or row.role or "member"
    row.status = (payload or {}).get("status") or "active"
    if not member_user.default_workspace_id:
        member_user.default_workspace_id = workspace_id
    db.commit()
    return {"id": row.id, "workspace_id": workspace_id,
            "user_id": member_user.id, "role": row.role, "status": row.status}


@router.patch("/admin/workspaces/{workspace_id}/members/{member_id}")
def admin_update_workspace_member(workspace_id: int, member_id: int,
                                  payload: dict, user: str = Depends(require_user),
                                  db: Session = Depends(get_db)):
    _require_super_admin(user, db)
    row = db.get(WorkspaceMember, member_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(404, "成员不存在")
    if "role" in (payload or {}):
        row.role = payload["role"]
    if "status" in (payload or {}):
        row.status = payload["status"]
    db.commit()
    return {"id": row.id, "workspace_id": row.workspace_id,
            "user_id": row.user_id, "role": row.role, "status": row.status}


@router.get("/admin/users")
def admin_list_users(user: str = Depends(require_user),
                     x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                     db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    if _is_super_admin(admin):
        rows = db.query(User).order_by(User.id.desc()).all()
    else:
        ws = _current_workspace(user, db, x_workspace_id)
        rows = (db.query(User)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .filter(WorkspaceMember.workspace_id == ws.id,
                        WorkspaceMember.status == "active")
                .order_by(User.id.desc()).all())
    memberships = {}
    for m in db.query(WorkspaceMember).all():
        memberships.setdefault(m.user_id, []).append(m.workspace_id)
    return [_public_user(u) | {
        "workspace_ids": memberships.get(u.id, []),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "locked_until": u.locked_until.isoformat() if u.locked_until else None,
    } for u in rows]


@router.post("/admin/users")
def admin_create_user(payload: dict, user: str = Depends(require_user),
                      x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                      db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    payload = payload or {}
    ws = _current_workspace(user, db, x_workspace_id)
    workspace_id = (int(payload.get("workspace_id") or ws.id)
                    if _is_super_admin(admin) else ws.id)
    if not db.get(Workspace, workspace_id):
        raise HTTPException(400, "workspace_id 不存在")
    try:
        username = validate_username(payload.get("username", ""))
        email = validate_email(payload.get("email", ""))
        password = payload.get("password") or secrets.token_urlsafe(10) + "A1"
        validate_password_strength(password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if db.query(User).filter(or_(User.username == username, User.email == email)).first():
        raise HTTPException(409, "用户名或邮箱已存在")
    role = payload.get("role") or "user"
    if role not in {"admin", "user", "viewer"}:
        raise HTTPException(400, "role 必须是 admin/user/viewer")
    row = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=role,
        default_workspace_id=workspace_id,
        status=payload.get("status") or "active",
        display_name=(payload.get("display_name") or username).strip(),
        email_verified=bool(payload.get("email_verified", False)),
        password_changed_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=row.id,
                           role="admin" if role == "admin" else "member"))
    db.commit()
    db.refresh(row)
    return {**_public_user(row), "temporary_password": password,
            "note": "请立即保存，临时密码不再展示"}


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: dict,
                      user: str = Depends(require_user),
                      x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                      db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, "用户不存在")
    if not _is_super_admin(admin):
        ws = _current_workspace(user, db, x_workspace_id)
        if not _user_has_workspace_access(db, row, ws.id):
            raise HTTPException(404, "用户不存在")
    payload = payload or {}
    if "display_name" in payload:
        row.display_name = str(payload.get("display_name") or "").strip()[:80]
    if "role" in payload:
        if payload["role"] not in {"admin", "user", "viewer"}:
            raise HTTPException(400, "role 必须是 admin/user/viewer")
        row.role = payload["role"]
    if "status" in payload:
        if payload["status"] not in {"active", "disabled"}:
            raise HTTPException(400, "status 必须是 active/disabled")
        row.status = payload["status"]
    db.commit()
    return _public_user(row)


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, payload: dict | None = None,
                         user: str = Depends(require_user),
                         x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                         db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, "用户不存在")
    if not _is_super_admin(admin):
        ws = _current_workspace(user, db, x_workspace_id)
        if not _user_has_workspace_access(db, row, ws.id):
            raise HTTPException(404, "用户不存在")
    password = (payload or {}).get("password") or secrets.token_urlsafe(10) + "A1"
    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    row.password_hash = hash_password(password)
    row.password_changed_at = datetime.utcnow()
    for session in db.query(UserSession).filter(UserSession.user_id == row.id,
                                                UserSession.revoked_at.is_(None)).all():
        session.revoked_at = datetime.utcnow()
    db.commit()
    return {"id": row.id, "temporary_password": password,
            "note": "请立即保存，临时密码不再展示"}


def _invite_response(invite: InviteCode) -> dict:
    return {
        "id": invite.id,
        "code_prefix": (invite.code_prefix or "") + "…",
        "active": invite.active,
        "max_uses": invite.max_uses,
        "used_count": invite.used_count or 0,
        "default_role": invite.default_role or "user",
        "target_type": invite.target_type or "workspace",
        "created_by_user_id": invite.created_by_user_id,
        "workspace_id": invite.workspace_id,
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "last_used_at": invite.last_used_at.isoformat() if invite.last_used_at else None,
    }


@router.get("/admin/invites")
def admin_list_invites(user: str = Depends(require_user),
                       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                       db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    q = db.query(InviteCode)
    if not _is_super_admin(admin):
        ws = _current_workspace(user, db, x_workspace_id)
        q = q.filter(InviteCode.workspace_id == ws.id)
    rows = q.order_by(InviteCode.id.desc()).all()
    return [_invite_response(row) for row in rows]


@router.post("/admin/invites")
def admin_create_invite(payload: dict | None = None,
                        user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    payload = payload or {}
    ws = _current_workspace(user, db, x_workspace_id)
    target_type = payload.get("target_type") or "workspace"
    if target_type not in {"workspace", "new_workspace"}:
        raise HTTPException(400, "target_type 必须是 workspace/new_workspace")
    workspace_id = None
    if target_type == "workspace":
        workspace_id = (int(payload.get("workspace_id") or ws.id)
                        if _is_super_admin(admin) else ws.id)
        if not db.get(Workspace, workspace_id):
            raise HTTPException(400, "workspace_id 不存在")
    max_uses = int(payload.get("max_uses") or 1)
    if max_uses <= 0:
        raise HTTPException(400, "max_uses 必须大于 0")
    days = int(payload.get("expires_in_days") or 7)
    if days <= 0:
        raise HTTPException(400, "expires_in_days 必须大于 0")
    role = payload.get("default_role") or "user"
    if role not in {"user", "viewer"}:
        raise HTTPException(400, "邀请码默认角色只能是 user/viewer")
    raw = _generate_invite_code()
    row = InviteCode(
        code_prefix=raw[:10],
        code_hash=hash_secret(raw),
        created_by_user_id=admin.id,
        workspace_id=workspace_id,
        target_type=target_type,
        max_uses=max_uses,
        used_count=0,
        active=True,
        default_role=role,
        expires_at=datetime.utcnow() + timedelta(days=days),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {**_invite_response(row), "code": raw,
            "note": "请立即保存，邀请码明文不再展示"}


@router.patch("/admin/invites/{invite_id}")
def admin_update_invite(invite_id: int, payload: dict,
                        user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    admin = _require_admin(user, db)
    row = db.get(InviteCode, invite_id)
    if not row:
        raise HTTPException(404, "邀请码不存在")
    if not _is_super_admin(admin):
        ws = _current_workspace(user, db, x_workspace_id)
        if row.workspace_id != ws.id:
            raise HTTPException(404, "邀请码不存在")
    if "active" in (payload or {}):
        if not isinstance(payload["active"], bool):
            raise HTTPException(400, "active 必须是 boolean")
        row.active = payload["active"]
    db.commit()
    return _invite_response(row)


@router.get("/scheduler")
def scheduler_jobs():
    """定时采集任务列表 —— C-001。"""
    try:
        from ..scheduler import list_scheduled_jobs
        return list_scheduled_jobs()
    except Exception:
        return []


# ---------- Excel 导出（API-006，Token 走 query 参数以支持浏览器直接下载）----------
@public_router.get("/export/products")
def export_products(token: str, site: str | None = None,
                    sites: str | None = None,
                    workspace_id: int | None = None,
                    categories: str | None = None,
                    tab: str = Query("all", pattern="^(all|bestseller|new)$"),
                    search: str | None = None,
                    status: str | None = None,
                    min_price: float | None = None,
                    max_price: float | None = None,
                    category: str | None = None,
                    min_rating: float | None = None,
                    max_rating: float | None = None,
                    min_reviews: int | None = None,
                    max_reviews: int | None = None,
                    min_sales: int | None = None,
                    max_sales: int | None = None,
                    min_revenue: float | None = None,
                    max_revenue: float | None = None,
                    min_variants: int | None = None,
                    max_variants: int | None = None,
                    has_video: bool | None = None,
                    free_shipping: bool | None = None,
                    created_from: str | None = None,
                    created_to: str | None = None,
                    scope: str | None = None,
                    export_scope: str = Query("all", pattern="^(all|page)$"),
                    page: int = 1,
                    page_size: int = 50,
                    format: str = "xlsx",
                    include_price_history: bool = False,
                    include_voc: bool = False,
                    include_images: bool = True,
                    split_by_category: bool = False,
                    db: Session = Depends(get_db)):
    """导出产品数据，支持多格式 + 4 个 toggle。
    - site=foo：单站点；sites=a,b,c：多站点（| 或 , 分隔）
    - categories=cat1|cat2：品类过滤（无品类则全站）
    - format=xlsx|csv|json|zip
    - include_price_history / include_voc：xlsx 额外加 sheet
    - include_images：xlsx 全字段表是否含 image_urls 列
    - split_by_category：xlsx 是否按品类拆 sheet
    """
    u = _user_from_token(db, token)
    ws = _current_workspace(u.username, db, str(workspace_id) if workspace_id else None)
    _require_report_editor(u.username, db, ws)
    allowed_sites = _workspace_site_names(db, ws.id)
    site_list = _scoped_sites_from_params(site, sites, allowed_sites)
    if not site_list:
        raise HTTPException(404, "当前 workspace 没有可导出的站点")
    cat_list = [c.strip() for c in categories.split("|")
                if c.strip()] if categories else None

    from ..export import export_workbook, export_csv, export_json, export_zip
    site_suffix = (site_list[0] if site_list and len(site_list) == 1
                   else f"{len(site_list)}sites" if site_list else "all")
    cat_suffix = "_".join(c.replace("/","-") for c in (cat_list or []))[:40]
    base_name = (f"smart-crawler_{site_suffix}"
                 f"{('_'+cat_suffix) if cat_suffix else ''}_{datetime.now():%Y%m%d}")

    fmt = (format or "xlsx").lower()
    workbook_kwargs = dict(
        include_price_history=include_price_history,
        include_voc=include_voc,
        include_images=include_images,
        split_by_category=split_by_category,
    )

    if fmt == "csv":
        data = export_csv(db, site_list, categories=cat_list)
        return StreamingResponse(
            io.BytesIO(data), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.csv"'},
        )
    if fmt == "json":
        data = export_json(db, site_list, categories=cat_list)
        return StreamingResponse(
            io.BytesIO(data), media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.json"'},
        )
    if fmt == "zip":
        data = export_zip(db, site_list or [], categories=cat_list,
                          **workbook_kwargs)
        return StreamingResponse(
            io.BytesIO(data), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'},
        )

    page_filter_active = any([
        tab != "all", search, status, category,
        min_price is not None, max_price is not None,
        min_rating is not None, max_rating is not None,
        min_reviews is not None, max_reviews is not None,
        min_sales is not None, max_sales is not None,
        min_revenue is not None, max_revenue is not None,
        min_variants is not None, max_variants is not None,
        has_video is not None, free_shipping is not None,
        created_from, created_to,
    ])
    if scope == "products" or page_filter_active:
        import pandas as pd
        from ..export import products_sample_df_from_rows
        q = _filtered_products_query(
            db, site_list, site=site, tab=tab, search=search, status=status,
            min_price=min_price, max_price=max_price, category=category,
            min_rating=min_rating, max_rating=max_rating,
            min_reviews=min_reviews, max_reviews=max_reviews,
            min_sales=min_sales, max_sales=max_sales,
            min_revenue=min_revenue, max_revenue=max_revenue,
            min_variants=min_variants, max_variants=max_variants,
            has_video=has_video, free_shipping=free_shipping,
            created_from=created_from, created_to=created_to,
        )
        q = q.order_by(*_product_order_cols(tab))
        if export_scope == "page":
            page_num = max(1, int(page or 1))
            size = min(max(1, int(page_size or 50)), 500)
            q = q.offset((page_num - 1) * size).limit(size)
        rows = q.all()
        variant_counts = _variant_counts_for_products(db, rows)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            products_sample_df_from_rows(rows, variant_counts).to_excel(
                writer, index=False, sheet_name="产品分析")
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f'attachment; filename="{base_name}_filtered.xlsx"'},
        )

    # 默认 xlsx
    data = export_workbook(db, site_list, categories=cat_list, **workbook_kwargs)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{base_name}.xlsx"'},
    )


@public_router.get("/export/promotions")
def export_promotions(token: str, site: str | None = None,
                      workspace_id: int | None = None,
                      search: str | None = None,
                      type: str | None = None,
                      date_from: str | None = None,
                      date_to: str | None = None,
                      export_scope: str = Query("all", pattern="^(all|page)$"),
                      page: int = 1,
                      page_size: int = 50,
                      db: Session = Depends(get_db)):
    """导出销售促销，筛选条件与 /api/promotions 保持一致。"""
    u = _user_from_token(db, token)
    ws = _current_workspace(u.username, db, str(workspace_id) if workspace_id else None)
    _require_report_editor(u.username, db, ws)
    allowed_sites = _workspace_site_names(db, ws.id)
    q = _filtered_promotions_query(
        db, allowed_sites, site=site, search=search, type=type,
        date_from=date_from, date_to=date_to,
    )
    q = q.order_by(Promotion.detected_time.desc().nullslast(),
                   Promotion.id.desc())
    if export_scope == "page":
        page_num = max(1, int(page or 1))
        size = min(max(1, int(page_size or 50)), 500)
        q = q.offset((page_num - 1) * size).limit(size)
    rows = q.all()
    product_by_key: dict[tuple[str | None, str | None], Product] = {}
    if rows:
        row_sites = sorted({row.site for row in rows if row.site})
        row_skus = sorted({row.sku for row in rows if row.sku})
        if row_sites and row_skus:
            product_by_key = {
                (product.site, product.sku): product
                for product in (
                    db.query(Product)
                    .filter(Product.site.in_(row_sites), Product.sku.in_(row_skus))
                    .all()
                )
            }
    import pandas as pd
    from ..export import promotions_sample_df_from_rows
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        promotions_sample_df_from_rows(rows, product_by_key).to_excel(
            writer, index=False, sheet_name="销售促销")
    output.seek(0)
    site_suffix = site or "all"
    base_name = f"smart-crawler_promotions_{site_suffix}_{datetime.now():%Y%m%d}"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{base_name}.xlsx"'},
    )


@public_router.get("/export/product-trend")
def export_product_trend(token: str, pid: int,
                         workspace_id: int | None = None,
                         granularity: str = Query("day", pattern="^(day|week|month)$"),
                         date_from: str | None = None,
                         date_to: str | None = None,
                         promo_search: str | None = None,
                         promo_type: str | None = None,
                         promo_sku: str | None = None,
                         export_scope: str = Query("all", pattern="^(all|page)$"),
                         promo_page: int = Query(1, ge=1),
                         promo_page_size: int = Query(20, ge=1, le=500),
                         db: Session = Depends(get_db)):
    """导出单品趋势分析，筛选条件与 /api/products/{pid}/trend 保持一致。"""
    from openpyxl import Workbook

    u = _user_from_token(db, token)
    ws = _current_workspace(u.username, db, str(workspace_id) if workspace_id else None)
    _require_report_editor(u.username, db, ws)
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    _require_site_in_workspace(p.site, _workspace_site_names(db, ws.id))
    payload = _build_product_trend_payload(
        db, p, granularity=granularity, date_from=date_from, date_to=date_to,
        promo_search=promo_search, promo_type=promo_type, promo_sku=promo_sku,
        promo_page=promo_page if export_scope == "page" else 1,
        promo_page_size=promo_page_size if export_scope == "page" else None,
    )
    currency = payload.get("summary", {}).get("currency") or _display_currency(p.currency, p.site)

    wb = Workbook()
    sh = wb.active
    sh.title = "Sales Trends"
    sh.append(["Date", "Sales", "Revenue", "Ratings", "Reviews",
               "Sale Price", "Price", "Points"])
    for row in payload["trend"]:
        sh.append([
            row.get("date"),
            row.get("estimated_sales") or 0,
            row.get("estimated_revenue") or 0,
            row.get("avg_rating"),
            row.get("review_total"),
            _report_money(row.get("sale_price"), currency),
            _report_money(row.get("original_price"), currency),
            row.get("points", 1),
        ])

    promo_sh = wb.create_sheet("Sales Promotion")
    promo_sh.append(["Updated Time", "SKU", "Products Details", "Product Image",
                     "Type", "Name", "Discount", "Pre-price", "Post-price",
                     "Threshold", "Start Time", "End Time"])
    for promo in payload["promotions"]:
        discount = promo.get("discount_percent")
        if discount is not None:
            discount = f"{discount}%"
        else:
            original = promo.get("original_price")
            promotion = promo.get("promotion_price")
            if original is not None and promotion is not None and original > promotion:
                discount = _report_money(round(original - promotion, 2), currency)
            else:
                discount = _report_money(promotion, currency)
        promo_sh.append([
            _report_time(promo.get("detected_time")),
            promo.get("sku"),
            promo.get("product_title"),
            promo.get("product_image"),
            _report_promo_type(promo.get("promotion_type")),
            promo.get("promotion_name") or _report_promo_type(promo.get("promotion_type")),
            discount,
            _report_money(promo.get("original_price"), currency),
            _report_money(promo.get("promotion_price"), currency),
            promo.get("threshold"),
            _report_time(promo.get("start_time")),
            _report_time(promo.get("end_time")),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    sku = re.sub(r"[^A-Za-z0-9_-]+", "_", p.sku or str(pid))[:80]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="product_trend_{sku}_{datetime.now():%Y%m%d}.xlsx"'},
    )


# ---------- 跨站点品类列表（drawer 用）----------
@router.get("/categories/cross")
def categories_cross(sites: str = "", user: str = Depends(require_user),
                     x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                     db: Session = Depends(get_db)):
    """跨站点品类汇总。优先从 Category 表取，缺数据时降级到 Product.category_path 去重。
    返回 {site: [{name, product_count, source, parent_id, level, category_id}], ...}。
    parent_id / level / category_id 用于前端建树（无 Category 表数据时为 null）。
    """
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = set(_workspace_site_names(db, ws.id))
    site_list = [s.strip() for s in sites.replace(",", "|").split("|")
                 if s.strip() and s.strip() in allowed_sites]
    if not site_list:
        return {}
    result: dict[str, list] = {}
    for s in site_list:
        cats = db.query(Category).filter(Category.site == s).all()
        if cats:
            result[s] = [{
                "name": c.category_name or "(unnamed)",
                "category_id": c.category_id,
                "parent_id": c.parent_id,
                "level": c.level,
                "product_count": c.product_count or 0,
                "source": "category-tree",
            } for c in cats if c.category_name]
        else:
            rows = db.query(Product.category_path, func.count(Product.id)).filter(
                Product.site == s,
                Product.category_path.isnot(None)).group_by(
                Product.category_path).all()
            result[s] = [{
                "name": p, "category_id": None, "parent_id": None,
                "level": None, "product_count": n, "source": "product-path"
            } for p, n in rows if p]
    return result


# ---------- 导出预览（drawer 实时统计）----------
@public_router.get("/export/preview")
def export_preview(token: str, site: str | None = None,
                   sites: str | None = None,
                   workspace_id: int | None = None,
                   categories: str | None = None,
                   include_price_history: bool = False,
                   include_voc: bool = False,
                   db: Session = Depends(get_db)):
    """轻量 count 查询返回 7 项预览统计。前端实时调用。"""
    u = _user_from_token(db, token)
    ws = _current_workspace(u.username, db, str(workspace_id) if workspace_id else None)
    allowed_sites = _workspace_site_names(db, ws.id)
    site_list = _scoped_sites_from_params(site, sites, allowed_sites)
    if not site_list:
        raise HTTPException(404, "当前 workspace 没有可预览导出的站点")
    cat_list = [c.strip() for c in categories.split("|")
                if c.strip()] if categories else None

    # SKU 查询
    from sqlalchemy import or_
    pq = db.query(Product)
    if site_list:
        pq = pq.filter(Product.site.in_(site_list)) if len(site_list) > 1 \
            else pq.filter(Product.site == site_list[0])
    if cat_list:
        pq = pq.filter(or_(*[Product.category_path.ilike(f"%{c}%")
                             for c in cat_list]))
    sku_count = pq.count()
    skus = [r[0] for r in pq.with_entities(Product.sku).all() if r[0]]

    # 促销
    promo_q = db.query(Promotion)
    if site_list:
        promo_q = promo_q.filter(Promotion.site.in_(site_list))
    if skus:
        promo_q = promo_q.filter(Promotion.sku.in_(skus))
    promo_count = promo_q.count() if skus else 0

    # 品类数
    cq = db.query(Product.category_path).filter(Product.category_path.isnot(None))
    if site_list:
        cq = cq.filter(Product.site.in_(site_list))
    if cat_list:
        cq = cq.filter(or_(*[Product.category_path.ilike(f"%{c}%")
                             for c in cat_list]))
    category_count = cq.distinct().count()

    # 价格历史 / 评论（仅 toggle 开时计数）
    price_history_rows = 0
    review_count = 0
    if include_price_history and skus:
        from datetime import date, timedelta
        cutoff = date.today() - timedelta(days=90)
        price_history_rows = db.query(PriceHistory).filter(
            PriceHistory.date >= cutoff,
            PriceHistory.site.in_(site_list),
            PriceHistory.sku.in_(skus)).count()
    if include_voc and skus:
        review_count = db.query(Review).filter(
            Review.site.in_(site_list),
            Review.sku.in_(skus)).count()
        # 限 10/sku：实际导出量上限 = sku_count × 10
        review_count = min(review_count, len(skus) * 10)

    # 文件大小估算：每 SKU ~5KB xlsx + price ~80B/行 + review ~1KB/条
    size_bytes = (sku_count * 5_000
                  + price_history_rows * 80
                  + review_count * 1_000)
    file_size_mb = round(size_bytes / 1_000_000, 2)

    # 耗时估算：每 SKU 0.03s
    duration_sec = max(2, round(sku_count * 0.03 + price_history_rows * 0.0005
                                + review_count * 0.01))

    return {
        "category_count": category_count,
        "sku_count": sku_count,
        "promo_count": promo_count,
        "price_history_rows": price_history_rows,
        "review_count": review_count,
        "file_size_mb": file_size_mb,
        "duration_sec": duration_sec,
    }


# ---------- 代理池状态 ----------
@router.get("/proxy/status")
def proxy_status_endpoint(db: Session = Depends(get_db)):
    """代理池状态：总数 / 可用 / 各代理失败率。"""
    from ..proxy_pool import pool_status
    from ..proxy_health import proxy_health_summary
    status = pool_status()
    status["health"] = proxy_health_summary(db)
    return status


@router.post("/proxy/reload")
def proxy_reload(user: str = Depends(require_user),
                 db: Session = Depends(get_db)):
    """热重载 proxies.txt（添加/删除代理后调用）。"""
    _require_admin(user, db)
    from ..proxy_pool import reload_pool
    reload_pool()
    from ..proxy_pool import pool_status
    return {"reloaded": True, "status": pool_status()}


# ---------- 数据覆盖率（3B 仪表盘）----------
# 估算全量 SKU 数。
# Vidaxl: 改为从 data/sitemap_totals.json 读取真实 sitemap 总数（爬虫每次跑都写）。
#   首次跑前 sidecar 文件不存在 → 回退到 _FULL_ESTIMATES 兜底数。
# 其他平台：保留人工校准值。
_FULL_ESTIMATES: dict[str, int] = {
    # Vidaxl 兜底（仅在 sitemap 总数尚未落地时使用，落地后被 sidecar 覆盖）
    "vidaxl_de": 12000, "vidaxl_uk": 8000, "vidaxl_fr": 8000,
    "vidaxl_es": 12000, "vidaxl_it": 8000, "vidaxl_nl": 6000,
    "vidaxl_pl": 8000, "vidaxl_pt": 12000, "vidaxl_ro": 12000,
    "vidaxl_ie": 8000, "vidaxl_us": 8000, "vidaxl_ca": 6000,
    # SONGMICS: Shopify 一次拉完，已是全量
    # Costway: API 分页采集，已接近全量
    # 其他：缺数据，0 = 不计入覆盖率
}

# 来自《标杆平台验收报告.xlsx / 爬取数据》里可明确映射到站点编码的人工统计目标。
# workspace_sites.target_sku_count 仍然优先；这里作为默认验收口径，避免只能靠人肉比对偏差。
_ACCEPTANCE_TARGETS: dict[str, int] = {
    "idealo_de": 824,
    "costway_es": 10435,
    "costway_pl": 12846,
    "costway_fr": 13234,
    "vidaxl_fr": 177694,
    "cratebarrel_us": 14044,
    "vidaxl_de": 177352,
    "vidaxl_es": 138819,
    "vidaxl_ie": 88104,
    "vidaxl_it": 175282,
    "vidaxl_pt": 176957,
    "costway_uk": 8643,
    "yaheetech_uk": 180,
    "costway_us": 13162,
    "article_us": 2537,
    "vidaxl_ro": 175824,
    "costway_ca": 13159,
    "costway_nl": 12615,
    "homary_us": 3039,
    "woltu_de": 1130,
    "yaheetech_us": 653,
    "costway_it": 11044,
    "vidaxl_uk": 81369,
    "vidaxl_pl": 177009,
    "homary_de": 1923,
    "homary_es": 1831,
    "overstock_us": 162828,
    "vonhaus_uk": 635,
    "homary_fr": 3039,
    "costway_de": 11123,
    "homary_uk": 1883,
    "vidaxl_nl": 177539,
}

_SITEMAP_TOTALS_PATH = os.environ.get(
    "SITEMAP_TOTALS_PATH", "/app/data/sitemap_totals.json")


def _load_sitemap_totals() -> dict[str, int]:
    """爬虫端写入的真实 sitemap URL 总数 —— 优先于 _FULL_ESTIMATES。"""
    import json
    try:
        with open(_SITEMAP_TOTALS_PATH, "r", encoding="utf-8") as f:
            return {k: int(v) for k, v in (json.load(f) or {}).items()}
    except Exception:
        return {}


def _load_hidden_sites() -> set[str]:
    """从 sites.yaml 读 settings.hidden_sites，dashboard 不展示这些站。"""
    import os
    import yaml
    yaml_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "sites.yaml",
    )
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return set(((cfg.get("settings") or {}).get("hidden_sites") or []))
    except Exception:
        return set()


# ── /api/coverage in-memory cache (30s TTL · invalidated on crawl success) ──
import time as _time
import threading as _threading
_COVERAGE_CACHE: dict = {}
_COVERAGE_CACHE_LOCK = _threading.Lock()
_COVERAGE_CACHE_TTL = 30  # seconds · 数据每 30s 才可能变 · UI 体感无差异


def _coverage_cache_get(key: str):
    with _COVERAGE_CACHE_LOCK:
        entry = _COVERAGE_CACHE.get(key)
        if not entry:
            return None
        if _time.time() - entry["ts"] > _COVERAGE_CACHE_TTL:
            _COVERAGE_CACHE.pop(key, None)
            return None
        return entry["data"]


def _coverage_cache_set(key: str, data) -> None:
    with _COVERAGE_CACHE_LOCK:
        _COVERAGE_CACHE[key] = {"ts": _time.time(), "data": data}


@router.get("/coverage")
def data_coverage(
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
    include_hidden: bool = Query(default=False, description="是否包含 hidden_sites（默认排除）"),
):
    """每站点数据覆盖率：fetched URL / sitemap 总 URL.

    优先用 fetched_urls 表（每次 fetch 都记录 · 含 4xx/5xx/parse_none）
    回退 Product.product_url（旧路径 · 只算成功落库的 unique SKU）。

    Perf: 30s in-memory cache · N+1 Product.count() 改成单 GROUP BY (chen-mj 反馈页面慢).
    """
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id, include_hidden=include_hidden)
    target_sku_counts = _workspace_site_targets(db, ws.id, include_hidden=include_hidden)
    cache_key = f"cov:{ws.id}:{include_hidden}"
    cached = _coverage_cache_get(cache_key)
    if cached is not None:
        return cached
    from sqlalchemy import text, func
    from ..models import Site as SiteModel
    hidden = _load_hidden_sites() if not include_hidden else set()
    sitemap_totals = _load_sitemap_totals()
    # 一次查全部 site 的 fetched_urls count（避免 N+1）
    try:
        fetched_counts = {
            row[0]: row[1]
            for row in db.execute(
                text("SELECT site, count(*) FROM fetched_urls GROUP BY site")
            ).all()
        }
    except Exception:
        fetched_counts = {}
        try:
            db.rollback()
        except Exception:
            pass
    # 一次查全部 site 的 Product count（避免 N+1 · 之前 55 次 SELECT 是 4-5s 的元凶）
    sku_counts = {
        row[0]: row[1]
        for row in db.query(Product.site, func.count(Product.id))
                     .group_by(Product.site).all()
    }
    listing_counts = {
        row[0]: int(row[1] or 0)
        for row in db.query(
            Product.site,
            func.count(func.distinct(func.coalesce(Product.spu, Product.sku))),
        ).group_by(Product.site).all()
    }
    discovered_product_counts = {
        row[0]: int(row[1] or 0)
        for row in db.query(CrawlUrl.site, func.count(func.distinct(CrawlUrl.url)))
                     .filter(CrawlUrl.kind == "product")
                     .filter(CrawlUrl.url.isnot(None))
                     .group_by(CrawlUrl.site).all()
    }
    rows = []
    for s in db.query(SiteModel).filter(SiteModel.site.in_(allowed_sites)).all():
        if s.site in hidden or s.site not in allowed_sites:
            continue
        # 真实 fetched URL count（包含 SKU dup 的）优先于 SKU-unique row count
        fetched = fetched_counts.get(s.site, 0)
        sku_count = sku_counts.get(s.site, 0)
        cur_raw = fetched if fetched >= sku_count else sku_count
        # 真实 sitemap 总数优先（爬虫每次跑都更新），缺失时回退人工估算
        configured_target = target_sku_counts.get(s.site)
        acceptance_target = _ACCEPTANCE_TARGETS.get(s.site)
        target_sku = configured_target or acceptance_target
        target_source = (
            "workspace" if configured_target else
            "acceptance" if acceptance_target else None
        )
        est = target_sku or sitemap_totals.get(s.site) or _FULL_ESTIMATES.get(s.site, 0)
        sku_deviation_pct = (
            round((sku_count - target_sku) / target_sku * 100, 2)
            if target_sku else None
        )
        sku_deviation_abs = (sku_count - target_sku) if target_sku else None
        # 覆盖率钳位:cur 不超 est (爬取期间 sitemap 新增 URL 会让 fetched > sitemap,
        # 但展示给客户的覆盖率必须 ≤ 100% · "超额"不算更覆盖 · 见 chen-mj 反馈)
        if est == 0:
            # 没有估算时,假定当前就是全量 (小站、不带 sitemap 的)
            est = cur_raw
            cur = cur_raw
            pct = 100.0 if cur > 0 else 0.0
        else:
            cur = min(cur_raw, est)  # ← 关键钳位
            pct = round(cur / est * 100, 2)
        # 健康度分级
        if cur == 0:
            status = "empty"
        elif pct < 5:
            status = "critical"
        elif pct < 50:
            status = "warning"
        else:
            status = "healthy"
        sitemap_product_count = int(sitemap_totals.get(s.site) or 0)
        discovered_product_count = int(discovered_product_counts.get(s.site) or 0)
        listing_product_count = int(listing_counts.get(s.site) or 0)
        actual_candidates = [
            ("sitemap", sitemap_product_count),
            ("discovered_url", discovered_product_count),
            ("product_listing", listing_product_count),
            ("sku_rows", int(sku_count or 0)),
        ]
        actual_source, actual_product_count = next(
            ((source, count) for source, count in actual_candidates if count > 0),
            ("none", 0),
        )
        rows.append({
            "site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform,
            "current": cur, "current_raw": cur_raw, "estimated_full": est,
            "actual_product_count": actual_product_count,
            "actual_product_count_source": actual_source,
            "sitemap_product_count": sitemap_product_count,
            "discovered_product_url_count": discovered_product_count,
            "product_listing_count": listing_product_count,
            "product_detail_count": int(sku_count or 0),
            "target_sku_count": target_sku,
            "target_sku_source": target_source,
            "sku_deviation_abs": sku_deviation_abs,
            "sku_deviation_pct": sku_deviation_pct,
            "coverage_pct": pct, "status": status,
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
        })
    rows.sort(key=lambda x: (x["status"] != "critical", x["coverage_pct"] or 0))

    # 汇总:current 已钳位 · 整体覆盖率必 ≤ 100%
    total_current = sum(r["current"] for r in rows)        # 钳位后
    total_current_raw = sum(r["current_raw"] for r in rows)  # 原始(可能超 100%)
    total_est = sum(r["estimated_full"] for r in rows)
    critical = sum(1 for r in rows if r["status"] == "critical")
    warning = sum(1 for r in rows if r["status"] == "warning")
    healthy = sum(1 for r in rows if r["status"] == "healthy")
    empty = sum(1 for r in rows if r["status"] == "empty")
    high_deviation = sum(
        1 for r in rows
        if r.get("sku_deviation_pct") is not None
        and abs(float(r["sku_deviation_pct"])) > 50
    )

    result = {
        "sites": rows,
        "summary": {
            "total_sites": len(rows),
            "total_current_sku": total_current,
            "total_current_sku_raw": total_current_raw,
            "total_estimated_full": total_est,
            "overall_coverage_pct": min(100.0, round(total_current / total_est * 100, 2))
                                   if total_est > 0 else 0,
            "critical_count": critical,
            "warning_count": warning,
            "healthy_count": healthy,
            "empty_count": empty,
            "high_deviation_count": high_deviation,
        },
    }
    _coverage_cache_set(cache_key, result)
    return result


def _empty_data_quality_payload() -> dict:
    return {"items": [], "summary": {
        "total_sites": 0, "healthy": 0, "needs_rerun": 0,
        "rerun_after_setup": 0,
        "rerun_precondition_total": 0,
        "rerun_preconditions": [],
        "no_products": 0, "never_crawled": 0,
        "weak_titles": 0, "high_deviation": 0,
        "missing_prices": 0, "missing_sales": 0,
        "missing_traffic": 0, "missing_conversion": 0,
        "missing_promotions": 0, "coverage_risk": 0,
        "partial_crawls": 0, "pdp_price_required": 0,
        "configured_price_sources": 0,
        "pending_jobs": 0, "running_jobs": 0, "stuck_jobs": 0,
        "failed_jobs": 0, "blocked_jobs": 0, "skipped_jobs": 0,
        "stale_pending_jobs": 0,
        "sites_without_jobs": 0,
        "sites_with_active_jobs": 0,
        "sites_with_failed_jobs": 0,
    }}


_NON_RERUN_FAILURE_CODES = {"market_paused", "empty_sitemap"}
_PROXY_FAILURE_CODES = {"proxy_unavailable", "proxy_auth_failed"}
_ANTI_BOT_FAILURE_CODES = {
    "http_401", "http_403", "http_429", "anti_bot_challenge",
}
_PDP_PRICE_REQUIRED_PLATFORMS = {"overstock", "cratebarrel", "bol"}
_EXTERNAL_DATA_ISSUES = {
    "traffic_missing", "conversion_missing", "pdp_price_required",
}
_RERUN_PRECONDITION_ISSUES = {
    "traffic_missing",
    "conversion_missing",
    "pdp_price_required",
    "proxy_unavailable",
    "proxy_auth_failed",
    "anti_bot_blocked",
    "sales_history_insufficient",
}
_RERUN_CANDIDATE_ISSUES = {
    "no_products",
    "coverage_low",
    "sku_deviation_high",
    "title_weak",
    "price_missing",
    "pdp_price_required",
    "currency_missing",
    "currency_mismatch",
    "sales_missing",
    "revenue_missing",
    "sales_history_insufficient",
    "promotions_missing",
    "latest_job_failed",
    "partial_crawl",
    "job_pending_stale",
    "never_crawled",
    "proxy_unavailable",
    "proxy_auth_failed",
    "anti_bot_blocked",
}
_NON_RERUN_ISSUES = set(_NON_RERUN_FAILURE_CODES)


def _compact_error(text: str | None, *, limit: int = 260) -> str | None:
    if not text:
        return None
    first = str(text).strip().splitlines()[0].strip()
    if len(first) <= limit:
        return first
    return first[:limit - 1] + "…"


def _job_failure_payload(job: CrawlJob | None,
                         failure: CrawlFailure | None = None) -> dict | None:
    if job is None and failure is None:
        return None
    if job is not None and job.status not in {"failed", "blocked", "partial"} and not (
        job.failure_code or job.failure_stage or job.failure_detail or job.error
    ):
        return None
    code = (job.failure_code if job else None) or (failure.code if failure else None)
    stage = (job.failure_stage if job else None) or (failure.stage if failure else None)
    detail = ((job.failure_detail or job.error) if job else None) or (
        failure.detail if failure else None)
    suggested = (job.suggested_action if job else None) or (
        failure.suggested_action if failure else None)
    retryable = (job.retryable if job else None)
    if retryable is None and failure is not None:
        retryable = failure.retryable
    return {
        "code": code,
        "stage": stage,
        "detail": _compact_error(detail),
        "retryable": retryable,
        "suggested_action": suggested,
        "job_id": job.id if job else failure.job_id,
        "status": job.status if job else None,
        "occurred_at": (
            job.finished_at.isoformat() if job and job.finished_at else
            failure.occurred_at.isoformat() if failure and failure.occurred_at else None
        ),
    }


def _configured_price_source(site: Site) -> dict:
    config = site.crawler_config or {}
    source_type = str(config.get("price_source_type") or "").strip().lower()
    feed_url = next((
        str(config.get(key) or "").strip()
        for key in ("price_feed_url", "feed_url", "price_feed")
        if str(config.get(key) or "").strip()
    ), "")
    api_url = next((
        str(config.get(key) or "").strip()
        for key in ("pdp_price_api_url", "price_api_url")
        if str(config.get(key) or "").strip()
    ), "")
    selector = next((
        str(config.get(key) or "").strip()
        for key in ("pdp_price_selector", "price_selector")
        if str(config.get(key) or "").strip()
    ), "")
    if not source_type:
        source_type = "feed" if feed_url else "api" if api_url else "pdp" if selector else ""
    configured = bool(source_type != "external" and (feed_url or api_url or selector))
    return {
        "configured": configured,
        "type": source_type or None,
        "source": feed_url or api_url or ("pdp_selector" if selector else None),
    }


def _data_quality_suggestion(*, status: str, issues: list[str],
                             failure: dict | None,
                             price_source_configured: bool = False) -> str:
    code = (failure or {}).get("code")
    if "job_pending_stale" in issues:
        return "任务已排队超过久排阈值；检查 worker/队列容量/代理可用性，必要时取消旧任务后重排"
    if "job_in_progress" in issues:
        return "已有抓取任务处理中；打开队列明细查看进度，不要重复入队"
    if code in _NON_RERUN_FAILURE_CODES:
        return "目标站当前无可采商品或市场暂停；重跑不能解决，需等待业务恢复或改用官方/API 数据源"
    if code in _PROXY_FAILURE_CODES:
        return "当前代理不可用；先在代理池补充/修复可用住宅代理，再重跑该站点"
    if code in _ANTI_BOT_FAILURE_CODES:
        return "目标站反爬/封禁；切换可用住宅代理、浏览器策略或外部数据源后再重跑"
    if "partial_crawl" in issues and failure and failure.get("suggested_action"):
        return f"最近采集部分成功但存在失败信号；{failure['suggested_action']}"
    if "partial_crawl" in issues:
        return "最近采集部分成功但存在失败信号；检查失败码/阶段后重跑或调整代理/解析策略"
    if "pdp_price_required" in issues:
        return "该站 sitemap 可枚举商品，但价格在 PDP/官方接口内；需接入可访问 PDP 的住宅代理、站点 API 或外部价格源后再补抓"
    if "price_missing" in issues and price_source_configured:
        return "已配置价格源；重跑该站点验证 feed/API/PDP 补价结果，失败时到队列详情查看 configured_price_source_failed"
    if (
        ("traffic_missing" in issues or "conversion_missing" in issues)
        and ("sku_deviation_high" in issues or "coverage_low" in issues)
    ):
        return "该站同时存在商品覆盖偏差和第三方流量/转化缺口；抓取重跑只能改善商品覆盖，流量/转化需先接入外部数据源"
    if "sku_deviation_high" in issues:
        return "SKU 数与验收目标偏差超过50%；优先检查分类覆盖、分页/去重策略和站点目标口径后重跑"
    if "title_weak" in issues and "price_missing" in issues:
        return "检查标题/价格解析与 PDP enrich；修复 selector 或配置可用住宅代理后重跑"
    if "title_weak" in issues:
        return "检查列表/PDP 标题解析；弱标题站点需要补抓详情页或修复 selector 后重跑"
    if "currency_mismatch" in issues or "currency_missing" in issues:
        return "检查站点币种映射和历史商品 currency 字段；先做币种回填/修正后再导出报表"
    if "price_missing" in issues:
        return "检查价格解析/PDP enrich；必要时配置可用住宅代理后重跑"
    if "sales_history_insufficient" in issues:
        return "已有评论信号但历史快照不足；保持定时抓取，至少形成两次评论快照后重算销量/收入"
    if "traffic_missing" in issues or "conversion_missing" in issues:
        return "缺少第三方流量/转化率数据源；接入 SimilarWeb/GA/BI 后刷新报表，单纯重跑抓取不能生成"
    if "latest_job_failed" in issues and failure and failure.get("suggested_action"):
        return f"已有数据但最近任务失败；{failure['suggested_action']}"
    if failure and failure.get("suggested_action"):
        return str(failure["suggested_action"])
    if status == "critical":
        return "重跑抓取并查看失败明细"
    if "latest_job_failed" in issues:
        return "已有数据但最近任务失败；查看队列失败明细后决定是否重跑"
    if status == "warning":
        return "补跑促销/销量估算或等待任务完成"
    return "数据质量正常"


def _build_data_quality_payload(
    db: Session,
    sites: list[Site],
    target_sku_by_site: dict[str, int] | None = None,
) -> dict:
    site_codes = [s.site for s in sites]
    if not site_codes:
        return _empty_data_quality_payload()
    target_sku_by_site = target_sku_by_site or {}

    sitemap_totals = _load_sitemap_totals()
    try:
        fetched_counts = {
            row[0]: int(row[1] or 0)
            for row in db.execute(
                text("SELECT site, count(*) FROM fetched_urls GROUP BY site")
            ).all()
        }
    except Exception:
        fetched_counts = {}
        try:
            db.rollback()
        except Exception:
            pass

    weak_title_expr = or_(
        func.length(func.trim(func.coalesce(Product.title, ""))) == 0,
        func.length(func.trim(func.coalesce(Product.title, ""))) < 4,
        func.lower(func.trim(func.coalesce(Product.title, ""))).in_(
            ("product", "item", "sku", "untitled", "detail", "details",
             "view product", "shop now")
        ),
        func.lower(func.trim(func.coalesce(Product.title, ""))) ==
        func.lower(func.trim(func.coalesce(Product.sku, ""))),
    )
    product_rows = {
        row[0]: {
            "sku_count": int(row[1] or 0),
            "spu_count": int(row[2] or 0),
            "price_signal_count": int(row[3] or 0),
            "sales_signal_count": int(row[4] or 0),
            "revenue_signal_count": int(row[5] or 0),
            "review_signal_count": int(row[6] or 0),
            "weak_title_count": int(row[7] or 0),
            "last_product_updated": row[8],
        }
        for row in db.query(
            Product.site,
            func.count(Product.id),
            func.count(func.distinct(func.coalesce(Product.spu, Product.sku))),
            func.count(Product.id).filter(
                func.coalesce(Product.sale_price, Product.original_price, 0) > 0
            ),
            func.count(Product.id).filter(func.coalesce(Product.thirty_day_sales, 0) > 0),
            func.count(Product.id).filter(func.coalesce(Product.thirty_day_revenue, 0) > 0),
            func.count(Product.id).filter(func.coalesce(Product.review_count, 0) > 0),
            func.count(Product.id).filter(weak_title_expr),
            func.max(Product.updated_time),
        ).filter(Product.site.in_(site_codes)).group_by(Product.site).all()
    }
    review_history_skus: dict[str, int] = {site: 0 for site in site_codes}
    for site, _sku, days in (
            db.query(PriceHistory.site, PriceHistory.sku,
                     func.count(func.distinct(PriceHistory.date)))
            .filter(PriceHistory.site.in_(site_codes),
                    PriceHistory.review_count.isnot(None))
            .group_by(PriceHistory.site, PriceHistory.sku)
            .all()):
        if int(days or 0) >= 2:
            review_history_skus[site] = int(review_history_skus.get(site, 0)) + 1
    expected_currency_by_site = {
        site: _currency_for_site(site) for site in site_codes
    }
    currency_quality: dict[str, dict[str, int]] = {
        site: {"missing": 0, "mismatch": 0} for site in site_codes
    }
    for site, currency, count in (
            db.query(Product.site, Product.currency, func.count(Product.id))
            .filter(Product.site.in_(site_codes))
            .group_by(Product.site, Product.currency)
            .all()):
        expected_currency = expected_currency_by_site.get(site)
        if not expected_currency:
            continue
        value = str(currency or "").strip().upper()
        n = int(count or 0)
        if not value:
            currency_quality.setdefault(site, {"missing": 0, "mismatch": 0})["missing"] += n
        elif value != expected_currency:
            currency_quality.setdefault(site, {"missing": 0, "mismatch": 0})["mismatch"] += n
    promotion_counts = {
        row[0]: int(row[1] or 0)
        for row in db.query(Promotion.site, func.count(Promotion.id))
                     .filter(Promotion.site.in_(site_codes))
                     .group_by(Promotion.site).all()
    }
    trend_signal_rows = {
        row[0]: {
            "traffic_signal_count": int(row[1] or 0),
            "conversion_signal_count": int(row[2] or 0),
        }
        for row in db.query(
            Trend.site,
            func.count(Trend.id).filter(Trend.traffic.isnot(None)),
            func.count(Trend.id).filter(Trend.conversion_rate.isnot(None)),
        ).filter(Trend.site.in_(site_codes)).group_by(Trend.site).all()
    }
    latest_jobs: dict[str, CrawlJob] = {}
    for job in (db.query(CrawlJob)
                .filter(CrawlJob.site.in_(site_codes))
                .order_by(CrawlJob.site, CrawlJob.id.desc())
                .all()):
        latest_jobs.setdefault(job.site, job)
    latest_failures: dict[str, CrawlFailure] = {}
    for failure in (db.query(CrawlFailure)
                    .filter(CrawlFailure.site.in_(site_codes))
                    .order_by(CrawlFailure.site, CrawlFailure.id.desc())
                    .all()):
        latest_failures.setdefault(failure.site, failure)
    failure_counts = {
        row[0]: int(row[1] or 0)
        for row in db.query(CrawlFailure.site, func.count(CrawlFailure.id))
                     .filter(CrawlFailure.site.in_(site_codes))
                     .group_by(CrawlFailure.site).all()
    }
    queue_by_site: dict[str, dict] = {
        site: {
            "pending": 0,
            "running": 0,
            "stuck": 0,
            "success": 0,
            "failed": 0,
            "blocked": 0,
            "skipped": 0,
            "partial": 0,
            "total": 0,
            "active_count": 0,
            "stale_pending": 0,
            "oldest_active_at": None,
            "latest_active_at": None,
        }
        for site in site_codes
    }
    for site, status, count in (
            db.query(CrawlJob.site, CrawlJob.status, func.count(CrawlJob.id))
            .filter(CrawlJob.site.in_(site_codes))
            .group_by(CrawlJob.site, CrawlJob.status)
            .all()):
        bucket = queue_by_site.setdefault(site, {"total": 0})
        key = (status or "unknown").lower()
        bucket[key] = int(bucket.get(key, 0)) + int(count or 0)
        bucket["total"] = int(bucket.get("total", 0)) + int(count or 0)

    now = datetime.utcnow()
    stuck_cutoff = now - timedelta(seconds=CRAWL_JOB_STUCK_SEC)
    for site, count in (
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.site.in_(site_codes),
                    CrawlJob.status == "running",
                    CrawlJob.started_at.isnot(None),
                    CrawlJob.started_at < stuck_cutoff,
                    or_(CrawlJob.heartbeat_at.is_(None),
                        CrawlJob.heartbeat_at < stuck_cutoff))
            .group_by(CrawlJob.site)
            .all()):
        bucket = queue_by_site.setdefault(site, {"total": 0})
        stuck_count = int(count or 0)
        bucket["stuck"] = stuck_count
        bucket["running"] = max(0, int(bucket.get("running", 0)) - stuck_count)

    for site, count in (
            db.query(CrawlJob.site, func.count(CrawlJob.id))
            .filter(CrawlJob.site.in_(site_codes),
                    CrawlJob.status == "pending",
                    CrawlJob.created_at.isnot(None),
                    CrawlJob.created_at < stuck_cutoff)
            .group_by(CrawlJob.site)
            .all()):
        bucket = queue_by_site.setdefault(site, {"total": 0})
        bucket["stale_pending"] = int(count or 0)

    for site, active_count, oldest_active_at, latest_active_at in (
            db.query(CrawlJob.site,
                     func.count(CrawlJob.id),
                     func.min(func.coalesce(CrawlJob.started_at, CrawlJob.created_at)),
                     func.max(func.coalesce(CrawlJob.started_at, CrawlJob.created_at)))
            .filter(CrawlJob.site.in_(site_codes),
                    CrawlJob.status.in_(("pending", "running")))
            .group_by(CrawlJob.site)
            .all()):
        bucket = queue_by_site.setdefault(site, {"total": 0})
        bucket["active_count"] = int(active_count or 0)
        bucket["oldest_active_at"] = oldest_active_at
        bucket["latest_active_at"] = latest_active_at

    items = []
    for s in sites:
        product = product_rows.get(s.site, {})
        sku_count = int(product.get("sku_count") or 0)
        spu_count = int(product.get("spu_count") or 0)
        fetched = max(fetched_counts.get(s.site, 0), sku_count)
        configured_target_sku = int(target_sku_by_site.get(s.site) or 0)
        acceptance_target_sku = int(_ACCEPTANCE_TARGETS.get(s.site) or 0)
        target_sku_count = configured_target_sku or acceptance_target_sku
        target_sku_source = (
            "workspace" if configured_target_sku else
            "acceptance" if acceptance_target_sku else None
        )
        estimated = target_sku_count or sitemap_totals.get(s.site) or _FULL_ESTIMATES.get(s.site, 0) or fetched
        coverage_pct = round(min(fetched, estimated) / estimated * 100, 2) if estimated else 0
        sku_deviation_pct = (
            round((sku_count - target_sku_count) / target_sku_count * 100, 2)
            if target_sku_count else None
        )
        sku_deviation_abs = (sku_count - target_sku_count) if target_sku_count else None
        price_signal_count = int(product.get("price_signal_count") or 0)
        sales_signal_count = int(product.get("sales_signal_count") or 0)
        revenue_signal_count = int(product.get("revenue_signal_count") or 0)
        review_signal_count = int(product.get("review_signal_count") or 0)
        review_history_signal_count = int(review_history_skus.get(s.site, 0) or 0)
        weak_title_count = int(product.get("weak_title_count") or 0)
        currency_counts = currency_quality.get(s.site, {})
        currency_missing_count = int(currency_counts.get("missing") or 0)
        currency_mismatch_count = int(currency_counts.get("mismatch") or 0)
        promotion_count = promotion_counts.get(s.site, 0)
        trend_signal = trend_signal_rows.get(s.site, {})
        traffic_signal_count = int(trend_signal.get("traffic_signal_count") or 0)
        conversion_signal_count = int(trend_signal.get("conversion_signal_count") or 0)
        latest_job = latest_jobs.get(s.site)
        latest_failure = _job_failure_payload(
            latest_job, latest_failures.get(s.site))
        latest_failure_code = (latest_failure or {}).get("code")
        queue_counts = queue_by_site.get(s.site, {})
        price_source = _configured_price_source(s)
        issues: list[str] = []
        if sku_count == 0:
            issues.append("no_products")
        if estimated and coverage_pct < 50:
            issues.append("coverage_low")
        if sku_deviation_pct is not None and abs(sku_deviation_pct) > 50:
            issues.append("sku_deviation_high")
        if sku_count > 0 and price_signal_count == 0:
            issues.append("price_missing")
            if (
                (s.platform or "").lower() in _PDP_PRICE_REQUIRED_PLATFORMS
                and not price_source["configured"]
            ):
                issues.append("pdp_price_required")
        if sku_count > 0 and sales_signal_count == 0:
            issues.append("sales_missing")
            if review_signal_count > 0 and review_history_signal_count == 0:
                issues.append("sales_history_insufficient")
        if sku_count > 0 and revenue_signal_count == 0:
            issues.append("revenue_missing")
        if sku_count > 0 and traffic_signal_count == 0:
            issues.append("traffic_missing")
        if sku_count > 0 and conversion_signal_count == 0:
            issues.append("conversion_missing")
        if sku_count > 0 and weak_title_count > 0:
            issues.append("title_weak")
        if sku_count > 0 and currency_missing_count > 0:
            issues.append("currency_missing")
        if sku_count > 0 and currency_mismatch_count > 0:
            issues.append("currency_mismatch")
        if promotion_count == 0:
            issues.append("promotions_missing")
        if latest_job and latest_job.status in {"failed", "blocked"}:
            issues.append("latest_job_failed")
        if latest_job and (
            latest_job.status == "partial" or
            (latest_job.status == "success" and latest_job.failure_code)
        ):
            issues.append("partial_crawl")
        if latest_job and latest_job.status in {"pending", "running"}:
            issues.append("job_in_progress")
        if latest_failure_code in _NON_RERUN_FAILURE_CODES:
            issues.append(str(latest_failure_code))
        elif latest_failure_code == "proxy_auth_failed":
            issues.append("proxy_auth_failed")
        elif latest_failure_code == "proxy_unavailable":
            issues.append("proxy_unavailable")
        elif latest_failure_code in _ANTI_BOT_FAILURE_CODES:
            issues.append("anti_bot_blocked")
        if int(queue_counts.get("stale_pending", 0) or 0) > 0:
            issues.append("job_pending_stale")
        if not latest_job and sku_count == 0:
            issues.append("never_crawled")
        status = "healthy"
        if "no_products" in issues or "coverage_low" in issues:
            status = "critical"
        elif issues:
            status = "warning"
        last_product_updated = product.get("last_product_updated")
        if latest_failure_code in _NON_RERUN_FAILURE_CODES and status == "critical":
            status = "warning"
        external_data_required = any(issue in _EXTERNAL_DATA_ISSUES
                                     for issue in issues)
        rerun_candidate = any(issue in _RERUN_CANDIDATE_ISSUES
                              for issue in issues)
        non_rerun_failure = any(issue in _NON_RERUN_ISSUES
                                for issue in issues)
        rerun_preconditions = [
            issue for issue in issues if issue in _RERUN_PRECONDITION_ISSUES
        ]
        rerun_ready = (
            rerun_candidate
            and not non_rerun_failure
            and not rerun_preconditions
        )
        rerun_after_setup = (
            rerun_candidate
            and not non_rerun_failure
            and bool(rerun_preconditions)
        )
        rerun_recommended = rerun_ready
        rerun_blocked = non_rerun_failure or bool(rerun_preconditions) or (
            external_data_required and not rerun_candidate
        )
        suggested_action = _data_quality_suggestion(
            status=status, issues=issues, failure=latest_failure,
            price_source_configured=bool(price_source["configured"]))
        severity = {"critical": "critical", "warning": "warning",
                    "healthy": "ok"}.get(status, status)
        last_error = (latest_failure or {}).get("detail")
        items.append({
            "site": s.site,
            "brand": s.brand,
            "country": s.country,
            "url": s.url,
            "platform": s.platform,
            "sku_count": sku_count,
            "spu_count": spu_count,
            "fetched_count": fetched,
            "estimated_full": estimated,
            "target_sku_count": target_sku_count or None,
            "target_sku_source": target_sku_source,
            "sku_deviation_abs": sku_deviation_abs,
            "sku_deviation_pct": sku_deviation_pct,
            "coverage_pct": coverage_pct,
            "promotion_count": promotion_count,
            "price_signal_count": price_signal_count,
            "price_source_configured": bool(price_source["configured"]),
            "price_source_type": price_source["type"],
            "price_source": price_source["source"],
            "sales_signal_count": sales_signal_count,
            "revenue_signal_count": revenue_signal_count,
            "review_signal_count": review_signal_count,
            "review_history_signal_count": review_history_signal_count,
            "weak_title_count": weak_title_count,
            "expected_currency": expected_currency_by_site.get(s.site),
            "currency_missing_count": currency_missing_count,
            "currency_mismatch_count": currency_mismatch_count,
            "traffic_signal_count": traffic_signal_count,
            "conversion_signal_count": conversion_signal_count,
            "title_quality_pct": round((sku_count - weak_title_count) / sku_count * 100, 2) if sku_count else 0,
            "price_signal_pct": round(price_signal_count / sku_count * 100, 2) if sku_count else 0,
            "sales_signal_pct": round(sales_signal_count / sku_count * 100, 2) if sku_count else 0,
            "revenue_signal_pct": round(revenue_signal_count / sku_count * 100, 2) if sku_count else 0,
            "failure_count": failure_counts.get(s.site, 0),
            "crawl_queue": {
                "pending": int(queue_counts.get("pending", 0) or 0),
                "running": int(queue_counts.get("running", 0) or 0),
                "stuck": int(queue_counts.get("stuck", 0) or 0),
                "success": int(queue_counts.get("success", 0) or 0),
                "failed": int(queue_counts.get("failed", 0) or 0),
                "blocked": int(queue_counts.get("blocked", 0) or 0),
                "skipped": int(queue_counts.get("skipped", 0) or 0),
                "partial": int(queue_counts.get("partial", 0) or 0),
                "total": int(queue_counts.get("total", 0) or 0),
                "active_count": int(queue_counts.get("active_count", 0) or 0),
                "stale_pending": int(queue_counts.get("stale_pending", 0) or 0),
                "oldest_active_at": (
                    queue_counts.get("oldest_active_at").isoformat()
                    if queue_counts.get("oldest_active_at") else None
                ),
                "latest_active_at": (
                    queue_counts.get("latest_active_at").isoformat()
                    if queue_counts.get("latest_active_at") else None
                ),
            },
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
            "last_product_updated": last_product_updated.isoformat() if last_product_updated else None,
            "latest_job": {
                "id": latest_job.id,
                "status": latest_job.status,
                "trigger": latest_job.trigger,
                "created_at": latest_job.created_at.isoformat() if latest_job.created_at else None,
                "finished_at": latest_job.finished_at.isoformat() if latest_job.finished_at else None,
                "products_count": latest_job.products_count or 0,
                "promotion_count": latest_job.promotion_count or 0,
                "failure_code": latest_job.failure_code,
                "failure_stage": latest_job.failure_stage,
                "failure_detail": _compact_error(
                    latest_job.failure_detail or latest_job.error),
                "retryable": latest_job.retryable,
                "suggested_action": latest_job.suggested_action,
            } if latest_job else None,
            "latest_failure": latest_failure,
            "last_error": last_error,
            "last_error_code": latest_failure_code,
            "issues": issues,
            "status": status,
            "severity": severity,
            "rerun_recommended": rerun_recommended,
            "rerun_ready": rerun_ready,
            "rerun_after_setup": rerun_after_setup,
            "rerun_blocked": rerun_blocked,
            "rerun_preconditions": rerun_preconditions,
            "external_data_required": external_data_required,
            "suggested_action": suggested_action,
            "suggestion": suggested_action,
        })

    items.sort(key=lambda r: (
        {"critical": 0, "warning": 1, "healthy": 2}.get(r["status"], 3),
        r["coverage_pct"],
        r["site"],
    ))
    summary = {
        "total_sites": len(items),
        "healthy": sum(1 for r in items if r["status"] == "healthy"),
        "needs_rerun": sum(1 for r in items if r.get("rerun_ready")),
        "rerun_after_setup": sum(1 for r in items if r.get("rerun_after_setup")),
        "rerun_blocked": sum(1 for r in items if r.get("rerun_blocked")),
        "external_data_required": sum(
            1 for r in items if r.get("external_data_required")),
        "no_products": sum(1 for r in items if "no_products" in r["issues"]),
        "never_crawled": sum(1 for r in items if "never_crawled" in r["issues"]),
        "weak_titles": sum(1 for r in items if "title_weak" in r["issues"]),
        "currency_missing": sum(1 for r in items if "currency_missing" in r["issues"]),
        "currency_mismatch": sum(1 for r in items if "currency_mismatch" in r["issues"]),
        "currency_issues": sum(1 for r in items if (
            "currency_missing" in r["issues"] or "currency_mismatch" in r["issues"]
        )),
        "high_deviation": sum(1 for r in items if "sku_deviation_high" in r["issues"]),
        "missing_prices": sum(1 for r in items if "price_missing" in r["issues"]),
        "missing_sales": sum(1 for r in items if "sales_missing" in r["issues"]),
        "insufficient_sales_history": sum(
            1 for r in items if "sales_history_insufficient" in r["issues"]),
        "missing_traffic": sum(1 for r in items if "traffic_missing" in r["issues"]),
        "missing_conversion": sum(1 for r in items if "conversion_missing" in r["issues"]),
        "missing_promotions": sum(1 for r in items if "promotions_missing" in r["issues"]),
        "partial_crawls": sum(1 for r in items if "partial_crawl" in r["issues"]),
        "pdp_price_required": sum(1 for r in items if "pdp_price_required" in r["issues"]),
        "configured_price_sources": sum(
            1 for r in items if r.get("price_source_configured")),
        "coverage_risk": sum(1 for r in items if "coverage_low" in r["issues"]),
        "pending_jobs": sum(r["crawl_queue"]["pending"] for r in items),
        "running_jobs": sum(r["crawl_queue"]["running"] for r in items),
        "stuck_jobs": sum(r["crawl_queue"]["stuck"] for r in items),
        "failed_jobs": sum(r["crawl_queue"]["failed"] for r in items),
        "blocked_jobs": sum(r["crawl_queue"]["blocked"] for r in items),
        "skipped_jobs": sum(r["crawl_queue"]["skipped"] for r in items),
        "stale_pending_jobs": sum(r["crawl_queue"]["stale_pending"] for r in items),
        "sites_without_jobs": sum(1 for r in items if r["crawl_queue"]["total"] == 0),
        "sites_with_active_jobs": sum(1 for r in items if r["crawl_queue"]["active_count"] > 0),
        "sites_with_failed_jobs": sum(1 for r in items if (
            r["crawl_queue"]["failed"] > 0 or r["crawl_queue"]["blocked"] > 0
        )),
    }
    precondition_rows = []
    for issue in sorted(_RERUN_PRECONDITION_ISSUES):
        sites_for_issue = [
            r["site"] for r in items
            if issue in set(r.get("rerun_preconditions") or [])
        ]
        if sites_for_issue:
            precondition_rows.append({
                "issue": issue,
                "count": len(sites_for_issue),
                "sites": sites_for_issue[:20],
            })
    summary["rerun_preconditions"] = precondition_rows
    summary["rerun_precondition_total"] = len({
        r["site"] for r in items if r.get("rerun_preconditions")
    })
    return {"items": items, "summary": summary}


@router.get("/data-quality")
def data_quality(
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
    include_hidden: bool = Query(default=False, description="是否包含 hidden_sites（默认排除）"),
):
    """站点数据质量明细：把验收关注的 SKU/促销/销量收入/任务失败集中展示。"""
    ws = _current_workspace(user, db, x_workspace_id)
    allowed_sites = _workspace_site_names(db, ws.id, include_hidden=include_hidden)
    target_sku_counts = _workspace_site_targets(db, ws.id, include_hidden=include_hidden)
    hidden = _load_hidden_sites() if not include_hidden else set()
    sites = [
        s for s in db.query(Site).filter(Site.site.in_(allowed_sites)).all()
        if s.site not in hidden
    ]
    return _build_data_quality_payload(db, sites, target_sku_counts)


# ---------- 按 record 计费 · 用量查询 ----------
# Schema 就绪 · 中间件层 metering 留给下个迭代（避免影响线上稳定性）
@router.get("/billing/usage")
def billing_usage(days: int = 30, user: str = Depends(require_user),
                  x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                  db: Session = Depends(get_db)):
    """当前用户所有 API key 的 N 天用量 + 账单。

    用于：
    · 海尔大数据湖项目 · 资源池按订单付费对接
    · 用户自助查询：调用量 / 字节数 / 账单 / 按 endpoint 分组
    """
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    from ..billing import get_usage_summary
    q = db.query(ApiKey)
    if _is_super_admin(u):
        q = q.filter(ApiKey.workspace_id == ws.id)
    else:
        q = q.filter(ApiKey.owner_user_id == u.id,
                     ApiKey.workspace_id == ws.id)
    keys = q.all()
    return {
        "days": days,
        "keys": [{
            "id": k.id,
            "name": k.name,
            "key_prefix": (k.key_prefix or "") + "…",
            **get_usage_summary(k.id, days),
        } for k in keys],
    }


@router.get("/billing/usage/{api_key_id}")
def billing_usage_detail(api_key_id: int, days: int = 30,
                         user: str = Depends(require_user),
                         x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                         db: Session = Depends(get_db)):
    """指定 API key 的 N 天用量明细。"""
    u = _require_dashboard_user(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    k = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
    if not k:
        raise HTTPException(404, "API key 不存在")
    if k.workspace_id != ws.id and not _is_super_admin(u):
        raise HTTPException(404, "API key 不存在")
    if not _is_admin_user(u) and (k.owner_user_id != u.id or k.workspace_id != ws.id):
        raise HTTPException(404, "API key 不存在")
    from ..billing import get_usage_summary
    return {
        "id": k.id,
        "name": k.name,
        "key_prefix": (k.key_prefix or "") + "…",
        **get_usage_summary(api_key_id, days),
    }


# ---------- Reports：HTML 报表见 /report?site=X · 数据走 /api/sites/{site}/overview ----------
# （废弃 PDF 链路：删了 /api/reports/list + /api/reports/generate + app/reports.py）


# ---------- Influencers · 替代 Apify 红人采集 actor（IG/TikTok/YT/X）----------
@router.get("/influencers/profile")
def influencer_profile(platform: str, username: str):
    from ..influencers import PLATFORMS, fetch_profile
    if platform not in PLATFORMS:
        raise HTTPException(400, f"未知平台 {platform}，支持: {','.join(PLATFORMS)}")
    try:
        return fetch_profile(platform, username).to_dict()
    except Exception as e:
        raise HTTPException(502, f"采集失败 {type(e).__name__}: {e}")


@router.get("/influencers/posts")
def influencer_posts(platform: str, username: str, limit: int = 20):
    from ..influencers import PLATFORMS, fetch_posts
    if platform not in PLATFORMS:
        raise HTTPException(400, f"未知平台 {platform}")
    try:
        return [p.to_dict() for p in fetch_posts(platform, username, limit=limit)]
    except Exception as e:
        raise HTTPException(502, f"采集失败 {type(e).__name__}: {e}")


@router.get("/influencers/full")
def influencer_full(platform: str, username: str, posts_limit: int = 12):
    """画像 + 近期帖子，一次返回."""
    from ..influencers import PLATFORMS, fetch_profile, fetch_posts
    if platform not in PLATFORMS:
        raise HTTPException(400, f"未知平台 {platform}")
    try:
        profile = fetch_profile(platform, username).to_dict()
        try:
            posts = [p.to_dict() for p in fetch_posts(
                platform, username, limit=posts_limit)]
        except Exception as e:
            posts = []
            profile["posts_error"] = f"{type(e).__name__}: {e}"
        return {"profile": profile, "posts": posts}
    except Exception as e:
        raise HTTPException(502, f"采集失败 {type(e).__name__}: {e}")
