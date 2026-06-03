"""登录鉴权 —— PBKDF2 口令哈希 + HMAC 签名 Token（零外部依赖）。"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass

# 生产必须经环境变量注入 SC_SECRET；下面只是开发兜底，绝不可用于生产
SECRET = os.environ.get("SC_SECRET", "dev-only-insecure-change-via-env")
TOKEN_TTL = 7 * 24 * 3600          # Token 有效期 7 天
_ITER = 100_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class TokenInfo:
    username: str
    exp: int
    session_id: str | None = None


def hash_password(password: str, salt: str | None = None) -> str:
    """生成 `salt$hash` 形式的口令哈希。"""
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITER)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITER)
    return hmac.compare_digest(calc.hex(), h)


def hash_secret(raw: str) -> str:
    """对一次性 secret 做带应用密钥的 hash。"""
    return hashlib.sha256((SECRET + (raw or "")).encode()).hexdigest()


def generate_session_id() -> str:
    return secrets.token_urlsafe(24)


def make_token(username: str, session_id: str | None = None) -> str:
    """签发 Token。

    兼容旧格式 `username:expiry:sig`；新登录使用
    `username:session_id:expiry:sig`，以便服务端撤销 session。
    """
    exp = int(time.time()) + TOKEN_TTL
    msg = f"{username}:{session_id}:{exp}" if session_id else f"{username}:{exp}"
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def parse_token(token: str | None) -> TokenInfo | None:
    """校验 Token，返回结构化信息或 None。"""
    if not token:
        return None
    try:
        parts = token.rsplit(":", 3)
        if len(parts) == 3:
            username, exp, sig = parts
            session_id = None
            msg = f"{username}:{exp}"
        elif len(parts) == 4:
            username, session_id, exp, sig = parts
            msg = f"{username}:{session_id}:{exp}"
        else:
            return None
    except ValueError:
        return None
    expected = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        exp_int = int(exp)
    except ValueError:
        return None
    if exp_int < time.time():
        return None
    return TokenInfo(username=username, exp=exp_int, session_id=session_id)


def verify_token(token: str | None) -> str | None:
    """校验 Token，返回 username 或 None。"""
    info = parse_token(token)
    return info.username if info else None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("邮箱格式不正确")
    return email


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线、短横线或点号")
    return username


def validate_password_strength(password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    if not re.search(r"[A-Za-z]", password or "") or not re.search(r"\d", password or ""):
        raise ValueError("密码必须同时包含字母和数字")
