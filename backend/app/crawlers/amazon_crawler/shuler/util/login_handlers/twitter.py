import time
from typing import Dict

from loguru import logger
from playwright.sync_api import sync_playwright

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account


class TwitterLoginHandler:
    platform = "twitter"

    def login(self, account: Account) -> Dict[str, str]:
        """使用 Playwright 登录推特，返回 {auth_token, ct0}"""
        logger.info(f"[twitter-login] 开始登录: {account.username}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=account.user_agent or (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            try:
                page.goto("https://x.com/i/flow/login", timeout=30000)

                page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
                page.fill('input[autocomplete="username"]', account.username)
                page.keyboard.press("Enter")

                try:
                    page.wait_for_selector('input[data-testid="ocfEnterTextTextInput"]', timeout=5000)
                    page.fill('input[data-testid="ocfEnterTextTextInput"]', account.username)
                    page.keyboard.press("Enter")
                except Exception:
                    pass

                page.wait_for_selector('input[autocomplete="current-password"]', timeout=15000)
                page.fill('input[autocomplete="current-password"]', account.password)
                page.keyboard.press("Enter")

                if account.totp_secret:
                    import pyotp
                    totp_code = pyotp.TOTP(account.totp_secret).now()
                    try:
                        page.wait_for_selector('input[data-testid="LoginTwoFactorAuthOTPCodePage"]', timeout=8000)
                        page.fill('input[data-testid="LoginTwoFactorAuthOTPCodePage"]', totp_code)
                        page.keyboard.press("Enter")
                    except Exception:
                        pass

                page.wait_for_url("https://x.com/home", timeout=20000)
                time.sleep(2)

                cookies = context.cookies()
                result = {
                    c["name"]: c["value"]
                    for c in cookies
                    if c["name"] in ("auth_token", "ct0")
                }
                if "auth_token" not in result or "ct0" not in result:
                    raise RuntimeError(f"登录后未找到 auth_token/ct0，账号: {account.username}")

                logger.info(f"[twitter-login] 登录成功: {account.username}")
                return result

            finally:
                context.close()
                browser.close()
