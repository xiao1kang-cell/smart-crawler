"""
BitBrowser 指纹浏览器窗口管理工具。

只负责 BitBrowser API 的 open / close 调用，不涉及 Playwright CDP 接管逻辑。
- open_browser()  : 打开指纹窗口，返回 CDP 调试地址
- close_browser() : 关闭指纹窗口，处理 token 失效告警
- delete_browser(): 删除指纹浏览器窗口配置，处理 token 失效告警
- ensure_bit_browser_running() : 本机模式下自动拉起比特浏览器进程

amazon_base.py / reviews_playwright.py 通过调用这里的函数保持一致行为。
"""

import os
import random
import time

import requests as http_requests
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message

BIT_BROWSER_IP = os.getenv('BIT_BROWSER_IP', '127.0.0.1')
BIT_API_BASE = f"http://{BIT_BROWSER_IP}:54345"
_BIT_BROWSER_RATE_CLIENT = None


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return default


def is_bit_browser_headless() -> bool:
    """Read headless mode at call time so each worker process can override it."""
    return os.getenv('BIT_BROWSER_HEADLESS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')


BIT_BROWSER_HEADLESS = is_bit_browser_headless()


def _get_bitbrowser_rate_client():
    global _BIT_BROWSER_RATE_CLIENT
    if _BIT_BROWSER_RATE_CLIENT is not None:
        return _BIT_BROWSER_RATE_CLIENT or None
    try:
        import redis
        from app.crawlers.amazon_crawler.shuler.util.config import (
            REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT, REDIS_USERNAME,
        )
        _BIT_BROWSER_RATE_CLIENT = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            username=REDIS_USERNAME,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    except Exception as exc:
        logger.warning(f"BitBrowser 启动限流 Redis 初始化失败，将跳过全局限流: {exc}")
        _BIT_BROWSER_RATE_CLIENT = False
    return _BIT_BROWSER_RATE_CLIENT or None


def _wait_for_bitbrowser_open_slot() -> None:
    """
    BitBrowser 本地 API 对 /browser/open 有每秒请求上限。
    用 Redis 秒级计数在多进程之间限速，避免恢复积压任务时一起打爆 API。
    """
    limit = _env_int("BIT_BROWSER_OPEN_RPS_LIMIT", 8)
    if limit <= 0:
        return
    client = _get_bitbrowser_rate_client()
    if client is None:
        return

    max_wait = _env_int("BIT_BROWSER_OPEN_RATE_WAIT_SECONDS", 120, minimum=1)
    deadline = time.monotonic() + max_wait
    waited = 0.0
    while True:
        try:
            now = time.time()
            bucket = int(now)
            key = f"crawler:bitbrowser:open_rate:{bucket}"
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, 2)
            if count <= limit:
                if waited >= 1.0:
                    logger.info(f"BitBrowser open 限流等待 {waited:.1f}s 后放行")
                return

            sleep_seconds = max(0.02, bucket + 1 - time.time()) + random.uniform(0.02, 0.2)
            if time.monotonic() + sleep_seconds > deadline:
                logger.warning(
                    f"BitBrowser open 限流等待超过 {max_wait}s，放行一次以避免 worker 卡死"
                )
                return
            time.sleep(sleep_seconds)
            waited += sleep_seconds
        except Exception as exc:
            logger.warning(f"BitBrowser open 限流失败，将直接请求: {exc}")
            return


# ─────────────────────────── 自定义异常 ───────────────────────────

class ProfileNotFoundError(Exception):
    """BitBrowser 找不到账号对应的指纹 profile（账号配置错误）"""
    pass


class TokenExpiredError(Exception):
    """BitBrowser token 失效，需要人工重新登录比特浏览器"""
    pass


class AccountLoginError(Exception):
    """账号自身问题导致的登录失败（密码错误/人工验证/TOTP未配置），不应重试"""
    pass


# ─────────────────────────── 工具函数 ────────────────────────────

