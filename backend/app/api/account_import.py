"""Legacy-compatible Amazon account import endpoints."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from datetime import datetime
from io import BytesIO
from typing import Dict, List
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from ..auth import hash_secret, parse_token
from ..config import FRONTEND_DIR
from ..db import SessionLocal
from ..models import User, UserSession
from .routes import _find_user_by_identifier, _is_admin_user, _require_admin as _require_dashboard_admin
from app.crawlers.amazon_crawler.shuler.services.amazon.account_add import (
    ACCOUNT_OPTIONAL_COLUMNS,
    ACCOUNT_REQUIRED_COLUMNS,
    build_account_record,
    normalize_account_username,
    parse_account_excel,
    proxy_to_url,
    resolve_account_proxy,
)
from app.crawlers.amazon_crawler.shuler.util.config import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_SECRET,
    ADMIN_SESSION_TTL_SECONDS,
    ADMIN_USERNAME,
)
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


router = APIRouter(tags=["amazon-account-import"])

ADMIN_SESSION_COOKIE = "crawler_admin_session"
ACCOUNT_IMPORT_HTML = FRONTEND_DIR / "account_import.html"
ADMIN_LOGIN_HTML = FRONTEND_DIR / "admin_login.html"


def _sign_admin_token(username: str, expire_ts: int) -> str:
    payload = f"{username}|{expire_ts}"
    sign = hmac.new(
        ADMIN_SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    token = f"{payload}|{sign}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("utf-8")


def _verify_admin_token(token: str) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, expire_ts_str, sign = raw.split("|", 2)
        payload = f"{username}|{expire_ts_str}"
        expected = hmac.new(
            ADMIN_SESSION_SECRET.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sign):
            return None
        if int(expire_ts_str) < int(time.time()):
            return None
        return username
    except Exception:
        return None


def _require_import_admin(request: Request, authorization: str = "") -> str:
    cookie_user = _verify_admin_token(request.cookies.get(ADMIN_SESSION_COOKIE, ""))
    if cookie_user:
        return cookie_user

    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    token_info = parse_token(token)
    if not token_info:
        raise HTTPException(status_code=401, detail="请先登录")
    with SessionLocal() as db:
        user = _find_user_by_identifier(db, token_info.username)
        if token_info.session_id and user:
            session = (db.query(UserSession)
                       .filter(UserSession.session_hash == hash_secret(token_info.session_id),
                               UserSession.user_id == user.id)
                       .first())
            if (not session or session.revoked_at is not None or
                    (session.expires_at and session.expires_at < datetime.utcnow())):
                raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
        if user is None and token_info.username == "admin":
            user = User(username="admin", role="admin", status="active", global_role="super_admin")
        _require_dashboard_admin(token_info.username, db)
        if not _is_admin_user(user):
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return token_info.username


def _parse_static_ips_text(text: str) -> List[Dict]:
    parsed_items: List[Dict] = []
    invalid_lines: List[str] = []
    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        candidate = None
        if "://" in raw:
            parsed = urlparse(raw)
            if parsed.hostname and parsed.port and parsed.username and parsed.password:
                candidate = {
                    "host": parsed.hostname,
                    "port": int(parsed.port),
                    "user": parsed.username,
                    "password": parsed.password,
                }
        else:
            parts = [p.strip() for p in raw.split(":")]
            if len(parts) == 4:
                host, port_str, user, password = parts
                try:
                    port = int(port_str)
                    if host and port > 0 and user and password:
                        candidate = {"host": host, "port": port, "user": user, "password": password}
                except Exception:
                    candidate = None
        if candidate:
            parsed_items.append(candidate)
        else:
            invalid_lines.append(raw)
    if invalid_lines:
        sample = " | ".join(invalid_lines[:3])
        raise HTTPException(
            status_code=422,
            detail="static_ips 存在无效行，格式应为 hostname:port:username:password "
                   f"或 http://user:pass@host:port，示例错误: {sample}",
        )
    return parsed_items


def _account_import_proxy_strategy(file_proxy_count: int, use_static: bool) -> str:
    if file_proxy_count and use_static:
        return "mixed"
    if file_proxy_count:
        return "file_proxy"
    if use_static:
        return "static"
    return "dynamic"


def _format_account_import_job_response(job: Dict) -> Dict:
    status = int(job.get("status") or 0)
    return {
        "message": "account import queued" if status in (0, 1) else "account import finished",
        "async": True,
        "job_id": job.get("job_id"),
        "status": job.get("status_desc") or {0: "pending", 1: "running", 2: "done", 3: "failed"}.get(status, "unknown"),
        "status_code": status,
        "node_id": job.get("node_id") or "",
        "account_type": job.get("account_type") or "",
        "target_country": job.get("target_country") or "",
        "proxy_strategy": job.get("proxy_strategy") or "",
        "static_ip_count": int(job.get("static_ip_count") or 0),
        "provided_static_ip_count": len(job.get("static_ip_pool") or []),
        "source_rows": int(job.get("source_rows") or 0),
        "queued_rows": int(job.get("queued_rows") or 0),
        "attempted_rows": int(job.get("attempted_rows") or 0),
        "created_count": int(job.get("success_count") or 0),
        "failed_count": int(job.get("failed_count") or 0),
        "existing_browser_count": int(job.get("existing_browser_count") or 0),
        "file_proxy_count": int(job.get("file_proxy_count") or 0),
        "created_usernames": job.get("created_usernames") or [],
        "failed_items": job.get("failed_items") or [],
        "error": job.get("error_msg") or "",
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _build_existing_browser_account(account: Dict, country: str, static_ip_count: int,
                                    static_ip_pool: List, suffix: int) -> Dict:
    account = dict(account)
    account["username"] = normalize_account_username(account.get("username"), country)
    browser_id = str(account.get("browser_id") or "").strip()
    proxy_item, static_ip = resolve_account_proxy(
        account,
        suffix,
        static_ip_count=static_ip_count,
        static_ip_pool=static_ip_pool,
    )
    proxy_url = proxy_to_url(proxy_item)
    proxy_config = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    return build_account_record(account, browser_id, proxy_config, static_ip)


@router.get("/account-import", include_in_schema=False)
def account_import_page(request: Request):
    if not _verify_admin_token(request.cookies.get(ADMIN_SESSION_COOKIE, "")):
        return RedirectResponse(url="/static/admin_login.html?next=/account-import")
    return FileResponse(ACCOUNT_IMPORT_HTML)


@router.get("/static/account_import.html", include_in_schema=False)
def account_import_static():
    return FileResponse(ACCOUNT_IMPORT_HTML)


@router.get("/static/admin_login.html", include_in_schema=False)
def admin_login_static():
    return FileResponse(ADMIN_LOGIN_HTML)


@router.post("/admin/login", include_in_schema=False)
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if username.strip() != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    expire_ts = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    token = _sign_admin_token(ADMIN_USERNAME, expire_ts)
    resp = JSONResponse({"message": "ok"})
    resp.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ADMIN_SESSION_TTL_SECONDS,
        secure=False,
    )
    return resp


@router.post("/admin/logout", include_in_schema=False)
def admin_logout():
    resp = JSONResponse({"message": "ok"})
    resp.delete_cookie(ADMIN_SESSION_COOKIE)
    return resp


@router.get("/admin/me", include_in_schema=False)
def admin_me(request: Request, authorization: str = Header(default="")):
    return {"username": _require_import_admin(request, authorization), "role": "admin"}


@router.post("/preview_accounts_excel")
async def preview_accounts_excel(
    request: Request,
    file: UploadFile = File(...),
    account_type: str = Form("US"),
    target_country: str = Form("US"),
    authorization: str = Header(default=""),
):
    _require_import_admin(request, authorization)
    filename = (file.filename or "").lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise HTTPException(status_code=422, detail="仅支持 xlsx/xls 文件")
    acct_type = str(account_type or "").strip().upper()
    if acct_type not in {"US", "JP", "AE"}:
        raise HTTPException(status_code=422, detail="account_type 仅支持 US/JP/AE")
    country = str(target_country or "").strip().upper() or acct_type
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="上传文件为空")

    try:
        parsed = parse_account_excel(BytesIO(content), country)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    accounts = parsed.get("accounts", []) or []
    preview_rows = []
    for item in accounts[:20]:
        proxy_raw = str(item.get("proxy") or "").strip()
        proxy_valid = True
        if proxy_raw:
            try:
                _parse_static_ips_text(proxy_raw)
            except HTTPException:
                proxy_valid = False
        preview_rows.append({
            "row_no": int(item.get("row_no") or 0),
            "username": str(item.get("username") or ""),
            "password_present": bool(item.get("password")),
            "totp_present": bool(item.get("totp_secret")),
            "browser_id_present": bool(item.get("browser_id")),
            "proxy_present": bool(proxy_raw),
            "proxy_valid": proxy_valid,
            "valid": bool(item.get("username") and item.get("password")),
        })
    return {
        "message": "preview success",
        "account_type": acct_type,
        "target_country": country,
        "proxy_strategy": "static_if_provided_else_dynamic",
        "excel_format": parsed.get("format"),
        "has_header": bool(parsed.get("has_header")),
        "header_columns": parsed.get("columns", []),
        "required_columns": list(ACCOUNT_REQUIRED_COLUMNS),
        "optional_columns": list(ACCOUNT_OPTIONAL_COLUMNS),
        "missing_columns": parsed.get("missing_columns", []),
        "total_rows": int(parsed.get("total_rows", len(accounts))),
        "valid_rows": len(accounts),
        "browser_id_rows": sum(1 for item in accounts if item.get("browser_id")),
        "file_proxy_rows": sum(1 for item in accounts if item.get("proxy")),
        "preview_rows": preview_rows,
    }


@router.post("/import_accounts_excel")
async def import_accounts_excel(
    request: Request,
    file: UploadFile = File(...),
    account_type: str = Form("US"),
    target_country: str = Form("US"),
    static_ip_count: int = Form(0),
    static_ips: str = Form(""),
    limit: int = Form(0),
    authorization: str = Header(default=""),
):
    username = _require_import_admin(request, authorization)
    filename = (file.filename or "").lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise HTTPException(status_code=422, detail="仅支持 xlsx/xls 文件")
    acct_type = str(account_type or "").strip().upper()
    if acct_type not in {"US", "JP", "AE"}:
        raise HTTPException(status_code=422, detail="account_type 仅支持 US/JP/AE")
    country = str(target_country or "").strip().upper() or acct_type
    if limit < 0 or static_ip_count < 0:
        raise HTTPException(status_code=422, detail="limit/static_ip_count 不能小于 0")
    static_ip_pool = _parse_static_ips_text(static_ips)
    use_static = bool(static_ip_pool)
    if use_static and static_ip_count > len(static_ip_pool):
        raise HTTPException(status_code=422, detail=f"static_ip_count 不能超过已提供静态IP数量({len(static_ip_pool)})")
    effective_static_ip_count = static_ip_count if (use_static and static_ip_count > 0) else len(static_ip_pool)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="上传文件为空")

    try:
        parsed = parse_account_excel(BytesIO(content), country)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    raw_accounts = parsed.get("accounts", []) or []
    selected_accounts = raw_accounts[:int(limit)] if int(limit or 0) > 0 else raw_accounts
    if not selected_accounts:
        return {
            "message": "import success",
            "account_type": acct_type,
            "target_country": country,
            "proxy_strategy": "dynamic",
            "static_ip_count": 0,
            "provided_static_ip_count": len(static_ip_pool),
            "source_rows": len(raw_accounts),
            "attempted_rows": 0,
            "created_count": 0,
            "failed_count": 0,
            "existing_browser_count": 0,
            "file_proxy_count": 0,
            "created_usernames": [],
            "failed_items": [],
        }

    existing_browser_accounts = [item for item in selected_accounts if str(item.get("browser_id") or "").strip()]
    missing_browser_accounts = [item for item in selected_accounts if not str(item.get("browser_id") or "").strip()]
    file_proxy_count = sum(1 for item in selected_accounts if str(item.get("proxy") or "").strip())
    proxy_strategy = _account_import_proxy_strategy(file_proxy_count, use_static)
    created = []
    failed_items = []

    db = MySQLTaskDB()
    try:
        for index, account in enumerate(existing_browser_accounts, start=1):
            try:
                record = _build_existing_browser_account(
                    account,
                    country,
                    effective_static_ip_count if use_static else 0,
                    static_ip_pool,
                    index,
                )
                db.insert_account(record)
                created.append(record)
            except Exception as exc:
                failed_items.append({
                    "row_no": int(account.get("row_no") or 0),
                    "username": str(account.get("username") or ""),
                    "reason": str(exc)[:240],
                })
        if missing_browser_accounts:
            job = db.create_account_import_job(
                job_id=str(uuid.uuid4()),
                accounts=missing_browser_accounts,
                account_type=acct_type,
                target_country=country,
                static_ip_count=effective_static_ip_count if use_static else 0,
                static_ip_pool=static_ip_pool,
                limit_count=0,
                proxy_strategy=proxy_strategy,
                created_by=username,
            )
            resp = _format_account_import_job_response(job)
            resp["message"] = "账号导入任务已入队，请保持 Windows browser-node 运行"
            resp["missing_browser_count"] = len(missing_browser_accounts)
            resp["source_rows"] = len(raw_accounts)
            resp["existing_browser_count"] = len(created)
            resp["file_proxy_count"] = file_proxy_count
            resp["created_count"] = len(created)
            resp["failed_count"] = len(failed_items)
            resp["created_usernames"] = [str(item.get("username", "")) for item in created[:50]]
            resp["failed_items"] = failed_items[:200]
            return resp
    finally:
        db.close()

    return {
        "message": "import success",
        "account_type": acct_type,
        "target_country": country,
        "proxy_strategy": proxy_strategy,
        "static_ip_count": effective_static_ip_count if use_static else 0,
        "provided_static_ip_count": len(static_ip_pool),
        "source_rows": len(raw_accounts),
        "attempted_rows": len(existing_browser_accounts),
        "created_count": len(created),
        "failed_count": len(failed_items),
        "existing_browser_count": len(created),
        "file_proxy_count": file_proxy_count,
        "created_usernames": [str(item.get("username", "")) for item in created[:50]],
        "failed_items": failed_items[:200],
    }


@router.get("/account_import_jobs/{job_id}")
def get_account_import_job(job_id: str, request: Request, authorization: str = Header(default="")):
    _require_import_admin(request, authorization)
    db = MySQLTaskDB()
    try:
        job = db.refresh_account_import_job_stats(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="账号导入任务不存在")
        return _format_account_import_job_response(job)
    finally:
        db.close()
