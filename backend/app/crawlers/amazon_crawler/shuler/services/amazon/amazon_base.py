import json
import random
import re
import time
import traceback
from datetime import datetime
from http.cookiejar import Cookie

import pyotp
from typing import Optional

import requests
from curl_cffi import requests as curl_requests

from curl_cffi.const import CurlOpt, CurlSslVersion
from loguru import logger
from retrying import retry

import os
import requests as http_requests
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import PROXY_MAPPING, SITE_MAPPING
# from app.crawlers.amazon_crawler.shuler.util.account_manage import AccountManager
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager as AccountManager
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message

BIT_BROWSER_IP = os.getenv('BIT_BROWSER_IP', '127.0.0.1')
BIT_API_BASE = f"http://{BIT_BROWSER_IP}:54345"
from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
from app.crawlers.amazon_crawler.shuler.util.event_logger import push_event, EventType
from app.crawlers.amazon_crawler.shuler.util.config import BIT_BROWSER_APP_PATH
import warnings
from urllib3.exceptions import InsecureRequestWarning
# 第二步：关闭 urllib3 的 InsecureRequestWarning 警告
warnings.simplefilter('ignore', InsecureRequestWarning)
MAX_REQUEST = 5


CDP_CONNECT_TIMEOUT_SECONDS = 20
CDP_CONNECT_ATTEMPTS = 2
CDP_CONNECT_RETRY_SLEEP_SECONDS = 1.0


def _start_playwright_in_clean_thread():
    """绕过 sync_playwright().start() 的两处 asyncio running-loop 检测。

    curl_cffi 遗留 running 状态的 asyncio loop，导致 Playwright sync API 有两处拦截：
      1. __enter__ 第47行：asyncio.get_running_loop() 成功 → 抛 "Please use Async API"
      2. greenlet_main 第56行：loop.run_until_complete() → _check_running() → 抛 "Cannot run..."

    修复：同时 patch 这两处，启动完成后立即还原。
    """
    import asyncio
    from greenlet import getcurrent
    from playwright.sync_api import sync_playwright

    _orig_get_running = asyncio.get_running_loop
    _orig_check = asyncio.BaseEventLoop._check_running
    _caller = getcurrent()

    def _no_running_loop():
        # 只在调用者 greenlet 里伪装成没有 running loop，通过 Playwright 的检测
        # Playwright 内部的 dispatcher_fiber greenlet 仍可拿到真实的 loop
        if getcurrent() is _caller:
            raise RuntimeError("no running event loop")
        return _orig_get_running()

    asyncio.get_running_loop = _no_running_loop
    asyncio.BaseEventLoop._check_running = lambda self: None
    try:
        return sync_playwright().start()
    finally:
        asyncio.get_running_loop = _orig_get_running
        asyncio.BaseEventLoop._check_running = _orig_check


# 自定义异常
class TLSAntiCrawlException(Exception):
    """检测到TSL反爬需要切换curl session"""
    pass


class CookieRefreshExhaustedException(Exception):
    """Cookie 过期后刷新超过上限，疑似指纹浏览器/会话异常"""
    pass


class AccountSwitchRequiredException(Exception):
    """当前账号/代理组合连续触发验证码或代理异常，需要换账号重试。"""
    pass


class NetworkException(Exception):
    """网络层故障（net::ERR_* / 导航超时），与风控无关"""
    pass


class CDPConnectException(Exception):
    """Playwright 接管 BitBrowser CDP 失败。"""
    pass


