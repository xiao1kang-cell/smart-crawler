"""登录鉴权 —— PBKDF2 口令哈希 + HMAC 签名 Token（零外部依赖）。"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

# 生产必须经环境变量注入 SC_SECRET；下面只是开发兜底，绝不可用于生产
SECRET = os.environ.get("SC_SECRET", "dev-only-insecure-change-via-env")
TOKEN_TTL = 7 * 24 * 3600          # Token 有效期 7 天
_ITER = 100_000


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


def make_token(username: str) -> str:
    """签发 `username:expiry:sig` 形式的 Token。"""
    exp = int(time.time()) + TOKEN_TTL
    msg = f"{username}:{exp}"
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def verify_token(token: str | None) -> str | None:
    """校验 Token，返回 username 或 None。"""
    if not token:
        return None
    try:
        username, exp, sig = token.rsplit(":", 2)
    except ValueError:
        return None
    msg = f"{username}:{exp}"
    expected = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(exp) < time.time():
        return None
    return username