def ensure_bit_browser_running(wait_seconds: int = 20) -> None:
    """
    探测本机比特浏览器 API 是否可达，不可达时按平台自动拉起。
    仅在 BIT_BROWSER_IP 指向本机 (127.0.0.1 / localhost) 时有效。
    """
    import subprocess

    # 快速探测：已在运行则直接返回
    try:
        http_requests.get("http://127.0.0.1:54345/browser/list", timeout=2)
        return
    except Exception:
        pass

    try:
        from app.crawlers.amazon_crawler.shuler.util.config import BIT_BROWSER_APP_PATH
        app_path = BIT_BROWSER_APP_PATH
    except Exception:
        app_path = None

    if not app_path or not os.path.exists(app_path):
        logger.warning("BIT_BROWSER_APP_PATH 未配置或路径不存在，跳过自动启动比特浏览器")
        return

    logger.info(f"比特浏览器未运行，正在自动启动: {app_path}")
    try:
        if app_path.endswith(".app"):
            subprocess.Popen(["open", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([app_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning(f"启动比特浏览器失败: {e}")
        return

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        try:
            http_requests.get("http://127.0.0.1:54345/browser/list", timeout=2)
            logger.info("比特浏览器 API 已就绪")
            return
        except Exception:
            pass
    logger.warning(f"比特浏览器在 {wait_seconds}s 内未就绪，继续尝试连接")


def open_browser(fingerprint_id: str) -> str:
    """
    调用 BitBrowser API 打开指纹浏览器窗口（最多重试3次）。

    返回值：CDP 调试地址字符串，如 "127.0.0.1:9222"。
    调用方拿到地址后自行用 Playwright connect_over_cdp 接管。

    异常：
      ProfileNotFoundError — BitBrowser 返回"没有找到相应数据"，账号 profile 配置错误
      Exception            — 其他启动失败
    """
    open_url = f"{BIT_API_BASE}/browser/open"
    headless = is_bit_browser_headless()
    launch_args = ["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"]
    if headless:
        launch_args.append("--headless")

    open_data = {
        "id": fingerprint_id,
        "ignoreDefaultUrls": True,
        "queue": True,
        "args": launch_args,
    }
    if not headless:
        # 无头不要配置newPageUrl
        open_data["newPageUrl"] = "about:blank"
    logger.info(f"启动指纹浏览器: {fingerprint_id}, headless={headless}")

    open_resp = None
    for attempt in range(5):
        try:
            _wait_for_bitbrowser_open_slot()
            resp = http_requests.post(open_url, json=open_data, timeout=300)
            open_resp = resp.json()
            if open_resp.get("success"):
                http_addr = open_resp["data"]["http"]
                logger.info(f"指纹浏览器已就绪，CDP 端点: http://{http_addr}")
                return http_addr
            logger.warning(f"BitBrowser 启动失败 (attempt {attempt+1}/5): {open_resp}")
            # 502 说明服务暂时过载，等更长时间再重试
            wait = 15 if '502' in str(open_resp) else 2 ** attempt
            if attempt < 4:
                time.sleep(wait)
            continue
        except Exception as e:
            err_str = str(e)
            # Connection refused → 本机模式下尝试自动拉起比特浏览器（仅第一次）
            if "Connection refused" in err_str and attempt == 0 and BIT_BROWSER_IP in ("127.0.0.1", "localhost"):
                ensure_bit_browser_running()
            logger.warning(f"BitBrowser POST 失败 (attempt {attempt+1}/5): {e}")
        if attempt < 4:
            time.sleep(2 ** min(attempt, 3))  # 指数退避: 1s, 2s, 4s, 8s

    # 五次均失败，判断具体原因
    err_msg = (open_resp or {}).get('msg', '')
    if '没有找到相应数据' in err_msg:
        raise ProfileNotFoundError(
            f"账号的指纹 profile 不存在 (fingerprint_id={fingerprint_id}): {open_resp}"
        )
    raise Exception(f"指纹浏览器启动失败: {open_resp}")


def close_browser(fingerprint_id: str) -> bool:
    """
    调用 BitBrowser API 关闭指纹浏览器窗口。

    token 失效时：发 DingTalk 告警 → sleep 5min（给运维时间重新登录）→ 抛 TokenExpiredError。
    返回 True 表示正常关闭，False 表示关闭失败（非 token 失效）。
    """
    close_url = f"{BIT_API_BASE}/browser/close"
    try:
        resp = http_requests.post(close_url, json={"id": fingerprint_id}, timeout=30).json()
        if resp.get("success"):
            logger.info(f"指纹浏览器已关闭: {fingerprint_id}")
            return True
        logger.warning(f"指纹浏览器关闭失败: {resp}")
        if 'token 失效' in resp.get('msg', ''):
            send_custom_robot_group_message(
                '[指纹浏览器] BitBrowser token 失效，请检查登录状态，worker 等待5分钟后继续',
                at_mobiles=['17398238551']
            )
            time.sleep(300)
            raise TokenExpiredError("BitBrowser token 失效")
        return False
    except TokenExpiredError:
        raise
    except Exception as e:
        logger.warning(f"关闭指纹浏览器异常: {e}")
        return False


def delete_browser(fingerprint_id: str) -> bool:
    """
    调用 BitBrowser API 删除指纹浏览器窗口。

    注意：这是删除窗口配置，不是关闭已打开窗口；如果只是结束本次会话，应调用 close_browser()。
    token 失效时：发 DingTalk 告警 → sleep 5min（给运维时间重新登录）→ 抛 TokenExpiredError。
    返回 True 表示删除成功，False 表示删除失败（非 token 失效）。
    """
    delete_url = f"{BIT_API_BASE}/browser/delete"
    try:
        resp = http_requests.post(delete_url, json={"id": fingerprint_id}, timeout=30).json()
        if resp.get("success"):
            logger.info(f"指纹浏览器窗口已删除: {fingerprint_id}")
            return True
        logger.warning(f"指纹浏览器窗口删除失败: {resp}")
        if 'token 失效' in resp.get('msg', ''):
            send_custom_robot_group_message(
                '[指纹浏览器] BitBrowser token 失效，请检查登录状态，worker 等待5分钟后继续',
                at_mobiles=['17398238551']
            )
            time.sleep(300)
            raise TokenExpiredError("BitBrowser token 失效")
        return False
    except TokenExpiredError:
        raise
    except Exception as e:
        logger.warning(f"删除指纹浏览器窗口异常: {e}")
        return False


def _perform_amazon_login(
    page,
    email: str,
    password: str,
    totp_secret: str,
    on_disable_account,
) -> None:
    """
    单次登录尝试（内部函数，由 playwright_amazon_login 带重试逻辑调用）。

    账号自身问题抛 AccountLoginError（不重试）；
    其他问题抛普通 Exception（调用方可重试）。
    """
    import pyotp

    # 1. 邮箱（账号已预填时元素不存在或为隐藏域，直接跳过）
    email_input = None
    for selector in ('#ap_email_login', '#ap_email'):
        el = page.query_selector(selector)
        if el and el.is_visible():
            email_input = el
            break
    if email_input:
        email_input.fill(email)
        time.sleep(0.5)

    # 2. 点击继续（排除 OTP 登录按钮 和 Passkey 登录按钮）
    # 注意：Amazon JP 登录页的 #continue 有时是"パスキーでサインイン"（通行密钥）按钮，
    # 点击后会触发 Chrome 的 Credential Manager 弹窗导致卡死，必须跳过。
    continue_btn = page.query_selector('#continue')
    if continue_btn:
        aria_label = continue_btn.get_attribute('aria-labelledby') or ''
        btn_text = ''
        try:
            btn_text = (continue_btn.inner_text() or '').strip()
        except Exception:
            pass
        _PASSKEY_KEYWORDS = ['パスキー', 'passkey', 'Passkey', 'Sign in with Passkey',
                             'Passkey', 'passkeys', 'パスキーで']
        is_passkey_btn = any(kw in btn_text for kw in _PASSKEY_KEYWORDS)
        is_otp_btn = 'auth-login-via-otp' in aria_label
        if not is_otp_btn and not is_passkey_btn:
            try:
                continue_btn.click(timeout=5000)
            except Exception as e:
                logger.warning(f"[登录] #continue 点击失败（可能被禁用），跳过: {e}")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
            time.sleep(1)
        elif is_passkey_btn:
            logger.info(f"[登录] #continue 是通行密钥按钮（{btn_text!r}），已跳过")

    # 3. 输入密码（等待元素出现，最多 5 秒）
    pwd_input = page.wait_for_selector('#ap_password', timeout=5000)
    if pwd_input:
        pwd_input.fill(password)
        time.sleep(0.5)

    # 4. Keep me signed in
    try:
        keep_signed = page.query_selector('[aria-label="Keep me signed in."]')
        if keep_signed:
            keep_signed.click()
    except Exception:
        pass

    # 5. 点击登录，等页面稳定后再检测 MFA
    sign_in_btn = page.query_selector('#signInSubmit')
    if sign_in_btn:
        sign_in_btn.click()
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
    time.sleep(1)

    # 6. 检测密码/账号错误
    # 页面导航时 query_selector 会抛 "Execution context was destroyed"，
    # 此时说明登录已提交跳转，不是密码错误，直接跳过检测。
    try:
        error_box = page.query_selector('#auth-error-message-box')
    except Exception:
        error_box = None
    if error_box:
        error_text = (error_box.inner_text() or '').lower()
        if 'incorrect' in error_text or 'password' in error_text or 'problem' in error_text:
            if on_disable_account:
                on_disable_account(email, 'password_incorrect')
            send_custom_robot_group_message(
                f'[账号停用] 密码错误，已停用等待人工处理: {email}',
                at_mobiles=['17398238551']
            )
            raise AccountLoginError(f'账号密码错误，已停用: {email}')

    # 7. 二次验证 (TOTP)
    # wait_for_selector 等待输入框出现（最多 5 秒），避免 MFA 页未渲染完时漏检
    mfa_input = None
    try:
        mfa_input = page.wait_for_selector('#auth-mfa-otpcode', timeout=5000)
    except Exception:
        pass
    if mfa_input:
        logger.info('二次验证 (TOTP)')
        if not totp_secret:
            raise AccountLoginError(f'账号 {email} 需要 TOTP 二次验证，但未配置 totp_secret')
        cleaned_secret = totp_secret.replace(' ', '').replace('-', '').replace('_', '').upper()
        auth_code = pyotp.TOTP(cleaned_secret).now()
        mfa_input.fill(auth_code)
        try:
            remember_cb = page.query_selector('#auth-mfa-remember-device')
            if remember_cb and not remember_cb.is_checked():
                remember_cb.click()
        except Exception:
            pass
        page.click('.a-button.a-button-span12.a-button-primary.auth-disable-button-on-submit')
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=5000)

    # 8. 其他验证页（手机/邮件 OTP、交易审批、人机挑战等）→ 停用账号 + 告警
    current_url = page.url
    _MANUAL_VERIFY_PATTERNS = ['/cvf/',  '/ap/challenge', '/ap/dcq', 'authenticationContext']
    if any(p in current_url for p in _MANUAL_VERIFY_PATTERNS):
        if on_disable_account:
            on_disable_account(email, f'manual_verify_required url={current_url}')
        logger.error(f'账号需要人工验证，已停用 (url={current_url}): {email}')
        send_custom_robot_group_message(
            f'[账号停用] 需要人工验证 (url={current_url}): {email}，已停用等待人工处理',
            at_mobiles=['17398238551']
        )
        raise AccountLoginError(f'账号需要人工验证，已停用: {email}')

    # 9. 检测是否还停留在登录页（非账号问题，可重试）
    if 'signin' in page.url:
        raise Exception(f'登录失败: {email}')

    logger.info(f'Amazon 登录成功: {email}')


def playwright_amazon_login(
    page,
    email: str,
    password: str,
    totp_secret: str = None,
    on_disable_account=None,
    max_retries: int = 2,
) -> None:
    """
    在已处于 Amazon 登录页的 Playwright page 上执行完整登录流程，支持自动重试。

    成功后返回（不做页面跳转，由调用方处理后续导航）。

    重试策略：
      - AccountLoginError（密码错误/人工验证/TOTP未配置）→ 立即抛出，不重试
      - 其他异常（页面超时/元素未找到等网络/渲染问题）→ 刷新回登录页后重试
      - 默认最多重试 2 次（共 3 次尝试）

    on_disable_account: 可选回调 (username: str, reason: str)，账号停用时调用。
    """
    login_url = page.url
    last_error = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"[登录] 第 {attempt + 1} 次重试，刷新登录页: {login_url}")
            try:
                page.goto(login_url, timeout=15000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                time.sleep(1)
            except Exception as nav_err:
                logger.warning(f"[登录] 刷新登录页失败: {nav_err}")

        try:
            _perform_amazon_login(page, email, password, totp_secret, on_disable_account)
            return  # 登录成功
        except AccountLoginError:
            raise  # 账号自身问题，不重试
        except Exception as e:
            last_error = e
            logger.warning(f"[登录] 第 {attempt + 1}/{max_retries + 1} 次失败（非账号问题，将重试）: {e}")

    raise Exception(f'登录失败（已重试 {max_retries} 次）: {last_error}')

if __name__ == '__main__':
    close_browser('acc976dca7184f329079e083f59d0ff5')