class AmazonBase:
    def __init__(self,account_info,task=None):

        self.account_info = account_info
        self.task = task
        self.page = None
        self._playwright = None
        self._browser = None
        self._is_cdp = False
        self.cookies = None
        self.user_agent = None
        self.proxies = {}
        self.first_page_url = ''
        self.curl_session = curl_requests.Session()
        self.session = requests.Session()
        # self.session.curl_options =  {
        #         # 解决 TLS 版本不匹配（核心）
        #         CurlOpt.SSLVERSION: CurlSslVersion.TLSv1_2,
        #         # 禁用老旧 SSL/TLS 版本（增强兼容性）
        #         CurlOpt.SSL_OPTIONS: 15,
        #         # 禁用 HTTP2，强制 HTTP1.1（解决亚马逊 HTTP2 握手异常）
        #         CurlOpt.HTTP_VERSION: 1  # 1 = CURL_HTTP_VERSION_1_1
        # }
        if self.task and not self.account_info:
            self.get_account_info(task)
        self.use_curl_session = True #是curl 针对tsl指纹检测
        self.use_local_browser = False  # True=本地Chrome, False=指纹浏览器
        # 绑定了 asin/account/country 的 logger，子类的 get_reviews_main() 会覆盖它
        self._log = logger
        # 真实 IP 缓存（每 12 分钟刷新一次，代理 15 分钟轮换留余量）
        self._proxy_ip_cache: str = ''
        self._proxy_ip_cache_ts: float = 0.0

    def get_account_info(self,task):
        if not self.account_info:
            account_manager = AccountManager()
            self.account_info = account_manager.get_account({"country":task['country']})
            # print(self.account_info.username)
    def init_dp(self, code):
        # 已有 page 时只做本地连接状态检查。不要用 page.evaluate() 探活：
        # CDP 半断开时 Playwright 协议调用可能无视业务超时并长时间挂住。
        if self.page:
            if self._is_existing_page_usable():
                return
            logger.warning(f"[init_dp] 浏览器连接已断开，重新接管")
            self.page = None

        # 重试时旧 playwright 实例先 stop 再重建
        if self._playwright is not None or self._browser is not None:
            self._close_browser()

        # 每次 CDP 接管失败都关闭 BitBrowser 窗口并重新打开，避免复用卡住的调试端口。
        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import open_browser, close_browser, ProfileNotFoundError
        last_error = None
        for attempt in range(CDP_CONNECT_ATTEMPTS):
            cdp_endpoint = ''
            try:
                # sync_playwright().start() 内部检测 asyncio.get_running_loop()，
                # curl_cffi 在 Windows 上会留下 running loop，需用干净线程启动。
                self._playwright = _start_playwright_in_clean_thread()

                http_addr = open_browser(code)
                cdp_endpoint = f"http://{http_addr}"
                logger.info(
                    f"CDP 连接尝试 profile={code} endpoint={cdp_endpoint} "
                    f"attempt={attempt + 1}/{CDP_CONNECT_ATTEMPTS} timeout={CDP_CONNECT_TIMEOUT_SECONDS}s"
                )
                self._browser = self._playwright.chromium.connect_over_cdp(
                    cdp_endpoint, timeout=CDP_CONNECT_TIMEOUT_SECONDS * 1000
                )
                self._is_cdp = True
                break
            except ProfileNotFoundError:
                username = getattr(self.account_info, 'username', 'unknown')
                send_custom_robot_group_message(
                    f'[指纹浏览器] 账号 {username} 浏览器配置不存在，已自动停用，请检查 BitBrowser profile 配置',
                    at_mobiles=['17398238551']
                )
                self._disable_account(username, reason='fingerprint_profile_not_found')
                self._close_browser()
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    f"CDP 连接失败 profile={code} endpoint={cdp_endpoint or '-'} "
                    f"attempt={attempt + 1}/{CDP_CONNECT_ATTEMPTS} "
                    f"timeout={CDP_CONNECT_TIMEOUT_SECONDS}s: {e}"
                )
                self._close_browser()
                try:
                    close_browser(code)
                except Exception as close_exc:
                    logger.warning(f"CDP 失败后关闭指纹浏览器异常 profile={code}: {close_exc}")
                if attempt < CDP_CONNECT_ATTEMPTS - 1:
                    time.sleep(CDP_CONNECT_RETRY_SLEEP_SECONDS)
        else:
            raise CDPConnectException(
                f"CDP 接管失败 profile={code}, attempts={CDP_CONNECT_ATTEMPTS}, "
                f"timeout={CDP_CONNECT_TIMEOUT_SECONDS}s: {last_error}"
            )

        # 4. 获取已有 context/page
        contexts = self._browser.contexts
        if contexts:
            ctx = contexts[0]
            self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            ctx = self._browser.new_context()
            self.page = ctx.new_page()

        try:
            ctx.set_default_timeout(15000)
            ctx.set_default_navigation_timeout(25000)
            self.page.set_default_timeout(15000)
            self.page.set_default_navigation_timeout(25000)
        except Exception as e:
            logger.warning(f"[init_dp] 设置 Playwright 默认超时失败（不影响主流程）: {e}")

        # 注入脚本：覆盖 WebAuthn/Passkey API，阻止 Amazon 触发 Chrome 凭据管理器弹窗
        # Chrome 的 "没有可用的通行密钥" 对话框由 navigator.credentials.get() 触发，
        # 拦截后 Amazon JS 会直接 fallback 到密码登录流程。
        try:
            self.page.add_init_script("""
                (() => {
                    const _reject = () => Promise.reject(
                        new DOMException('The operation either timed out or was not allowed.', 'NotAllowedError')
                    );
                    try {
                        Object.defineProperty(navigator, 'credentials', {
                            get: () => ({
                                get: _reject,
                                create: _reject,
                                store: () => Promise.resolve(),
                                preventSilentAccess: () => Promise.resolve(),
                            }),
                            configurable: true,
                        });
                    } catch(e) {}
                    // 同时覆盖 PublicKeyCredential，阻止 conditional mediation 检测
                    try {
                        if (window.PublicKeyCredential) {
                            window.PublicKeyCredential.isConditionalMediationAvailable = () => Promise.resolve(false);
                            window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = () => Promise.resolve(false);
                        }
                    } catch(e) {}
                })();
            """)
        except Exception as e:
            logger.warning(f"[WebAuthn拦截] add_init_script 失败（不影响主流程）: {e}")

        self.user_agent = self.page.evaluate("navigator.userAgent")
        logger.info(f"Playwright 已通过 CDP 接管指纹浏览器: {code}")

    def _is_existing_page_usable(self) -> bool:
        if not self.page or not self._browser:
            return False
        try:
            if hasattr(self._browser, "is_connected") and not self._browser.is_connected():
                return False
            if hasattr(self.page, "is_closed") and self.page.is_closed():
                return False
            return True
        except Exception:
            return False

    def _close_browser(self):
        """断开 Playwright 连接（CDP 模式只断连，不关闭指纹浏览器窗口）"""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self._is_cdp = False
        self.page = None

    def _quit_fingerprint_browser(self):
        """关闭 BitBrowser 指纹浏览器窗口（会话结束时调用）"""
        fingerprint_id = getattr(self, 'fingerprint_id', None) or getattr(self.account_info, 'fingerprint_id', None)
        if not fingerprint_id:
            return
        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import close_browser
        try:
            close_browser(fingerprint_id)
        except Exception as e:
            logger.warning(f"_quit_fingerprint_browser 异常: {e}")

    # ====================== 本地Chrome方法（替代指纹浏览器） ======================
    def init_local_dp(self):
        """使用本地 Chrome 替代指纹浏览器，通过独立 user-data-dir 隔离多账号"""
        if not self.page:
            from app.crawlers.amazon_crawler.shuler.util.local_browser import LocalChrome
            self.chrome = LocalChrome()
            proxy = getattr(self.account_info, 'proxy_', None)
            if proxy and 'http' not in proxy:
                proxy = f'http://{proxy["user"]}:{proxy["password"]}@{proxy["host"]}:{proxy["port"]}'
                proxies = {
                    'http': proxy,
                    'https': proxy,
                }
            elif not proxy:
                proxy = f'http://PsFaJMphAU0hH1s20E-zone-custom-region-{PROXY_MAPPING[self.task["country"].upper()]}-session-{int(time.time() * 1000)}-sessTime-5:iWrz7GbWhm@a477c1a8e06d7ff8.qzc.na.grassdata.net:2333'
                proxies = {
                    'http': proxy,
                    'https': proxy,
                }
            else:
                proxies = proxy
            print(f'当前使用代理：{proxies["http"]}')
            success = self.chrome.start(
                account_id=self.account_info.username,
                proxy=proxies
            )
            if not success:
                raise Exception('本地Chrome启动失败')
            self.page = self.chrome.page
            self.user_agent = self.chrome.user_agent
            self.proxies = proxies or {}

    @retry(stop_max_attempt_number=3, wait_random_min=1000, wait_random_max=2000)
    def get_info_from_local_dp(self, url):
        """使用本地Chrome获取页面信息并提取cookies（不依赖指纹浏览器API）"""
        try:
            current_time = datetime.now()
            self.init_local_dp()
            self.page.goto(url, timeout=15000)
            time.sleep(3)
            self.verify_login(url)
            self.cookies = None
            self.get_cookies_local()
            self.account_info.refresh_time = current_time.strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:

            raise

    def get_cookies_local(self):
        """从本地Chrome提取cookies（不调用指纹浏览器API获取代理）"""
        if f'https://{SITE_MAPPING[self.task["country"].upper()]}/' not in self.page.url:
            raise Exception('页面加载失败')
        if not self.cookies:
            is_login = False
            cookie_dict = {}
            for cookie in self.page.context.cookies():
                if 'ubid-' in cookie['name']:
                    is_login = True
                cookie_dict[cookie['name']] = cookie['value']
            if not is_login:
                raise Exception('cookies中没有ubid，登录失败')
            self.cookies = cookie_dict
            self.inject_cookies_to_session(
                cookie_dict,
                domain=SITE_MAPPING[self.task['country'].upper()].replace('www', '')
            )
            self.user_agent = self.page.evaluate("navigator.userAgent")
            self.proxies = getattr(self.account_info, 'proxy_', {}) or {}
            self.account_info.cookies = cookie_dict
            self.account_info.user_agent = self.user_agent

    def quit_local_dp(self):
        """关闭本地Chrome浏览器"""
        self._close_browser()

    def _simulate_human_behavior(self):
        """模拟人类浏览行为：滚动 + 鼠标移动，让页面交互更真实"""
        try:
            # 1. 随机滚动
            scroll_y = random.randint(200, 500)
            self.page.evaluate(f"window.scrollBy(0, {scroll_y})")
            time.sleep(random.uniform(0.4, 1.0))

            # 2. 随机鼠标移动（dispatch mousemove 事件）
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, 900)
                y = random.randint(150, 600)
                self.page.evaluate(
                    "([x, y]) => document.dispatchEvent(new MouseEvent('mousemove', "
                    "{clientX: x, clientY: y, bubbles: true}))",
                    [x, y]
                )
                time.sleep(random.uniform(0.1, 0.4))

            # 3. 随机悬停在某个元素上
            try:
                elements = self.page.locator('a, button, [role="button"]').all()
                if elements:
                    target = random.choice(elements[:min(5, len(elements))])
                    try:
                        target.scroll_into_view_if_needed()
                        time.sleep(random.uniform(0.3, 0.6))
                        target.hover()
                        time.sleep(random.uniform(0.5, 1.5))
                    except Exception:
                        pass
            except Exception:
                pass

            # 4. 再滚动一点
            self.page.evaluate(f"window.scrollBy(0, {random.randint(100, 300)})")
            time.sleep(random.uniform(0.3, 0.8))

        except Exception as e:
            logger.debug(f"[行为模拟] 异常（不影响主流程）: {e}")

    # ====================== 指纹浏览器方法（Playwright） ======================
    def _check_logged_in_state(self) -> bool:
        """通过导航栏文本判断当前页面是否处于真实登录态（不依赖 cookie 内容）。
        Amazon 登录后 #nav-link-accountList 的 aria-label 包含账号名而非 "Sign in"。
        """
        try:
            nav = self.page.query_selector('#nav-link-accountList')
            if not nav:
                return False
            label = (nav.get_attribute('aria-label') or '').lower()
            # 未登录时 aria-label 为 "Hello, sign in ..."，登录后为 "Hello, <name> ..."
            return 'sign in' not in label and 'hello' in label
        except Exception:
            return False

    def get_info_from_dp(self, url):
        self._log.info('从指纹浏览器获取 cookies 等')
        msg = 'get_info_from_dp error'
        for retry_ in range(3):
            try:
                step_start = time.perf_counter()
                current_time = datetime.now()
                self.init_dp(self.account_info.fingerprint_id)
                init_cost = time.perf_counter() - step_start
                try:
                    goto_start = time.perf_counter()
                    self.page.goto(url, timeout=12000, wait_until="domcontentloaded")
                    try:
                        self.page.wait_for_selector(
                            '#nav-link-accountList, form[name="signIn"], '
                            '[action="/errors/validateCaptcha"], #cm_cr-review_list, [data-hook="review"]',
                            timeout=3000,
                        )
                    except Exception:
                        pass
                    goto_cost = time.perf_counter() - goto_start
                except Exception as goto_exc:
                    goto_msg = str(goto_exc)
                    # ERR_HTTP_RESPONSE_CODE_FAILURE = Amazon 返回了 4xx/5xx（反爬/封禁）
                    # 不是代理连通性问题，重置 page 让下轮重新接管，等待后重试
                    if 'ERR_HTTP_RESPONSE_CODE_FAILURE' in goto_msg:
                        logger.warning(
                            f'get_info_from_dp: Amazon 返回非 2xx（反爬/限流），'
                            f'url={url}, retry={retry_+1}/3'
                        )
                        self.page = None
                        time.sleep(5 * (retry_ + 1))
                        raise
                    raise
                login_start = time.perf_counter()
                self.verify_login(url)
                login_cost = time.perf_counter() - login_start
                # 导航栏二次校验：确保真正登录成功（verify_login 依赖 URL 判断，不够可靠）
                # if not self._check_logged_in_state():
                #     logger.warning(
                #         f'get_info_from_dp: 导航栏显示未登录，触发重新登录 account={self.account_info.username}'
                #     )
                #     self.verify_login(url)   # 再试一次
                #     if not self._check_logged_in_state():
                #         raise Exception(f'登录态校验失败，导航栏仍显示未登录: {self.account_info.username}')
                if os.getenv('AMAZON_INIT_HUMAN_BEHAVIOR', 'false').strip().lower() in ('1', 'true', 'yes', 'on'):
                    behavior_start = time.perf_counter()
                    self._simulate_human_behavior()
                    behavior_cost = time.perf_counter() - behavior_start
                else:
                    behavior_cost = 0.0
                self.cookies = None
                cookie_start = time.perf_counter()
                self.get_cookies()
                cookie_cost = time.perf_counter() - cookie_start
                total_cost = time.perf_counter() - step_start
                self._log.info(
                    f"[指纹初始化耗时] total={total_cost:.1f}s init={init_cost:.1f}s "
                    f"goto={goto_cost:.1f}s login={login_cost:.1f}s "
                    f"behavior={behavior_cost:.1f}s cookies_proxy={cookie_cost:.1f}s"
                )
                self.account_info.refresh_time = current_time.strftime('%Y-%m-%d %H:%M:%S')
                return True
            except CDPConnectException:
                msg = traceback.format_exc()
                logger.error(f'get_info_from_dp CDP 接管失败，不继续刷新代理重试: {msg}')
                self._close_browser()
                self._quit_fingerprint_browser()
                raise
            except Exception:
                msg = traceback.format_exc()
                time.sleep(2)
                if '账号需要手机号验证' in msg or '账号需要人工验证' in msg or '账号密码错误' in msg or '账号的指纹 profile 不存在' in msg:
                    break
                # ERR_HTTP_RESPONSE_CODE_FAILURE 已在 goto 内单独处理，不刷代理
                if 'ERR_HTTP_RESPONSE_CODE_FAILURE' in msg:
                    logger.error(f'get_info_from_dp error (retry {retry_ + 1}/3): {msg}')
                elif 'net::ERR_' in msg or 'ERR_FAILED' in msg or 'TimeoutError: Page.goto' in msg:
                    # 代理连通性问题：刷新代理后重试
                    try:
                        self.proxies = self.get_proxies(self.account_info.fingerprint_id)
                        self.refresh_proxy(self.proxies['http'])
                        self.page = None
                        logger.info(f'get_info_from_dp: 检测到代理异常，已刷新代理')
                    except Exception:
                        logger.warning(f'get_info_from_dp: 刷新代理失败: {traceback.format_exc()}')
                else:
                    logger.error(f'get_info_from_dp error (retry {retry_ + 1}/3): {msg}')

        # send_custom_robot_group_message(f'get_info_from_dp error:{msg}', at_mobiles=['17398238551'])
        # 代理网络持续失效（而非账号问题）→ 抛带标记的异常，让上层切换账号而非无效等待
        if 'net::ERR_' in msg or 'ERR_FAILED' in msg or 'TimeoutError: Page.goto' in msg:
            raise Exception(f'指纹浏览器代理持续失效，已重试3次: {msg}')
        raise Exception(msg)

    def refresh_proxy(self,proxy):
        # 清缓存；_resolve_proxy_ip() 由调用方在 self.proxies 更新后调用
        self._proxy_ip_cache = ''
        self._proxy_ip_cache_ts = 0.0

        # 发射代理切换事件
        try:
            _rc = getattr(self, '_redis_client', None)
            if _rc is None:
                import redis as redis_lib
                from app.crawlers.amazon_crawler.shuler.util.config import (
                    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
                )
                _rc = redis_lib.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    username=REDIS_USERNAME, password=REDIS_PASSWORD,
                    db=REDIS_DB, decode_responses=True,
                    socket_connect_timeout=3, socket_timeout=3,
                )
                self._redis_client = _rc
            _username = getattr(self.account_info, 'username', '') if self.account_info else ''
            _country = (self.task or {}).get('country', '')
            push_event(
                _rc, EventType.PROXY_ROTATE,
                username=_username,
                country=_country,
                proxy=proxy[:60] if proxy else '',
            )
        except Exception:
            pass

        if 'session' not in proxy:
            print(f'{proxy} 静态代理 不需要刷新')
            time.sleep(3)
            return None
        # 记录切换前 IP，用于验证是否真正切换成功
        old_ip = ''
        try:
            old_ip = self._resolve_proxy_ip()
        except Exception:
            pass

        import requests
        for retry_ in range(4):
            try:
                session = re.findall('session-(.*?)-',proxy)[0]
                url = f'http://apiproxy.grassdata.net/changeAccountSession?sn=b67fe49b34bdda53c8a484f801679bfb&account=PsFaJMphAU0hH1s20E&session={session}'
                resp = requests.get(url,timeout=5)
                if resp.json()['code'] == 0:
                    # API 声称成功，验证 IP 是否真的变化了
                    self._proxy_ip_cache = ''
                    self._proxy_ip_cache_ts = 0.0
                    try:
                        new_ip = self._resolve_proxy_ip()
                    except Exception:
                        new_ip = ''
                    if new_ip and new_ip != old_ip:
                        print(f'代理更新成功，IP: {old_ip} → {new_ip}')
                        return None
                    else:
                        print(f'代理切换返回成功但 IP 未变化（old={old_ip}, new={new_ip}），启用备用方案')
                        break
            except Exception as e:
                print(f'代理更新失败:{str(e)}')
                pass
        proxy = f'http://PsFaJMphAU0hH1s20E-zone-custom-region-{PROXY_MAPPING[self.task["country"].upper()]}-session-{int(time.time() * 1000)}-sessTime-15:iWrz7GbWhm@a477c1a8e06d7ff8.qzc.na.grassdata.net:2333'
        print(f'代理更新失败自动生成新代理：{proxy}')
        return proxy

    def _resolve_proxy_ip(self) -> str:
        """
        通过当前代理查询真实出口 IP（http://ip234.in/ip.json）。
        结果缓存 12 分钟，代理刷新后自动失效。
        失败时返回上次缓存值（或空字符串），不影响主流程。
        """
        now = time.time()
        if self._proxy_ip_cache and now - self._proxy_ip_cache_ts < 60:
            return self._proxy_ip_cache
        try:
            resp = self.curl_session.get(
                'http://ip234.in/ip.json',
                proxies=self.proxies,
                timeout=5,
                impersonate="chrome120",
                verify=False,
            )
            ip = resp.json().get('ip', '')
            if ip:
                self._proxy_ip_cache = ip
                self._proxy_ip_cache_ts = now
                self._log.info(f"[代理IP] 当前出口IP={ip}")
            return self._proxy_ip_cache
        except Exception:
            return self._proxy_ip_cache

    def refresh_x_main(self):
        #刷新cookies中的 x_main
        pass

    def request(self, method, url, **kwargs):
        """
        封装统一请求方法，自动检测反爬并切换session
        :param method: 'get' 或 'post'（字符串，不再传方法对象）
        :param url: 请求URL
        :param kwargs: 请求参数
        :return: 响应对象
        """
        proxy_error_count = 0
        cookie_refresh_count = 0
        captcha_count = 0
        count = MAX_REQUEST

        # 设置默认参数
        if 'timeout' not in kwargs:
            kwargs['timeout'] = 20
        if 'verify' not in kwargs:
            kwargs['verify'] = False
        if self.use_curl_session and 'impersonate' not in kwargs:
            kwargs['impersonate'] = "chrome120"
        # 优先使用当前标记的session
        current_session = self.curl_session if self.use_curl_session else self.session

        while count > 0:
            try:
                count -= 1
                _t0 = time.time()

                # 根据method字符串调用对应方法
                if method.lower() == 'get':
                    response = current_session.get(url, **kwargs)
                elif method.lower() == 'post':
                    response = current_session.post(url, **kwargs)
                else:
                    raise ValueError(f"不支持的请求方法: {method}")

                _latency_ms = (time.time() - _t0) * 1000
                _account_id = getattr(self.account_info, 'username', '') if self.account_info else ''
                _proxy_ip = ''
                try:
                    _proxy_ip = self._resolve_proxy_ip()
                    if _proxy_ip:
                        self._log = self._log.bind(ip=_proxy_ip)
                except Exception:
                    pass
                _site = (self.task or {}).get('country', '')
                _is_blocked = False

                # 检测TSL反爬标识
                if 'Click the button below to continue shopping' in response.text:
                    _is_blocked = True
                    try:
                        get_reporter().request.report(
                            account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                            status_code=response.status_code, latency_ms=_latency_ms,
                            is_blocked=True, url_path=url,
                        )
                    except Exception:
                        pass
                    if not self.use_curl_session:
                        self._log.warning(f"[反爬] 检测到TLS反爬，切换curl session: {url}")
                        self.sync_cookies_between_sessions()  # 同步cookie
                        self.use_curl_session = True
                    else:
                        self._log.warning(f"[反爬] TLS反爬+已是curl session，刷新代理")
                        proxy = self.refresh_proxy(kwargs['proxies']['http'])
                        if proxy:
                            kwargs['proxies'] = {"http": proxy, "https": proxy}
                            self.proxies = kwargs['proxies']
                        _proxy_ip = self._resolve_proxy_ip()
                        if _proxy_ip:
                            self._log = self._log.bind(ip=_proxy_ip)
                    raise TLSAntiCrawlException("需要切换到curl session")

                # # 检测验证码（CAPTCHA）— 代理IP被拦截，与cookie无关，只刷代理重试
                # if response.text and (
                #         '/errors/validateCaptcha' in response.text or
                #         'opfcaptcha.amazon.com' in response.text
                # ):
                #     _is_blocked = True
                #     captcha_count += 1
                #     self._log.warning(f"[验证码] 检测到CAPTCHA(第{captcha_count}次)，刷新代理重试: {url}")
                #     try:
                #         proxy = self.refresh_proxy(kwargs.get('proxies', {}).get('http', ''))
                #         if proxy:
                #             kwargs['proxies'] = {"http": proxy, "https": proxy}
                #             self.proxies = kwargs['proxies']
                #             _proxy_ip = self._resolve_proxy_ip()
                #             if _proxy_ip:
                #                 self._log = self._log.bind(ip=_proxy_ip)
                #     except Exception:
                #         pass
                #
                #     time.sleep(random.uniform(5, 10))
                #     continue  # 不计入cookie_refresh_count，不调用get_info_from_dp

                # 检测cookie过期 / 登录跳转
                if response.text and (
                        'Please Enable Cookies to Continue' in response.text or
                        'Sign in or create' in response.text or
                        'auth-signin-button' in response.text or 'form name="signIn"' in response.text or
                        'Click the button below to continue shopping' in response.text or
                        'Continuar a Compras' in response.text or
                        '/errors/validateCaptcha' in response.text or
                        'opfcaptcha.amazon.com' in response.text
                ):
                    if response.text and (
                            '/errors/validateCaptcha' in response.text or
                            'opfcaptcha.amazon.com' in response.text
                    ):

                        captcha_count += 1
                        self._log.warning(f"[验证码] 检测到CAPTCHA(第{captcha_count}次)，刷新代理重试: {url}")
                        if captcha_count >= 3:
                            raise AccountSwitchRequiredException(
                                f"ACCOUNT_SWITCH_REQUIRED: CAPTCHA连续{captcha_count}次，切换账号重试, url={url}"
                            )
                    else:
                        self._log.warning(f"[Cookie] Cookie过期，尝试重新登录: {url}")

                    _is_blocked = True
                    # 发射 LOGIN_REDIRECT 事件
                    try:
                        _rc = getattr(self, '_redis_client', None)
                        if _rc:
                            push_event(
                                _rc, EventType.LOGIN_REDIRECT,
                                username=_account_id,
                                country=_site,
                                proxy=_proxy_ip,
                                http_status=response.status_code,
                            )
                    except Exception:
                        pass
                    cookie_refresh_count += 1
                    if cookie_refresh_count > 2:
                        raise CookieRefreshExhaustedException(
                            f"COOKIE_REFRESH_EXHAUSTED: cookie刷新超过2次仍失效, url={url}"
                        )
                    try:
                        get_reporter().request.report(
                            account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                            status_code=response.status_code, latency_ms=_latency_ms,
                            is_blocked=True, url_path=url,
                        )
                    except Exception:
                        pass
                    if 'Click the button below to continue shopping' in response.text:
                        proxy = self.refresh_proxy(kwargs['proxies']['http'])
                        if proxy:
                            kwargs['proxies'] = {"http": proxy, "https": proxy}
                            self.proxies = kwargs['proxies']
                        _proxy_ip = self._resolve_proxy_ip()
                        if _proxy_ip:
                            self._log = self._log.bind(ip=_proxy_ip)
                        time.sleep(5)
                    try:
                        if self.use_local_browser:
                            self.get_info_from_local_dp(self.first_page_url)
                        else:
                            self.get_info_from_dp(self.first_page_url)
                        continue
                    except Exception as e:
                        if '账号需要手机号验证' in str(e):
                            raise
                        self._log.error(f"[登录] Cookie刷新失败: {e}")
                        if cookie_refresh_count > 2:
                            raise CookieRefreshExhaustedException(
                                f"COOKIE_REFRESH_EXHAUSTED: cookie刷新失败超过2次, url={url}, err={e}"
                            )
                        continue

                # 429/503 限速处理：退避重试，不刷新 Cookie/代理
                if response.status_code in (429, 503):
                    _is_blocked = True
                    try:
                        get_reporter().request.report(
                            account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                            status_code=response.status_code, latency_ms=_latency_ms,
                            is_blocked=True, url_path=url,
                        )
                    except Exception:
                        pass
                    _backoff = min(2 ** (MAX_REQUEST - count), 60) + random.uniform(0, 3)
                    self._log.warning(
                        f"[限速] HTTP {response.status_code}, {_backoff:.1f}s 后重试: {url}")
                    time.sleep(_backoff)
                    continue

                # 有效响应判断
                if (response.status_code == 200 and response.text) or response.status_code in [302, 404]:
                    try:
                        get_reporter().request.report(
                            account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                            status_code=response.status_code, latency_ms=_latency_ms,
                            is_blocked=False, url_path=url,
                        )
                    except Exception:
                        pass
                    return response

                else:
                    # 其他异常状态码上报
                    try:
                        get_reporter().request.report(
                            account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                            status_code=response.status_code, latency_ms=_latency_ms,
                            is_blocked=response.status_code >= 400, url_path=url,
                        )
                    except Exception:
                        pass
                    self.get_info_from_dp(self.first_page_url)
                    self._log.warning(f"[请求] 响应异常，状态码={response.status_code}, url={url}")

            except TLSAntiCrawlException:
                # 捕获反爬异常，重试时会使用curl session
                continue
            except CookieRefreshExhaustedException:
                raise
            except AccountSwitchRequiredException:
                raise
            except Exception as e:
                error_msg = str(e)
                # 上报请求异常到 InfluxDB（网络/代理错误）
                try:
                    get_reporter().request.report(
                        account_id=_account_id, proxy_ip=_proxy_ip, site=_site,
                        status_code=0, latency_ms=0,
                        is_blocked=True, url_path=url[:80] if url else '',
                    )
                except Exception:
                    pass
                # 代理异常处理
                if any(err in error_msg for err in
                       ['(35) TLS connect error','Connection aborted', 'curl: (56)', 'curl: (28)','curl: (35)', 'ProxyError', 'HTTPSConnectionPool']):
                    proxy_error_count += 1

                    if proxy_error_count > 3:
                        self._log.error(f"[代理] 请求连接连续失败{proxy_error_count}次，切换账号重试: {url}")
                        raise AccountSwitchRequiredException(
                            f"ACCOUNT_SWITCH_REQUIRED: proxy_error_count={proxy_error_count}, url={url}, err={error_msg}"
                        )

                    if proxy_error_count >= 2:
                        self._log.warning(f"[代理] 代理连续失败，刷新代理（失败次数={proxy_error_count}）")
                        proxy = self.refresh_proxy(kwargs['proxies']['http'])
                        if proxy:
                            kwargs['proxies'] = {"http": proxy, "https": proxy}
                            self.proxies = kwargs['proxies']
                        _proxy_ip = self._resolve_proxy_ip()
                        if _proxy_ip:
                            self._log = self._log.bind(ip=_proxy_ip)
                self._log.error(f"[请求] 失败，剩余重试={count}, 错误={error_msg}, url={url}")
                time.sleep(2)

        # 重试耗尽，发送告警
        if proxy_error_count > 3:
            self._log.error(f"[代理] 请求连接连续失败{proxy_error_count}次，切换账号重试: {url}")
            raise AccountSwitchRequiredException(
                f"ACCOUNT_SWITCH_REQUIRED: proxy_error_count={proxy_error_count}, url={url}"
            )

        raise ValueError(f"{MAX_REQUEST}次请求失败: {url}")
    # 纯字典注入（无 Cookie 类，无参数报错）
    def inject_cookies_to_session(self, cookie_dict, domain):
        self.session.cookies.clear()  # 清空原有Cookie
        # 遍历字典，用 set 方法直接添加（自动处理参数）
        for name, value in cookie_dict.items():
            self.session.cookies.set(
                name=name,
                value=value,
                domain=domain,
                path="/",
                secure=True
            )

        self.curl_session.cookies.clear()  # 清空原有Cookie
        # 遍历字典，用 set 方法直接添加（自动处理参数）
        for name, value in cookie_dict.items():
            self.curl_session.cookies.set(
                name=name,
                value=value,
                domain=domain,
                path="/",
                secure=True
            )

    def sync_cookies_between_sessions(self):
        """双向同步两个session的cookie，确保上下文一致"""
        # 从当前活跃的session同步cookie到另一个session
        # requests session -> curl session
        requests_cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
        self.inject_cookies_to_session(requests_cookies, domain=SITE_MAPPING[self.task["country"].upper()].replace('www',''))

    def get_cookies(self):
        if f'https://{SITE_MAPPING[self.task["country"].upper()]}/' not in self.page.url:
            raise Exception('页面加载失败')
        if not self.cookies:
            is_login = False
            cookie_dict = {}
            for cookie in self.page.context.cookies():
                if 'ubid-' in cookie['name']:
                    is_login = True
                cookie_dict[cookie['name']] = cookie['value']
            if not is_login:
                self.proxies = self.get_proxies(self.account_info.fingerprint_id)
                self.refresh_proxy(self.proxies['http'])
                raise Exception('cookies中没有ubid，登录失败，刷新代理后重试')
            self.cookies = cookie_dict
            self.inject_cookies_to_session(cookie_dict, domain=SITE_MAPPING[self.task["country"].upper()].replace('www', ''))

            resp = self.get_proxies(self.account_info.fingerprint_id, return_full_resp=True)
            fp_data = (resp.get('data') or {}).get('browserFingerPrint') or {}
            self.user_agent = {
                'userAgent': fp_data.get('userAgent') or self.page.evaluate("navigator.userAgent"),
                'os': fp_data.get('os', 'Win32'),
                'deviceMemory': fp_data.get('deviceMemory', 8),
                'devicePixelRatio': fp_data.get('devicePixelRatio', 2),
            }
            self.proxies = self.get_proxies(self.account_info.fingerprint_id, full_resp=resp or None)
            self.account_info.cookies = cookie_dict
            self.account_info.user_agent = json.dumps(self.user_agent)
            self.account_info.proxy_ = self.proxies


    def get_proxies(self, code, resp=None, full_resp=None, return_full_resp=False):
        if full_resp:
            resp = full_resp
        if not resp:
            try:
                resp = http_requests.post(
                    f"{BIT_API_BASE}/browser/detail",
                    data={"id": code},
                    timeout=10,
                ).json()
            except Exception as e:
                logger.warning(f"get_browser_detail 失败: {e}")
                return {} if return_full_resp else {}
        if return_full_resp:
            return resp or {}
        detail = (resp or {}).get('data') or {}
        if detail.get("proxyUserName"):
            proxy = f'http://{detail["proxyUserName"]}:{detail["proxyPassword"]}@{detail["host"]}:{detail["port"]}'
            logger.info(f'当前使用代理：{proxy}')
            return {'http': proxy, 'https': proxy}
        return {}

    def clean_totp_secret(self,secret: str) -> str:
        """清理 TOTP 密钥，移除空格和特殊字符"""
        return secret.strip().replace(" ", "").replace("-", "")



    def verify_login(self, url):
        # 处理验证码表单
        try:
            captcha_form = self.page.query_selector('[action="/errors/validateCaptcha"]')
            if captcha_form:
                submit_btn = captcha_form.query_selector('[type="submit"]')
                if submit_btn:
                    submit_btn.click()
                time.sleep(5)
        except Exception:
            pass

        if 'signin' in self.page.url:
            try:
                self.amazon_login(self.page, self.account_info.username, self.account_info.password,
                                  self.account_info.totp_secret)
            except Exception:
                raise Exception(f'登录失败:{self.account_info.username}--{traceback.format_exc()}')

    def _disable_account(self, username: str, reason: str = ''):
        """将账号 state 设为 0（停用），加入 Redis 封禁集合，等待人工处理。"""
        logger.warning(f'[账号停用] {username} reason={reason}')
        try:
            from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
            db = MySQLTaskDB()
            db.update_account({'username': username, 'state': 0})
            db.close()
        except Exception:
            logger.error(f'停用账号写 MySQL 失败: {traceback.format_exc()}')
        # 同步内存对象，防止后续 _end_session → _save_account 用 state=1 覆盖回去
        if self.account_info and self.account_info.username == username:
            self.account_info.state = 0
        try:
            _rc = getattr(self, '_redis_client', None)
            if _rc is None:
                import redis as redis_lib
                from app.crawlers.amazon_crawler.shuler.util.config import (
                    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
                )
                _rc = redis_lib.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    username=REDIS_USERNAME, password=REDIS_PASSWORD,
                    db=REDIS_DB, decode_responses=True,
                    socket_connect_timeout=3, socket_timeout=3,
                )
                self._redis_client = _rc
            _rc.set(f'crawler:banned:{username}', '1', ex=86400)
        except Exception:
            logger.error(f'停用账号写 Redis 失败: {traceback.format_exc()}')

    def amazon_login(self, page, email: str, password: str, totp_secret: Optional[str] = None):
        logger.info('开始登录')

        # 登录核心步骤（委托给统一工具模块，与 reviews_playwright.py 共用同一实现）
        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import playwright_amazon_login
        try:
            playwright_amazon_login(
                page, email, password, totp_secret,
                on_disable_account=self._disable_account,
            )
        except Exception:
            # 完全登录失败（仍停留在 signin 页）时上报到 InfluxDB
            if 'signin' in page.url:
                try:
                    rpt = get_reporter()
                    if rpt:
                        _username = getattr(self.account_info, 'username', '') if self.account_info else ''
                        _country = (self.task or {}).get('country', '')
                        rpt.account.report_ban(account_id=_username, site=_country, reason="login_failed")
                except Exception:
                    pass
            raise

        # 登录后跳回评论页（以此确认真正登录成功）
        if 'product-reviews' not in page.url:
            asin = (self.task or {}).get('asin', '')
            country = (self.task or {}).get('country', '').upper()
            site = SITE_MAPPING.get(country, '')
            if asin and site:
                review_url = f'https://{site}/product-reviews/{asin}'
                page.goto(review_url, timeout=12000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        '#nav-link-accountList, #cm_cr-review_list, [data-hook="review"], '
                        'form[name="signIn"], [action="/errors/validateCaptcha"]',
                        timeout=5000,
                    )
                except Exception:
                    pass

        if 'product-reviews' not in page.url:
            raise Exception(f'登录后无法访问评论页，当前页面: {page.url}')


        logger.info('登录成功')
        try:
            rpt = get_reporter()
            if rpt:
                _username = getattr(self.account_info, 'username', '') if self.account_info else ''
                _country = (self.task or {}).get('country', '')
                rpt.account.report_status(account_id=_username, site=_country, status="login_success")

        except Exception:
            pass
        try:
            asin = (self.task or {}).get('asin', '')
            country = (self.task or {}).get('country', '').upper()
            site = SITE_MAPPING.get(country, '')
            if asin and site:
                review_url = f'https://{site}/dp/{asin}'  # 访问商品页
                page.goto(review_url, timeout=12000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        '#title_feature_div, #averageCustomerReviews, form[name="signIn"], '
                        '[action="/errors/validateCaptcha"]',
                        timeout=5000,
                    )
                except Exception:
                    pass
        except:
            pass

    # curl_session CookieJar 适配内置方法的示例
    def curl_cookie_to_requests_dict(self,curl_session):
        # 转换为requests兼容的CookieJar
        req_cookiejar = requests.cookies.RequestsCookieJar()
        if not curl_session:
            return {}

        cookies_source = getattr(curl_session, 'cookies', curl_session)

        # dict-like: {'name': 'value'} / RequestsCookieJar.items()
        if hasattr(cookies_source, 'items'):
            try:
                for name, value in cookies_source.items():
                    req_cookiejar.set(str(name), str(value))
                return requests.utils.dict_from_cookiejar(req_cookiejar)
            except Exception:
                pass

        # iterable: [CookieObj] / [('name', 'value')] / ['name', ...]
        for item in cookies_source:
            if hasattr(item, 'name') and hasattr(item, 'value'):
                req_cookiejar.set(
                    item.name,
                    item.value,
                    domain=getattr(item, 'domain', None),
                    path=getattr(item, 'path', '/'),
                )
                continue

            if isinstance(item, tuple) and len(item) >= 2:
                req_cookiejar.set(str(item[0]), str(item[1]))
                continue

            if isinstance(item, str):
                value = None
                if hasattr(cookies_source, 'get'):
                    value = cookies_source.get(item)
                if value is not None:
                    req_cookiejar.set(item, str(value))

        # 使用内置方法转换（扁平字典，无同名处理）
        return requests.utils.dict_from_cookiejar(req_cookiejar)

    import json
    import re

    def generate_sec_ch_headers(self):
        """
        根据 User-Agent 动态生成 sec-ch-ua 和 sec-ch-ua-full-version-list
        """
        # 1. 使用正则从 UA 中提取主版本号 (例如: Chrome/139.0.7258.155 -> 139)
        data = self.user_agent

        # 如果 user_agent 为 None，返回默认 headers
        if data is None:
            logger.warning("[generate_sec_ch_headers] user_agent 为 None，使用默认 headers")
            return {
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "device-memory": "8",
                "downlink": "1.45",
                "dpr": "2",
                "ect": "3g",
                "rtt": "500",
                "sec-ch-device-memory": "8",
                "sec-ch-dpr": "2",
                "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="139", "Google Chrome";v="139"',
                "sec-ch-ua-full-version-list": '"Not(A:Brand";v="8.0.0.0", "Chromium";v="139.0.0.0", "Google Chrome";v="139.0.0.0"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-viewport-width": "1399",
                "viewport-width": "1399"
            }

        match = re.search(r"Chrome/(\d+)\.", data.get('userAgent', ''))
        if not match:
            # 如果解析失败，使用一个默认的安全值
            major_version = "139"
        else:
            major_version = match.group(1)

        # 2. 构建 sec-ch-ua (简略版)
        # 格式: "Not(A:Brand";v="8", "Chromium";v="{主版本}", "Google Chrome";v="{主版本}"
        sec_ch_ua = f'"Not(A:Brand";v="8", "Chromium";v="{major_version}", "Google Chrome";v="{major_version}"'

        # 3. 构建 sec-ch-ua-full-version-list (完整版)
        # 我们需要从 UA 中提取完整的版本号 (例如: 139.0.7258.155)
        full_version_match = re.search(r"Chrome/([\d.]+)", data['userAgent'])
        full_version = full_version_match.group(1) if full_version_match else "139.0.0.0"

        sec_ch_ua_full = f'"Not(A:Brand";v="8.0.0.0", "Chromium";v="{full_version}", "Google Chrome";v="{full_version}"'
        platform_code = data['os']
        if platform_code == "Win32":
            target_platform = '"Windows"'
        elif platform_code == "MacIntel":
            target_platform = '"macOS"'
        elif platform_code.startswith("Linux"):  # 兼容 "Linux x86_64" 或 "Linux armv81"
            target_platform = '"Linux"'
        elif platform_code == "iPhone":
            target_platform = '"iOS"'
        elif platform_code == "Android":  # 或者是 "Linux armv81" 这种安卓底层标识
            target_platform = '"Android"'
        # 3. 组装最终的 headers
        headers = {
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": data['userAgent'],

            # 从 JSON 动态读取数值
            "device-memory": str(data.get("deviceMemory", 8)),
            "downlink": "1.45",  # 这个通常不在指纹配置里，保持你原来的或者随机
            "dpr": str(data.get("devicePixelRatio", 2)),
            "ect": "3g",
            "rtt": "500",
            "sec-ch-device-memory": str(data.get("deviceMemory", 8)),
            "sec-ch-dpr": str(data.get("devicePixelRatio", 2)),

            # 动态生成的字段
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-full-version-list": sec_ch_ua_full,

            # 其他固定或动态字段
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": target_platform,
            "sec-ch-viewport-width": "1399",
            "viewport-width": "1399"
        }
        return headers
