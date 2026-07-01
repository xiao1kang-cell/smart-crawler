"""
基于 Playwright 的亚马逊评论抓取器
通过浏览器请求监听（response interception）方式获取评论数据，
避免手工构造 AJAX 请求头和 CSRF token。

用法：
    task = {
        'asin': 'B0DPZK6K3C',
        'country': 'US',
        'id': '111111',
        'query_conditions': {'stars': [1, 2, 3, 4, 5]}
    }
    scraper = PlaywrightReviewScraper(account_info=None, task=task)
    reviews = scraper.run(task)
"""
# 禁用 Node.js DeprecationWarning 告警
import os
os.environ['NODE_OPTIONS'] = '--no-warnings'

import copy
import json
import math
import os
import re
import time
import random
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import requests as http_requests
from bs4 import BeautifulSoup
from loguru import logger
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_base import (
    _start_playwright_in_clean_thread,
    CDP_CONNECT_ATTEMPTS,
    CDP_CONNECT_RETRY_SLEEP_SECONDS,
    CDP_CONNECT_TIMEOUT_SECONDS,
    CDPConnectException,
    NetworkException,
)

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import (
    SITE_MAPPING, COUNTRY_MAPPING, PROXY_MAPPING,
    RETRY_TIMES, REFRESH_TIME, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
)
from app.crawlers.amazon_crawler.shuler.services.amazon.review_parser_utils import (
    add_current_format_param,
    add_recent_sort_param,
    alert_review_parse_error,
    parse_review_block_html,
    parse_review_date,
    should_use_current_format_filter,
    should_use_recent_sort,
)
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager as AccountManager
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
from app.crawlers.amazon_crawler.shuler.util.event_logger import push_event, EventType
from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import (
    increment_account_error, reset_account_error,
    BanReason,
    increment_network_error, get_network_error_count,
    should_alert_network_error, mark_network_alert_sent,
    NETWORK_ERR_MULTI_THRESHOLD, NETWORK_ERR_ALERT_THRESHOLD,
)
from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
from app.crawlers.amazon_crawler.shuler.util.config import BIT_BROWSER_APP_PATH

# BitBrowser API 地址
BIT_BROWSER_IP = os.getenv('BIT_BROWSER_IP', '127.0.0.1')
BIT_API_BASE = f"http://{BIT_BROWSER_IP}:54345"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _ensure_bit_browser_running(wait_seconds: int = 20) -> None:
    """
    检测比特浏览器 API 是否可达，不可达时按平台自动启动：
      - macOS：open /Applications/比特浏览器.app
      - Windows：直接执行 .exe
    路径从 BIT_BROWSER_APP_PATH 配置读取，未配置则跳过。
    仅在 BIT_BROWSER_IP 指向本机时调用。
    """
    import subprocess

    # 快速探测：已经在跑就直接返回
    try:
        http_requests.get(f"http://127.0.0.1:54345/browser/list", timeout=2)
        return
    except Exception:
        pass

    app_path = BIT_BROWSER_APP_PATH
    if not app_path:
        logger.warning("BIT_BROWSER_APP_PATH 未配置，跳过自动启动比特浏览器")
        return
    if not os.path.exists(app_path):
        logger.warning(f"比特浏览器路径不存在: {app_path}，跳过自动启动")
        return

    logger.info(f"比特浏览器未运行，正在自动启动: {app_path}")
    try:
        if app_path.endswith(".app"):
            # macOS
            subprocess.Popen(["open", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Windows .exe
            subprocess.Popen([app_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning(f"启动比特浏览器失败: {e}")
        return

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        try:
            http_requests.get(f"http://127.0.0.1:54345/browser/list", timeout=2)
            # logger.info("比特浏览器 API 已就绪")
            return
        except Exception:
            pass

    logger.warning(f"比特浏览器在 {wait_seconds}s 内未就绪，继续尝试连接")


class PlaywrightReviewScraper:
    """
    Playwright 版评论抓取器。
    核心思路：
      1. 启动 Playwright 浏览器（带代理 + cookies）
      2. 导航到评论页，监听所有 response
      3. 拦截匹配 review AJAX 的响应，解析评论数据
      4. 自动翻页（点击下一页 / show-more 按钮）
      5. 收集完毕后关闭浏览器，返回评论列表
    """

    # 需要拦截的 URL 关键词
    INTERCEPT_KEYWORDS = [
        '/portal/customer-reviews/ajax/reviews/get/',
        '/product-reviews/',
    ]

    def __init__(self, account_info=None, task=None, proxy: dict = None,
                 use_fingerprint: bool = True):
        """
        :param account_info: 账号对象（含 cookies / user_agent / proxy_ / fingerprint_id 等属性）
        :param task: 任务字典 {'asin', 'country', 'id', 'query_conditions', ...}
        :param proxy: 手动指定代理 {"host": ..., "port": ..., "user": ..., "password": ...}
        :param use_fingerprint: True=接管BitBrowser指纹浏览器(CDP), False=独立启动Playwright
        """
        self.account_info = account_info
        self.task = task
        self.proxy = proxy
        self.use_fingerprint = use_fingerprint

        # Playwright 相关
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self._is_cdp = False  # 标记是否通过 CDP 连接（关闭时不 close browser）

        # 数据收集
        self._captured_responses: List[dict] = []  # 存放拦截到的原始响应
        self._all_reviews: List[dict] = []
        self._seen_review_ids: Set[str] = set()
        self._counted_review_page_keys: Set[str] = set()
        self._raw_review_slots_count = 0
        self._duplicate_review_slots_count = 0

        # 状态
        self.review_counts = 0
        self.pages = 0
        self.reviews_csrf_token = ''
        self.worker_id = None
        self.user_agent = None
        self.fingerprint_id = None  # BitBrowser 窗口 ID
        self._task_count = 0  # 已执行任务计数，用于定期访问主页
        self._retry_after_seconds: Optional[int] = None  # 429 Retry-After 秒数

        # 当前预期的筛选状态（用于校验拦截到的请求参数）
        self._expected_sort = 'recent'
        self._expected_star_filter = ''      # '' 表示不筛选
        self._expected_is_variant = False
        self._active_review_asin = task.get('asin', '') if task else ''

        # 绑定了 asin/account/country 上下文的 logger，会被 InfluxDB sink 采集
        # run() 里会用正式值覆盖；此处给出默认值避免方法在 run() 外调用时 AttributeError
        self._log = logger

        # 如果没有 account_info，自动获取
        if self.task and not self.account_info:
            account_manager = AccountManager()
            self.account_info = account_manager.get_account({"country": task['country']})

        if self.account_info:
            self.fingerprint_id = getattr(self.account_info, 'fingerprint_id', None)

    # ========================== 浏览器生命周期 ==========================

    def _build_proxy_config(self) -> Optional[dict]:
        """构建 Playwright 代理配置"""
        proxy_info = self.proxy or getattr(self.account_info, 'proxy_', None)
        if not proxy_info:
            return None

        if isinstance(proxy_info, dict):
            # 如果是 {"http": "http://...", "https": "..."} 格式
            if 'http' in proxy_info:
                url = proxy_info['http']
                return {"server": url}
            # 如果是 {"host": ..., "port": ..., "user": ..., "password": ...} 格式
            if 'host' in proxy_info:
                server = f"http://{proxy_info['host']}:{proxy_info['port']}"
                result = {"server": server}
                if proxy_info.get('user'):
                    result["username"] = proxy_info['user']
                if proxy_info.get('password'):
                    result["password"] = proxy_info['password']
                return result
        return None

    def _start_browser(self):
        """启动浏览器：优先接管指纹浏览器，否则独立启动"""
        if self.use_fingerprint and self.fingerprint_id:
            self._start_browser_cdp()
        else:
            self._start_browser_standalone()

    def _start_browser_cdp(self):
        """
        通过 CDP 接管 BitBrowser 指纹浏览器，带重试机制。
        等价于原 DrissionPage 的 init_dp() 流程：
          1. POST /browser/open 启动指纹窗口（带重试）
          2. 拿到 debugging_port
          3. Playwright connect_over_cdp 接管（短超时、失败后重开窗口）
        """
        if self._playwright is not None or self._browser is not None:
            self._close_browser()

        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import open_browser, ProfileNotFoundError
        last_error = None
        for attempt in range(CDP_CONNECT_ATTEMPTS):
            cdp_endpoint = ''
            try:
                # 在干净线程中启动，绕过 curl_cffi 在 Windows 遗留 running asyncio loop 的问题
                self._playwright = _start_playwright_in_clean_thread()
                http_addr = open_browser(self.fingerprint_id)
                cdp_endpoint = f"http://{http_addr}"
                timeout_ms = CDP_CONNECT_TIMEOUT_SECONDS * 1000
                logger.info(
                    f"CDP 连接尝试 profile={self.fingerprint_id} endpoint={cdp_endpoint} "
                    f"attempt={attempt + 1}/{CDP_CONNECT_ATTEMPTS} timeout={CDP_CONNECT_TIMEOUT_SECONDS}s"
                )
                self._browser = self._playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=timeout_ms
                )
                self._is_cdp = True
                logger.info("CDP 连接成功")
                break
            except ProfileNotFoundError:
                username = getattr(self.account_info, 'username', 'unknown')
                send_custom_robot_group_message(
                    f'[指纹浏览器] 账号 {username} 浏览器配置不存在，已自动停用，请检查 BitBrowser profile 配置',
                    at_mobiles=['17398238551']
                )
                # 停用账号（写 MySQL + Redis），让调度器不再复用此账号
                try:
                    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
                    db = MySQLTaskDB()
                    db.update_account({'username': username, 'state': 0})
                    db.close()
                except Exception:
                    pass
                self._close_browser()
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    f"CDP 连接失败 profile={self.fingerprint_id} endpoint={cdp_endpoint or '-'} "
                    f"attempt={attempt + 1}/{CDP_CONNECT_ATTEMPTS} "
                    f"timeout={CDP_CONNECT_TIMEOUT_SECONDS}s: {e}"
                )
                self._close_browser()
                self._quit_fingerprint_browser()
                if attempt < CDP_CONNECT_ATTEMPTS - 1:
                    time.sleep(CDP_CONNECT_RETRY_SLEEP_SECONDS)
        else:
            raise CDPConnectException(
                f"CDP 接管失败 profile={self.fingerprint_id}, attempts={CDP_CONNECT_ATTEMPTS}, "
                f"timeout={CDP_CONNECT_TIMEOUT_SECONDS}s: {last_error}"
            )

        # 获取已有的 context 和 page（指纹浏览器已经有一个打开的页面）
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
            pages = self._context.pages
            if pages:
                self.page = pages[0]
            else:
                self.page = self._context.new_page()
        else:
            self._context = self._browser.new_context()
            self.page = self._context.new_page()

        try:
            self._context.set_default_timeout(15000)
            self._context.set_default_navigation_timeout(30000)
            self.page.set_default_timeout(15000)
            self.page.set_default_navigation_timeout(30000)
        except Exception as e:
            logger.warning(f"[浏览器初始化] 设置 Playwright 默认超时失败（不影响主流程）: {e}")

        # 获取 UA
        self.user_agent = self.page.evaluate("navigator.userAgent")

        # 注入 cookies（如果 account_info 有缓存的 cookies）
        # self._inject_cookies()

        # 注册响应监听
        self.page.on("response", self._on_response)

        logger.info(f"Playwright 已通过 CDP 接管指纹浏览器: {self.fingerprint_id}, UA: {self.user_agent[:60]}...")

    def _start_browser_standalone(self):
        """独立启动 Playwright 浏览器（不依赖指纹浏览器）"""
        self._playwright = _start_playwright_in_clean_thread()

        proxy_config = self._build_proxy_config()
        launch_args = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        if proxy_config:
            launch_args["proxy"] = proxy_config

        self._browser = self._playwright.chromium.launch(**launch_args)
        self._is_cdp = False

        ua = getattr(self.account_info, 'user_agent', None) or \
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

        context_args = {
            "user_agent": ua,
            "viewport": {"width": 1399, "height": 900},
            "ignore_https_errors": True,
        }
        self._context = self._browser.new_context(**context_args)

        self._inject_cookies()
        self.page = self._context.new_page()
        self.user_agent = ua

        # 注册响应监听
        self.page.on("response", self._on_response)

        logger.info("Playwright 独立浏览器已启动")

    def _inject_cookies(self):
        """将 account_info 中的 cookies 注入 Playwright context"""
        cookies_dict = getattr(self.account_info, 'cookies', None) if self.account_info else None
        if not cookies_dict or not isinstance(cookies_dict, dict):
            return

        country = self.task['country'].upper() if self.task else 'US'
        domain = SITE_MAPPING.get(country, 'www.amazon.com')

        pw_cookies = []
        for name, value in cookies_dict.items():
            pw_cookies.append({
                "name": name,
                "value": str(value),
                "domain": f".{domain.replace('www.', '')}",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            })
        if pw_cookies:
            self._context.add_cookies(pw_cookies)
            logger.info(f"已注入 {len(pw_cookies)} 个 cookies")

    def _is_browser_alive(self) -> bool:
        """
        检测浏览器是否仍然存活（未被人为关闭）。
        只检查 Playwright 本地连接状态，避免 CDP 半断开时 evaluate 长时间挂住。
        """
        if not self.page or not self._browser:
            return False
        try:
            if hasattr(self._browser, "is_connected") and not self._browser.is_connected():
                return False
            if hasattr(self.page, "is_closed") and self.page.is_closed():
                return False
            return True
        except Exception:
            logger.warning(f"[浏览器检测] 浏览器连接已断开 (账号={getattr(self.account_info, 'username', '')})")
            return False

    def _get_proxy_ip(self) -> str:
        """提取代理真实 IP/host（优先取 http/https 代理 URL 中 @ 后的 host）"""
        try:
            proxy_info = getattr(self.account_info, 'proxy_', None)
            if not proxy_info:
                return ''

            def _parse_proxy_host(proxy_value) -> str:
                if not proxy_value:
                    return ''
                raw = str(proxy_value).strip()
                if not raw:
                    return ''

                candidate = raw.rsplit('@', 1)[-1]
                candidate = candidate.split('://', 1)[-1].split('/', 1)[0]
                candidate = candidate.split(':', 1)[0].strip()
                if candidate:
                    return candidate

                return raw[:60]

            if isinstance(proxy_info, dict):
                proxy_url = proxy_info.get('http') or proxy_info.get('https')
                if proxy_url:
                    return _parse_proxy_host(proxy_url)
                if proxy_info.get('host'):
                    return str(proxy_info.get('host'))
                return _parse_proxy_host(proxy_info)

            return _parse_proxy_host(proxy_info)
        except Exception:
            return ''

    def _report_request(self, status_code: int, latency_ms: float,
                        is_blocked: bool, url_path: str = "") -> None:
        """上报 HTTP 请求指标到 InfluxDB crawler_request"""
        try:
            rpt = get_reporter()
            if not rpt:
                return
            username = getattr(self.account_info, 'username', '') if self.account_info else ''
            country = self.task.get('country', '') if self.task else ''
            rpt.request.report(
                account_id=username,
                proxy_ip=self._get_proxy_ip(),
                site=country,
                status_code=status_code,
                latency_ms=float(latency_ms),
                is_blocked=is_blocked,
                url_path=url_path[:80] if url_path else '',
            )
        except Exception:
            pass

    def _disable_account(self, username: str, reason: str = ''):
        """将账号 state 设为 0（停用），加入 Redis 封禁集合，等待人工处理。"""
        logger.warning(f'[账号停用] {username} reason={reason}')
        try:
            db = MySQLTaskDB()
            db.update_account({'username': username, 'state': 0})
            db.close()
        except Exception:
            logger.error(f'停用账号写 MySQL 失败: {traceback.format_exc()}')
        try:
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
            _rc.set(f'crawler:banned:{username}', '1', ex=86400)
        except Exception:
            logger.error(f'停用账号写 Redis 失败: {traceback.format_exc()}')
        # 同步内存对象，防止后续 _end_session → _save_account 用 state=1 覆盖回去
        if self.account_info and self.account_info.username == username:
            self.account_info.state = 0

    def _goto_with_retry(self, url: str, wait_until: str = "domcontentloaded",
                        timeout: int = 25000, max_retries: int = 3) -> Optional:
        """
        带重试机制的 page.goto()，用于容错性较高的导航场景。

        :param url: 目标 URL
        :param wait_until: 等待条件
        :param timeout: 单次超时（毫秒）
        :param max_retries: 最大重试次数
        :return: Response 对象或 None
        """
        delays = [3, 6, 12]  # 秒数
        response = None

        for attempt in range(max_retries):
            _t0 = time.time()
            try:
                response = self.page.goto(url, wait_until=wait_until, timeout=timeout)
                _latency = (time.time() - _t0) * 1000
                _status = response.status if response else 0
                self._report_request(_status, _latency, False, url)
                self._log.debug(f"[导航] {url[:80]} 成功 (attempt {attempt+1})")
                return response
            except Exception as err:
                _latency = (time.time() - _t0) * 1000
                err_str = str(err)
                # 网络层错误（代理挂/超时）不是被风控拦截，is_blocked=False
                _is_network = "net::ERR_" in err_str or "timeout" in err_str.lower()
                self._report_request(0, _latency, not _is_network, url)
                if _is_network:
                    if attempt < max_retries - 1:
                        wait_sec = delays[min(attempt, len(delays)-1)]
                        self._log.warning(
                            f"[导航] 网络错误 (attempt {attempt+1}/{max_retries}), "
                            f"{wait_sec}s 后重试: {err_str[:150]}"
                        )
                        time.sleep(wait_sec)
                    else:
                        logger.error(f"[导航] 连续失败{max_retries}次: {url[:80]}")
                        raise NetworkException(err_str) from err
                else:
                    # 非网络错误，直接抛出
                    raise

        return response

    def _close_browser(self):
        """关闭浏览器（CDP 模式只断开连接，不关闭指纹浏览器窗口）"""
        try:
            if self._is_cdp:
                # CDP 模式：只断开 Playwright 连接，不关闭浏览器窗口
                # 指纹浏览器窗口保留，后续可以复用
                if self._browser:
                    self._browser.close()  # 断开 CDP 连接
            else:
                # 独立模式：关闭 context 和 browser
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self.page = None
        self._is_cdp = False

    def _release_page_memory_after_task(self):
        """
        成功任务后释放当前 Amazon 页面 DOM/JS 堆，但保留浏览器和 profile 供下个任务复用。
        """
        if not _env_bool("REVIEW_PLAYWRIGHT_BLANK_AFTER_TASK", True):
            return
        if not self.page:
            return
        try:
            if hasattr(self.page, "is_closed") and self.page.is_closed():
                return
        except Exception:
            return

        try:
            self._captured_responses.clear()
        except Exception:
            self._captured_responses = []

        try:
            self.page.evaluate("window.stop()")
        except Exception:
            pass

        try:
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            self._log.info("[内存释放] 任务完成后已跳转 about:blank")
        except Exception as e:
            self._log.debug(f"[内存释放] 跳转 about:blank 失败（不影响主流程）: {e}")
        finally:
            try:
                self._captured_responses.clear()
            except Exception:
                self._captured_responses = []

    def _quit_fingerprint_browser(self):
        """主动关闭 BitBrowser 指纹浏览器窗口（任务全部完成后调用）"""
        if not self.fingerprint_id:
            return
        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import close_browser
        try:
            close_browser(self.fingerprint_id)
        except Exception as e:
            logger.warning(f"_quit_fingerprint_browser 异常: {e}")

    # ========================== 响应监听 ==========================

    @staticmethod
    def _parse_post_data(post_data: str) -> dict:
        """解析 POST 表单数据为字典"""
        if not post_data:
            return {}
        try:
            from urllib.parse import parse_qs
            parsed = parse_qs(post_data, keep_blank_values=True)
            return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
        except Exception:
            return {}

    @staticmethod
    def _parse_url_params(url: str) -> dict:
        """从 URL 中解析查询参数"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = parse_qs(urlparse(url).query, keep_blank_values=True)
            return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
        except Exception:
            return {}

    def _set_active_review_asin(self, asin: str):
        """记录当前浏览器正在访问的评论页 ASIN，用于响应监听防脏数据。"""
        if asin:
            self._active_review_asin = asin

    def _get_response_filter_asin(self) -> str:
        return self._active_review_asin or (self.task.get('asin', '') if self.task else '')

    def _reset_review_integrity_stats(self) -> None:
        """重置本次任务的完整性统计。"""
        self._counted_review_page_keys = set()
        self._raw_review_slots_count = 0
        self._duplicate_review_slots_count = 0
        self._invalid_review_slots_count = 0
        self._review_expected_pages_seen = 0
        self._review_expected_pages_total = 0
        self._review_page_completion_ok = True

    def _review_page_key(self, entry: dict) -> str:
        """生成逻辑评论页 key，避免 document/AJAX/兜底重复解析时重复计数。"""
        req_params = entry.get('req_params') or {}
        url_params = self._parse_url_params(entry.get('url', ''))

        asin = (
            req_params.get('asin')
            or self._get_response_filter_asin()
            or (self.task or {}).get('asin', '')
        )
        page_number = req_params.get('pageNumber') or url_params.get('pageNumber') or '1'
        if not page_number:
            page_number = req_params.get('nextPageToken') or '1'

        star = (
            req_params.get('filterByStar')
            or url_params.get('filterByStar')
            or self._expected_star_filter
            or 'all_stars'
        )
        sort_by = req_params.get('sortBy') or url_params.get('sortBy') or self._expected_sort or ''
        format_type = (
            req_params.get('formatType')
            or url_params.get('formatType')
            or ('current_format' if self._expected_is_variant else '')
        )

        return '|'.join(map(str, [asin, star, sort_by, format_type, page_number]))

    def _record_review_integrity_stats(
            self,
            entry: dict,
            raw_count: int,
            parsed_count: int,
            duplicate_count: int,
            invalid_count: int,
    ) -> int:
        """记录唯一逻辑页的原始评论槽位数，返回本次新增槽位数。"""
        if raw_count <= 0:
            return 0

        page_key = self._review_page_key(entry)
        if page_key in self._counted_review_page_keys:
            self._log.debug(f"[完整性] 跳过重复页面计数: {page_key}")
            return 0

        self._counted_review_page_keys.add(page_key)
        self._raw_review_slots_count += int(raw_count or 0)
        self._duplicate_review_slots_count += max(duplicate_count, 0)
        self._invalid_review_slots_count += max(invalid_count, 0)
        self._log.info(
            f"[完整性] page_key={page_key} blocks={raw_count}, parsed={parsed_count}, "
            f"duplicate={duplicate_count}, invalid={invalid_count}"
        )
        return raw_count

    def _record_review_page_completion(self, pages_seen: int, expected_pages: int) -> None:
        expected_pages = max(int(expected_pages or 0), 0)
        pages_seen = max(int(pages_seen or 0), 0)
        if expected_pages <= 0:
            return
        self._review_expected_pages_total += expected_pages
        self._review_expected_pages_seen += min(pages_seen, expected_pages)
        if pages_seen < expected_pages:
            self._review_page_completion_ok = False

    def _on_response(self, response):
        """
        Playwright 响应回调：拦截评论相关的 AJAX 响应。
        防脏数据：
          1. URL 中必须包含当前 ASIN
          2. AJAX POST 请求的 data 参数（asin/sortBy/filterByStar/formatType）必须匹配预期
        """
        url = response.url
        try:
            # 只拦截评论相关的请求
            is_review_ajax = '/portal/customer-reviews/ajax/reviews/get/' in url
            is_first_page = '/product-reviews/' in url and response.request.resource_type == "document"

            if not is_review_ajax and not is_first_page:
                return
            # print(f'数据包：{url}')
            status = response.status
            current_asin = self._get_response_filter_asin()
            if status != 200:
                logger.warning(f'状态：{status}')
                self._report_request(status, 0, status in (403, 429, 503), url)
                # 429 → 提取 Retry-After 供外层重试退避
                if status == 429:
                    try:
                        retry_after_val = response.header_value('retry-after') or ''
                        if retry_after_val.isdigit():
                            self._retry_after_seconds = int(retry_after_val)
                            logger.warning(f"[429] Retry-After={self._retry_after_seconds}s")
                    except Exception:
                        pass
                # 发射 PAGE_FAILED 事件
                try:
                    push_event(self._get_redis(), EventType.PAGE_FAILED,
                               username=getattr(self.account_info, 'username', '') if self.account_info else '',
                               asin=current_asin,
                               country=self.task.get('country', '') if self.task else '',
                               http_status=status,
                               worker_id=str(self.worker_id or ''),
                               error_msg=f"HTTP {status}")
                except Exception:
                    pass
                return

            # ---- 防脏数据 第1层：URL 中的 ASIN 校验（仅对首页 document 请求）----
            # AJAX POST 请求的 ASIN 在 POST body 中，由第2层校验
            if is_first_page and current_asin and current_asin not in url:
                logger.warning(f"[监听] 丢弃非当前 ASIN 的响应: active_asin={current_asin}, url={url[:120]}")
                return

            # ---- 防脏数据 第2层：AJAX 请求参数校验 ----
            req_params = {}
            if is_review_ajax:
                post_data = response.request.post_data
                req_params = self._parse_post_data(post_data)
                logger.debug(f'req_params:{req_params}')
                # 校验 asin
                req_asin = req_params.get('asin', '')
                if current_asin and req_asin and req_asin != current_asin:
                    logger.warning(
                        f"[监听] 丢弃 ASIN 不匹配的 AJAX: "
                        f"请求 asin={req_asin}, 当前 asin={current_asin}"
                    )
                    return

                # 校验 sortBy
                req_sort = req_params.get('sortBy', '')
                if self._expected_sort and req_sort and req_sort != self._expected_sort:
                    logger.warning(
                        f"[监听] 丢弃排序不匹配的 AJAX: "
                        f"请求 sortBy={req_sort}, 预期={self._expected_sort}"
                    )
                    return

                # 校验 filterByStar
                req_star = req_params.get('filterByStar', '')
                expected_star = self._expected_star_filter or 'all_stars'
                if req_star and req_star != expected_star and expected_star != 'all_stars':
                    logger.warning(
                        f"[监听] 丢弃星级不匹配的 AJAX: "
                        f"请求 filterByStar={req_star}, 预期={expected_star}"
                    )
                    return

                # 校验 formatType（变体）
                req_format = req_params.get('formatType', '')
                if self._expected_is_variant and req_format and req_format != 'current_format':
                    self._log.warning(
                        f"[监听] 丢弃变体不匹配的 AJAX: "
                        f"请求 formatType={req_format}, 预期=current_format"
                    )
                    return

            elif is_first_page:
                req_params = self._parse_url_params(url)

            # ❗ 不要在回调里调用 response.text()，Playwright sync API 会死锁
            # 只保存 response 引用，body 延迟到 _read_pending_bodies() 再读取
            entry = {
                "url": url,
                "_response": response,
                "is_ajax": is_review_ajax,
                "is_first_page": is_first_page,
                "timestamp": time.time(),
                "req_params": req_params,
            }
            self._captured_responses.append(entry)
            # 上报拦截到的评论请求到 InfluxDB
            self._report_request(status, 0, False, url)
            # 发射 PAGE_FETCHED 事件
            try:
                _page_num = int(req_params.get('pageNumber', 1))
                push_event(self._get_redis(), EventType.PAGE_FETCHED,
                           username=getattr(self.account_info, 'username', '') if self.account_info else '',
                           asin=current_asin,
                           country=self.task.get('country', '') if self.task else '',
                           page=_page_num,
                           http_status=status,
                           worker_id=str(self.worker_id or ''),
                           proxy=self._get_proxy_ip())
            except Exception:
                pass
            # logger.debug(f"[监听] 拦截到数据包: ajax={is_review_ajax}, url={url[:120]}")

        except Exception as e:
            logger.warning(f"[监听] 处理响应异常: {e}\n{traceback.format_exc()}")

    # ========================== 行为模拟（触发 unagi 埋点） ==========================

    def _human_delay(self, base: float = 3.0, sigma: float = 0.5):
        """对数正态分布延迟，中位数≈base秒，偶尔出现长停顿"""
        import math as _math
        mu = _math.log(base)
        delay = max(1.5, random.lognormvariate(mu, sigma))
        time.sleep(min(delay, 30))

    def _simulate_human_behavior(self):
        """模拟人类浏览行为：滚动 + 鼠标移动 + hover 评论，让 unagi md 字段有真实数据"""
        try:
            # 1. 随机滚动（触发 scroll 事件）
            scroll_y = random.randint(200, 500)
            self.page.evaluate(f"window.scrollBy(0, {scroll_y})")
            time.sleep(random.uniform(0.4, 1.0))

            # 2. 随机鼠标移动（触发 mousemove 事件）
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, 900)
                y = random.randint(150, 600)
                self.page.mouse.move(x, y, steps=random.randint(5, 15))
                time.sleep(random.uniform(0.1, 0.4))

            # 3. hover 到某条评论上（模拟阅读）
            reviews = self.page.query_selector_all('[data-hook="review"]')
            if reviews:
                target = random.choice(reviews[:min(5, len(reviews))])
                try:
                    target.scroll_into_view_if_needed(timeout=3000)
                    time.sleep(random.uniform(0.3, 0.6))
                    target.hover(timeout=3000)
                    time.sleep(random.uniform(1.0, 3.0))
                except Exception:
                    pass

            # 4. 再滚动一点
            self.page.evaluate(f"window.scrollBy(0, {random.randint(100, 300)})")
            time.sleep(random.uniform(0.3, 0.8))

        except Exception as e:
            logger.debug(f"[行为模拟] 异常（不影响主流程）: {e}")

    def _warmup_homepage(self,asin):
        """会话预热：访问首页，产生正常浏览痕迹（仅会话首次调用）"""
        try:
            site = SITE_MAPPING[self.task['country'].upper()]
            self._log.info("[预热] 访问首页...")
            self._goto_with_retry(f"https://{site}/dp/{asin}", timeout=20000, max_retries=2)
            self._human_delay(2.5, 0.4)
            self._simulate_human_behavior()
            logger.info("[预热] 首页访问完成")
            # 首页预热成功事件
            try:
                _redis = self._get_redis()
                _username = getattr(self.account_info, 'username', '') if self.account_info else ''
                _country = self.task.get('country', '') if self.task else ''
                if _redis:
                    push_event(
                        _redis, EventType.PAGE_FETCHED,
                        username=_username, country=_country,
                        page=0, http_status=200,
                        extra={"type": "homepage_warmup"},
                    )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[预热] 首页异常（继续执行）: {e}")
            # 首页预热失败事件
            try:
                _redis = self._get_redis()
                _username = getattr(self.account_info, 'username', '') if self.account_info else ''
                _country = self.task.get('country', '') if self.task else ''
                if _redis:
                    push_event(
                        _redis, EventType.PAGE_FAILED,
                        username=_username, country=_country,
                        page=0, error_msg=str(e)[:200],
                        extra={"type": "homepage_warmup"},
                    )
            except Exception:
                pass

    def _visit_reviews_page(self, asin: str, country: str, use_recent_sort: bool = True) -> str:
        """
        直接访问评论页，检测 404 / 新 ASIN。
        如果评论页 404，访问首页验证 ASIN 是否错误或变更。

        :return: 实际 ASIN（可能因重定向而改变），None 表示无效
        """
        site = SITE_MAPPING[country.upper()]
        reviews_url = f"https://{site}/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_srt?pageNumber=1"
        reviews_url = add_recent_sort_param(reviews_url, use_recent_sort)
        reviews_url = add_current_format_param(reviews_url, should_use_current_format_filter(self.task))
        referer = f"https://{site}/dp/{asin}"
        self._set_active_review_asin(asin)

        self._log.info(f"[评论页] 直接访问: {reviews_url}")

        # 第一次进入评论页，设置 referer
        self.page.set_extra_http_headers({"referer": referer})
        _net_retries = 3
        _net_delays = [3, 6, 12]
        response = None

        response = self._goto_with_retry(reviews_url, timeout=25000, max_retries=3)

        self._human_delay(2.5, 0.4)
        self.page.set_extra_http_headers({})

        # 检测验证码
        self._handle_captcha(expected_url=reviews_url)

        # 检测登录
        self._handle_login_if_needed(reviews_url)

        # 登录后可能触发额外跳转，再次等待页面稳定后才能安全读取 content()
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)

        # ---- 1. 检测评论页 404 ----
        status = response.status if response else 0
        page_text = self.page.content()

        is_404 = (
            status == 404
            or "find that page. Try searching or go to Amazon's home page" in page_text
            or 'a href="/ref=cs_404_link"' in page_text
            or "PageNotFound" in page_text
        )

        if is_404:
            logger.warning(f"[评论页] 404，尝试访问首页验证 ASIN: {asin}")
            result = self._verify_asin_via_homepage(asin, country, site, use_recent_sort)
            if result == None:
                return result
            else:
                asin = result  #新asin 而且已经打开了新页面

        actual_asin = asin
        # ---- 3. 检测评论页是否有效 ----
        # 检查页面是否有评论相关元素
        has_reviews_element = self.page.query_selector('[data-hook="review"]') is not None
        reviews_title_el = self.page.query_selector('[data-hook="cr-filter-info-section"]')

        if not has_reviews_element and reviews_title_el:
            # 有标题栏但无评论元素，可能是 "No reviews"
            title_text = reviews_title_el.inner_text() if reviews_title_el else ""
            if "no reviews" in title_text.lower() or title_text.strip() == "0":
                logger.info(f"[评论页] 检测到无评论: {title_text[:50]}")
                self.review_counts = 0
                return actual_asin

        # 标记为有评论（后续会解析具体数量）
        self.review_counts = -1
        return actual_asin

    def _verify_asin_via_homepage(self, asin: str, country: str, site: str, use_recent_sort: bool = True) -> str:
        """
        评论页404后的验证流程：
        1. 进入商品页检查商品标题
        2. 检查 [data-hook="top-customer-reviews-title"] 判断是否有评论
        3. 有评论时调用 _click_see_more_reviews 进入评论页
        4. 从评论页URL检查是否有新ASIN
        :return: 实际 ASIN 或 None（无效）
        """
        product_url = f"https://{site}/dp/{asin}"

        logger.info(f"[商品页验证] 评论页404，进入商品页检查: {asin}")

        try:
            # 设置 referer 访问商品页
            # self.page.set_extra_http_headers({"referer": f"https://{site}/"})
            product_response = self._goto_with_retry(product_url, timeout=25000, max_retries=3)
            self._human_delay(2.0, 0.5)

            # 检测验证码
            self._handle_captcha(expected_url=product_url)
            self._handle_login_if_needed(product_url)

            # 检测商品页状态
            prod_status = product_response.status if product_response else 0
            prod_text = self.page.content()

            prod_is_404 = (
                prod_status == 404
                or "find that page" in prod_text
                or 'a href="/ref=cs_404_link"' in prod_text
            )

            if prod_is_404:
                logger.error(f"[商品页验证] 商品页 404，ASIN 无效: {asin}")
                # send_custom_robot_group_message(f'地址错误；{product_url} - {self.task}')
                self.review_counts = 0
                self._asin_not_found = True
                return None

            # ---- 1. 检查商品标题 ----
            if 'id="title_feature_div"' not in prod_text:
                # 先排除账号被重定向到登录页/验证码的情况，避免误判为 ASIN 无效
                current_url = self.page.url
                if 'ap/signin' in current_url or 'validateCaptcha' in current_url or 'captcha' in current_url.lower():
                    raise Exception(f'商品页访问被重定向到登录/验证码，账号异常: {current_url}')
                logger.error(f"[商品页验证] 商品页无效，无标题区域: {asin}")
                send_custom_robot_group_message(f'地址错误2；{product_url} - {self.task}')
                self.review_counts = 0
                self._asin_not_found = True
                return None

            # ---- 2. 检查评论标识 ----
            title_el = self.page.query_selector('[data-hook="top-customer-reviews-title"]')
            no_reviews = False
            if title_el:
                title_text = title_el.inner_text().strip()
                title_nums = re.findall(r'(\d{1,3}(?:[,\.]\d{3})*)', title_text)
                if not title_nums:
                    # "No customer reviews" 等无数字文本 → 无评论
                    no_reviews = True
                    logger.info(f"[商品页验证] 检测到无评论标识: '{title_text[:50]}'")
                    self.review_counts = 0
                    return asin

            # 有评论，标记为有评论
            self.review_counts = -1
            # ---- 3. 点击 "See more reviews" 进入评论页 ----
            entered_reviews = self._click_see_more_reviews(site)
            if not entered_reviews:
                # 未能点击，先从商品页检测是否有新ASIN
                logger.warning(f"[商品页验证] 未能点击 'See more reviews'，从商品页检测新ASIN")

                new_asin = None
                # 方法1: 从 averageCustomerReviews 元素提取
                pattern = r'id="averageCustomerReviews"[^>]*data-asin="([^"]+)"'
                matches = re.findall(pattern, self.page.content())
                if matches and matches[0] != asin:
                    new_asin = matches[0]
                    logger.warning(f"[商品页验证] 商品页发现新 ASIN: {asin} → {new_asin}")

                # 方法2: 从当前URL提取
                if not new_asin:
                    current_url = self.page.url
                    url_asin_match = re.search(r'/dp/([A-Z0-9]{10})', current_url)
                    if url_asin_match:
                        url_asin = url_asin_match.group(1)
                        if url_asin != asin:
                            new_asin = url_asin
                            logger.warning(f"[商品页验证] URL中发现新 ASIN: {asin} → {new_asin}")

                # 使用新ASIN跳转评论页，或原ASIN
                target_asin = new_asin if new_asin else asin
                reviews_url = f"https://{site}/product-reviews/{target_asin}/ref=cm_cr_arp_d_viewopt_srt?pageNumber=1"
                reviews_url = add_recent_sort_param(reviews_url, use_recent_sort)
                reviews_url = add_current_format_param(reviews_url, should_use_current_format_filter(self.task))
                referer = f"https://{site}/dp/{target_asin}"
                self._set_active_review_asin(target_asin)

                logger.info(f"[商品页验证] 直接导航到评论页: {reviews_url}")
                self.page.set_extra_http_headers({"referer": referer})
                self._goto_with_retry(reviews_url, timeout=25000, max_retries=3)
                self._human_delay(2.0, 0.3)
                self._handle_captcha(expected_url=reviews_url)
                self.page.set_extra_http_headers({})

                # 商品页正常但评论页再次 404 → 账号被风控，封号处理
                _retry_text = self.page.content()
                _retry_is_404 = (
                    "find that page" in _retry_text
                    or 'a href="/ref=cs_404_link"' in _retry_text
                    or "PageNotFound" in _retry_text
                )
                if _retry_is_404:
                    logger.error(
                        f"[商品页验证] 商品页正常但评论页二次 404，账号风控封禁: "
                        f"asin={asin}, account={getattr(self.account_info, 'username', '')}"
                    )
                    self._mark_account_unusable("商品页正常但评论页二次404，账号风控")
                    raise Exception("商品页正常但评论页二次404，账号可能被封")

                # 更新task中的ASIN
                if new_asin:
                    self.task['new_asin'] = new_asin
                    self.task['asin'] = new_asin
                    asin = new_asin

            # ---- 4. 从评论页 URL 检查 ASIN 是否变化 ----
            actual_asin = asin
            current_url = self.page.url
            url_asin_match = re.search(r'/product-reviews/([A-Z0-9]{10})', current_url)
            if url_asin_match:
                url_asin = url_asin_match.group(1)
                if url_asin != asin:
                    actual_asin = url_asin
                    logger.warning(f"[商品页验证] 评论页 URL 中发现新 ASIN: {asin} → {actual_asin}")
                    self._set_active_review_asin(actual_asin)
                    self.task['new_asin'] = actual_asin
                    self.task['asin'] = actual_asin
                    return actual_asin

            # # 检查 averageCustomerReviews 作为二次确认
            # pattern = r'id="averageCustomerReviews"[^>]*data-asin="([^"]+)"'
            # matches = re.findall(pattern, self.page.content())
            # if matches and matches[0] != asin:
            #     actual_asin = matches[0]
            #     logger.warning(f"[商品页验证] 页面属性中发现新 ASIN: {asin} → {actual_asin}")
            #     self.task['new_asin'] = actual_asin
            #     self.task['asin'] = actual_asin
            #     return actual_asin

            return asin

        except Exception as e:
            logger.error(f"[商品页验证] 验证过程异常: {e}")
            raise

    def _click_see_more_reviews(self, site: str) -> bool:
        """
        在商品详情页滚动到评论区，点击 "See more reviews" 链接进入评论页。
        对应图片中的 data-hook="see-all-reviews-link-foot"

        :return: True 如果成功点击并进入评论页
        """
        try:
            # 先滚动到页面中下部（评论区通常在底部）
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
            time.sleep(random.uniform(0.8, 1.5))

            # # 继续缓慢滚动，模拟真人浏览
            # for _ in range(random.randint(2, 4)):
            #     self.page.evaluate(f"window.scrollBy(0, {random.randint(200, 400)})")
            #     time.sleep(random.uniform(0.5, 1.2))

            # 查找 "See more reviews" 链接
            see_more = self.page.query_selector('[data-hook="see-all-reviews-link-foot"]')
            if not see_more:
                # 备选：有时链接文本不同
                see_more = self.page.query_selector('a[data-hook="see-all-reviews-link-foot"]')

            if not see_more:
                return False

            href = see_more.get_attribute('href') or ''
            href_asin_match = re.search(r'/product-reviews/([A-Z0-9]{10})', href)
            if href_asin_match:
                self._set_active_review_asin(href_asin_match.group(1))

            # 滚动到按钮可见
            see_more.scroll_into_view_if_needed(timeout=5000)
            time.sleep(random.uniform(0.5, 1.0))

            # 鼠标移过去再点击（更真实）
            see_more.hover(timeout=3000)
            time.sleep(random.uniform(0.3, 0.8))

            logger.info("[商品页] 点击 'See more reviews'")
            see_more.click()
            self._human_delay(3.0, 0.5)

            # 等待评论页加载
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)

            # 验证是否到了评论页（注意：可能被重定向到登录页或404）
            current_url = self.page.url
            page_text = self.page.content()

            # 检测 404（可能是账号被封）
            is_404 = (
                "find that page" in page_text
                or 'a href="/ref=cs_404_link"' in page_text
                or "PageNotFound" in page_text
            )
            if is_404:
                logger.error(f"[商品页] 点击后页面 404，账号可能被封: {current_url}")
                raise Exception(f'点击更多评论后404，账号可能被封')

            if 'ap/signin' in current_url or 'signin' in current_url:
                logger.warning(f"[商品页] 点击后跳转到登录页，尝试自动登录: {current_url}")
                self._handle_login_if_needed(current_url)
                self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                self._human_delay(2.0, 0.5)
                current_url = self.page.url

            if 'product-reviews' in current_url or 'customerReviews' in current_url:
                # logger.info(f"[商品页] 已进入评论页: {current_url}")
                return True
            else:
                logger.warning(f"[商品页] 点击后未到评论页，当前URL: {current_url}")
                return False

        except Exception as e:
            logger.warning(f"[商品页] 点击 'See more reviews' 异常: {e}")
            return False

    # ========================== 下拉框交互（模拟真人操作） ==========================

    def _try_select_dropdown(self, selector: str, value: str, name: str = '') -> bool:
        """
        通过下拉框选择选项。
        每次选择前清空 _captured_responses，选择后等待页面刷新，
        这样 _captured_responses 中只保留最新一次筛选结果的数据。
        """
        try:
            el = self.page.query_selector(selector)
            if not el:
                logger.debug(f"[下拉框] 未找到 {name or selector}")
                return False

            current = el.input_value()
            if current == value:
                logger.debug(f"[下拉框] {name or selector} 已是目标值 {value}")
                return True

            # 清空旧的响应数据，准备接收新页面数据
            self._captured_responses = []

            self._log.info(f"[下拉框] {name or selector}: {current} → {value}")
            self.page.select_option(selector, value)
            self._human_delay(2.5, 0.5)

            # 等待页面刷新
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            self.page.wait_for_timeout(1000)
            return True

        except Exception as e:
            logger.warning(f"[下拉框] {name or selector} 操作失败: {e}")
            return False

    def _select_sort_order(self, sort_by: str = 'recent') -> bool:
        """
        选择排序方式：recent=Most recent, helpful=Top reviews。
        返回 True=下拉框成功(AJAX更新), False=下拉框失败已使用 URL 回退导航。
        """
        value = 'recent' if sort_by == 'recent' else 'helpful'
        self._expected_sort = value  # 先设置，确保 _on_response 校验时已生效
        ok = self._try_select_dropdown('#sort-order-dropdown', value, '排序方式')
        if not ok:
            logger.warning(f"[回退] 排序下拉框失败，使用 URL 直接导航")
            asin = self.task.get('asin', '') if self.task else ''
            country = self.task.get('country', 'US') if self.task else 'US'
            self._navigate_to_reviews_fallback(
                asin, country, sort_by=value,
                filter_star=self._expected_star_filter,
                is_variant=self._expected_is_variant)
            return False  # URL 回退已包含所有参数
        return True  # 下拉框成功，页面 AJAX 更新

    def _select_variant_filter(self) -> bool:
        """如果存在变体下拉框，选择当前变体（current_format）"""
        el = self.page.query_selector('.format-type-select')
        if not el:
            return False
        # logger.info("[下拉框] 检测到变体下拉框，选择当前变体")
        self._expected_is_variant = True  # 先设置，确保 _on_response 校验时已生效
        ok = self._try_select_dropdown('#format-type-dropdown', 'current_format', '变体筛选')
        if not ok:
            logger.warning(f"[回退] 变体下拉框失败，使用 URL 直接导航")
            asin = self.task.get('asin', '') if self.task else ''
            country = self.task.get('country', 'US') if self.task else 'US'
            self._navigate_to_reviews_fallback(
                asin, country, sort_by=self._expected_sort,
                filter_star=self._expected_star_filter,
                is_variant=True)
        return True

    def _select_star_filter(self, filter_star: str) -> bool:
        """
        选择星级筛选。
        filter_star: five_star / four_star / three_star / two_star / one_star / all_stars / ''
        """
        if not filter_star:
            filter_star = 'all_stars'
        self._expected_star_filter = filter_star  # 先设置，确保 _on_response 校验时已生效
        ok = self._try_select_dropdown('#star-count-dropdown', filter_star, '星级筛选')
        if not ok:
            logger.warning(f"[回退] 星级下拉框失败，使用 URL 直接导航")
            asin = self.task.get('asin', '') if self.task else ''
            country = self.task.get('country', 'US') if self.task else 'US'
            self._navigate_to_reviews_fallback(
                asin, country, sort_by=self._expected_sort,
                filter_star=filter_star,
                is_variant=self._expected_is_variant)
        return True

    # ========================== 页面操作 ==========================

    def _navigate_to_reviews_fallback(self, asin: str, country: str,
                                       filter_star: str = '', sort_by: str = 'recent',
                                       is_variant: bool = False):
        """
        直接通过 URL 导航到评论页（回退方案）。
        当下拉框交互失败时使用。
        """
        site = SITE_MAPPING[country.upper()]
        params = "pageNumber=1"
        if sort_by:
            params += f"&sortBy={sort_by}"
        if filter_star:
            params += f"&filterByStar={filter_star}"

        url = f"https://{site}/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_srt?{params}"
        url = add_current_format_param(url, is_variant)
        logger.info(f"[回退] 直接导航到: {url}")
        self._set_active_review_asin(asin)

        # 同步预期状态（供 _on_response 校验）
        self._expected_sort = sort_by if sort_by else 'recent'
        self._expected_star_filter = filter_star or 'all_stars'
        self._expected_is_variant = is_variant

        self._captured_responses = []
        self._goto_with_retry(url, timeout=30000, max_retries=2)
        self._human_delay(2.5, 0.4)

        # 检测验证码
        self._handle_captcha(expected_url=url)

        # 检测登录
        self._handle_login_if_needed(url)

        # 模拟浏览行为
        self._simulate_human_behavior()

        return url

    def _handle_captcha(self, expected_url: str = None):
        """处理验证码页面：点击 'Continue shopping' 按钮，并验证跳转回预期页面"""
        try:
            from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import is_bit_browser_headless
            headless = is_bit_browser_headless()
            captcha_form = self.page.query_selector('form[action*="validateCaptcha"]')
            if captcha_form:
                self._log.warning("[验证码] 检测到验证码拦截页，尝试自动通过")
                # 发射 ROBOT_CHECK 事件
                try:
                    push_event(self._get_redis(), EventType.ROBOT_CHECK,
                               username=getattr(self.account_info, 'username', ''),
                               asin=self.task.get('asin', '') if self.task else '',
                               country=self.task.get('country', '') if self.task else '',
                               worker_id=str(self.worker_id or ''))
                except Exception:
                    pass
                # 用 page.locator 定位按钮（避免 ElementHandle 上下文失效）
                submit_btn = self.page.locator('form[action*="validateCaptcha"] button[type="submit"]')
                if submit_btn.count() > 0:
                    submit_btn.first.click()
                    logger.info("已点击 Continue shopping 按钮，等待页面跳转...")
                    self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(random.uniform(2.0, 4.0))

                    # 检查是否仍然在验证码页面（可能需要图片验证码）
                    still_captcha = self.page.query_selector('form[action*="validateCaptcha"]')
                    if still_captcha:
                        if headless:
                            raise Exception("HEADLESS_CAPTCHA: 无头模式仍在验证码页面，无法等待人工处理")
                        logger.warning("点击后仍在验证码页面（可能需要图片验证码），等待手动处理...")
                        self.page.wait_for_selector(
                            'form[action*="validateCaptcha"]',
                            state="hidden",
                            timeout=60000
                        )
                        time.sleep(3)
                else:
                    if headless:
                        raise Exception("HEADLESS_CAPTCHA: 无头模式未找到验证码提交按钮，无法等待人工处理")
                    logger.warning("未找到 submit 按钮，等待手动处理...")
                    self.page.wait_for_selector(
                        'form[action*="validateCaptcha"]',
                        state="hidden",
                        timeout=60000
                    )
                    time.sleep(3)

                # 验证码处理完毕，检查是否回到预期页面
                if expected_url:
                    current_url = self.page.url
                    if 'validateCaptcha' in current_url or 'challenge' in current_url:
                        logger.warning(f"[验证码] 仍在验证码相关页面，重新导航: {expected_url}")
                        self._goto_with_retry(expected_url, timeout=25000, max_retries=2)
                        self._human_delay(2.0, 0.3)
                    elif expected_url not in current_url and current_url.rstrip('/') != expected_url.rstrip('/'):
                        logger.warning(f"[验证码] 跳转页面不匹配，当前: {current_url}，预期: {expected_url}，重新导航")
                        self._goto_with_retry(expected_url, timeout=25000, max_retries=2)
                        self._human_delay(2.0, 0.3)
                    else:
                        logger.info(f"[验证码] 已正确回到预期页面: {current_url}")
        except Exception as exc:
            if "HEADLESS_CAPTCHA" in str(exc):
                raise
            logger.warning(f"处理验证码异常: {traceback.format_exc()}")

    def _handle_login_if_needed(self, return_url: str):
        """检测是否需要登录"""
        current_url = self.page.url
        if 'signin' not in current_url and 'ap/signin' not in current_url:
            return

        self._log.warning("[登录] 检测到登录页，尝试自动登录")
        # 发射 LOGIN_REDIRECT 事件
        try:
            push_event(self._get_redis(), EventType.LOGIN_REDIRECT,
                       username=getattr(self.account_info, 'username', '') if self.account_info else '',
                       asin=self.task.get('asin', '') if self.task else '',
                       country=self.task.get('country', '') if self.task else '',
                       worker_id=str(self.worker_id or ''))
        except Exception:
            pass
        if not self.account_info:
            raise Exception("需要登录但无可用账号信息")

        # 登录核心步骤（委托给统一工具模块，与 amazon_base.py 共用同一实现）
        from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import playwright_amazon_login
        try:
            playwright_amazon_login(
                self.page,
                self.account_info.username,
                self.account_info.password,
                getattr(self.account_info, 'totp_secret', None),
                on_disable_account=self._disable_account,
            )
        except Exception as e:
            raise Exception(f"自动登录失败: {str(e)}")

        logger.info("登录成功")
        time.sleep(2)

        # 导航回评论页
        if 'product-reviews' not in self.page.url:
            self._goto_with_retry(return_url, timeout=30000, max_retries=2)
            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                self.page.wait_for_load_state("domcontentloaded", timeout=8000)

    def _get_page_review_count(self) -> int:
        """从页面中获取评论总数"""
        try:
            count_el = self.page.query_selector('[data-hook="cr-filter-info-review-rating-count"]')
            if count_el:
                text = count_el.inner_text()
                nums = re.findall(r'(\d{1,3}(?:[,.]\d{3})*)', text)
                if nums:
                    return int(nums[0].replace(',', '').replace('.', ''))
        except Exception:
            pass
        return 0

    def _check_blocked(self) -> bool:
        """检查账号是否被限制查看评论"""
        try:
            widget = self.page.query_selector('[data-hook="request-more-reviews-widget"]')
            if widget:
                text = widget.inner_text()
                if 'To see more reviews' in text or 'Your request to see more reviews' in text:
                    logger.error(f'账号无法查看评论，可能被封: {getattr(self.account_info, "username", "")}')
                    return True
        except Exception:
            pass
        return False

    def _has_variant(self) -> bool:
        """检测是否存在产品变体"""
        try:
            return self.page.query_selector('.format-type-select') is not None
        except Exception:
            return False

    def _click_next_page(self) -> bool:
        """
        点击下一页按钮，返回是否成功。
        翻页前模拟人类行为（滚动到底部 + 鼠标移动），让 unagi 埋点捕获自然交互。
        支持两种翻页方式：
          1. show-more-button（新分页）
          2. 传统的下一页链接（a-last）
        """
        self._last_pagination_failure_reason = ""
        try:
            # 翻页前模拟阅读：逐步向下滚动，偶尔小幅回滚（模拟真人浏览评论）
            for _ in range(random.randint(2, 4)):
                if random.random() < 0.25:
                    # 偶尔小幅上滑（回看上一条评论）
                    self.page.evaluate(f"window.scrollBy(0, -{random.randint(80, 150)})")
                    self.page.wait_for_timeout(random.randint(500, 1000))
                else:
                    self.page.evaluate(f"window.scrollBy(0, {random.randint(300, 500)})")
                    self.page.wait_for_timeout(random.randint(800, 1500))

            # 方式1：show-more-button（新分页方式，AJAX 加载）
            show_more = self.page.query_selector('[data-hook="show-more-button"]')
            if show_more and show_more.is_visible():
                # logger.info("[翻页] 点击 show-more-button")
                show_more.scroll_into_view_if_needed(timeout=3000)
                self.page.wait_for_timeout(random.randint(300, 800))
                show_more.click()
                # ❗ 必须用 wait_for_timeout 而非 time.sleep，否则 _on_response 回调不会触发
                self.page.wait_for_timeout(random.randint(2000, 3500))
                return True

            # 方式2：传统下一页（整页导航）
            next_link = self.page.query_selector('li.a-last a')
            if next_link and next_link.is_visible():
                logger.info("[翻页] 点击下一页链接")
                next_link.scroll_into_view_if_needed(timeout=3000)
                self.page.wait_for_timeout(random.randint(300, 800))
                next_link.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                self.page.wait_for_timeout(random.randint(1500, 3000))
                return True

            self._last_pagination_failure_reason = "next_button_missing"
            logger.info("[翻页] 未找到翻页按钮，已到最后一页")
            return False

        except Exception as e:
            self._last_pagination_failure_reason = f"click_exception: {str(e)[:160]}"
            logger.warning(f"[翻页] 异常: {e}")
            return False

    def _wait_for_pagination_responses(self, timeout_ms: int = 5000) -> bool:
        """等待翻页 AJAX 响应进入捕获队列，避免代理慢时过早判断翻页失败。"""
        deadline = time.time() + max(timeout_ms, 500) / 1000
        while time.time() < deadline:
            if self._captured_responses:
                return True
            self.page.wait_for_timeout(250)
        return bool(self._captured_responses)

    def _fetch_show_more_ajax_fallback(self) -> Optional[dict]:
        """点击 show-more 无响应时，按 data-reviews-state-param 在浏览器上下文里补发 AJAX。"""
        try:
            show_more = self.page.query_selector('[data-hook="show-more-button"]')
            if not show_more:
                self._last_pagination_failure_reason = "fallback_show_more_missing"
                return None

            state_raw = str(show_more.get_attribute("data-reviews-state-param") or "").strip()
            if not state_raw:
                self._last_pagination_failure_reason = "fallback_show_more_state_empty"
                return None

            try:
                state = json.loads(state_raw)
            except Exception:
                self._last_pagination_failure_reason = "fallback_show_more_state_invalid"
                return None

            current_asin = self._get_response_filter_asin()
            url_params = self._parse_url_params(self.page.url)
            sort_by = self._expected_sort or url_params.get("sortBy", "")
            format_type = "current_format" if (
                self._expected_is_variant or url_params.get("formatType") == "current_format"
            ) else ""
            filter_by_star = self._expected_star_filter or url_params.get("filterByStar") or "all_stars"
            reftag = str(show_more.get_attribute("data-reftag") or "cm_cr_getr_d_paging_btm")
            origin = self.page.evaluate("location.origin")
            ajax_url = f"{origin}/portal/customer-reviews/ajax/reviews/get/ref=cm_cr_getr_d_paging_btm"

            data = {
                "sortBy": sort_by,
                "reviewerType": "",
                "formatType": format_type,
                "mediaType": "",
                "filterByStar": filter_by_star,
                "filterByAge": "",
                "pageNumber": str(state.get("pageNumber") or ""),
                "filterByLanguage": "",
                "filterByKeyword": "",
                "nextPageToken": str(state.get("nextPageToken") or ""),
                "shouldAppend": str(state.get("shouldAppend") or "true"),
                "deviceType": str(state.get("deviceType") or "desktop"),
                "canShowIntHeader": str(state.get("canShowIntHeader") or "true"),
                "reviewsShown": "",
                "reftag": reftag,
                "pageSize": "10",
                "asin": current_asin,
                "scope": "reviewsAjax3",
            }

            csrf = self.reviews_csrf_token or ""
            self._log.warning(
                f"[翻页] 点击无响应，使用 show-more AJAX 兜底: "
                f"page={data['pageNumber']}, token={bool(data['nextPageToken'])}, asin={current_asin}"
            )
            result = self.page.evaluate(
                """async ({url, data, csrf}) => {
                    const body = new URLSearchParams(data).toString();
                    const headers = {
                        "accept": "text/html,*/*",
                        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "x-requested-with": "XMLHttpRequest"
                    };
                    if (csrf) headers["anti-csrftoken-a2z"] = csrf;
                    const resp = await fetch(url, {
                        method: "POST",
                        headers,
                        body,
                        credentials: "include",
                        referrer: location.href,
                        referrerPolicy: "strict-origin-when-cross-origin"
                    });
                    return {status: resp.status, url: resp.url, text: await resp.text()};
                }""",
                {"url": ajax_url, "data": data, "csrf": csrf},
            )
            status = int((result or {}).get("status") or 0)
            if status != 200:
                self._last_pagination_failure_reason = f"fallback_show_more_http_{status}"
                self._log.warning(f"[翻页] show-more AJAX 兜底失败: status={status}")
                return None
            resp_text = result.get("text") or ""
            self._sync_show_more_state_from_ajax(resp_text)
            return {
                "resp": resp_text,
                "url": result.get("url") or ajax_url,
                "is_ajax": True,
                "is_first_page": False,
                "req_params": data,
            }
        except Exception as e:
            self._last_pagination_failure_reason = f"fallback_show_more_exception: {str(e)[:160]}"
            self._log.warning(f"[翻页] show-more AJAX 兜底异常: {e}")
            return None

    def _sync_show_more_state_from_ajax(self, resp_text: str) -> None:
        """手动 fetch 不会更新 DOM，这里把返回里的下一页 token 同步到当前按钮。"""
        try:
            html_content = self._extract_html_from_ajax(resp_text)
            if not html_content:
                return
            soup = BeautifulSoup(html_content, "html.parser")
            next_button = soup.find(attrs={"data-hook": "show-more-button"})
            if not next_button:
                return
            next_state = next_button.get("data-reviews-state-param") or ""
            next_reftag = next_button.get("data-reftag") or ""
            if not next_state:
                return
            self.page.evaluate(
                """({state, reftag}) => {
                    const btn = document.querySelector('[data-hook="show-more-button"]');
                    if (!btn) return false;
                    btn.setAttribute('data-reviews-state-param', state);
                    if (reftag) btn.setAttribute('data-reftag', reftag);
                    return true;
                }""",
                {"state": next_state, "reftag": next_reftag},
            )
            self._log.info("[翻页] 已同步 show-more 下一页 token")
        except Exception as e:
            self._log.warning(f"[翻页] 同步 show-more token 失败: {e}")

    # ========================== 数据解析 ==========================

    def _parse_captured_responses(self):
        """解析所有已捕获的响应，提取评论数据"""
        # ---- 防脏数据：解析前校验页面状态 ----
        current_asin = self._get_response_filter_asin()

        # 调试日志：帮助诊断 review_counts 为何保持 -1
        # logger.debug(f"[_parse_captured_responses] 开始解析，当前 review_counts={self.review_counts}, 响应队列长度={len(self._captured_responses)}")

        if current_asin and self.page:
            page_url = self.page.url
            if current_asin not in page_url and 'product-reviews' in page_url:
                self._log.error(
                    f"[防脏数据] 页面 ASIN 与任务不符！"
                    f"任务 ASIN={current_asin}, 页面 URL={page_url[:120]}。"
                    f"可能有人工干预，丢弃 {len(self._captured_responses)} 条待解析响应"
                )
                self._captured_responses.clear()
                return

        while self._captured_responses:
            entry = self._captured_responses.pop(0)
            try:
                # 延迟读取 body（回调里不能调 response.text()，会死锁）
                if '_response' in entry and 'resp' not in entry:
                    try:
                        entry['resp'] = entry['_response'].text()
                    except Exception as e:
                        self._log.warning(f"[解析] 读取响应 body 失败: {e}, url={entry['url'][:100]}")
                        continue
                    finally:
                        entry.pop('_response', None)

                if not entry.get('resp'):
                    continue

                # 二次校验：仅对首页 document 请求检查 URL 中的 ASIN
                # AJAX 请求的 ASIN 在 POST data 中，已在 _on_response 中校验过
                if entry.get('is_first_page') and current_asin and current_asin not in entry.get('url', ''):
                    self._log.warning(f"[防脏数据] 丢弃非当前 ASIN 响应: {entry['url'][:100]}")
                    continue

                reviews = self._parse_single_response(entry)
                if reviews:
                    self._all_reviews.extend(reviews)
                    # self._log.info(f"[解析] 从数据包解析到 {len(reviews)} 条评论，累计 {len(self._all_reviews)} 条")
                else:
                    # print('没有数据')
                    pass
            except Exception as e:
                self._log.warning(f"[解析] 数据包解析失败: {e}")

    def _parse_single_response(self, entry: dict) -> List[dict]:
        """解析单个响应中的评论数据"""
        resp_text = entry['resp']
        url = entry['url']
        is_ajax = entry['is_ajax']

        # AJAX 响应格式：用 &&& 分隔的 JSON 数组
        if is_ajax and '&&&' in resp_text:
            html_content = self._extract_html_from_ajax(resp_text)
        else:
            html_content = resp_text

        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")

        # 提取 CSRF token（仅首页 HTML 包含）
        cr_state_span = soup.find('span', id='cr-state-object')
        if cr_state_span and cr_state_span.get('data-state'):
            data_state = json.loads(cr_state_span['data-state'])
            self.reviews_csrf_token = data_state.get('reviewsCsrfToken', '')

        # 提取评论数量
        count_div = soup.find('div', {'data-hook': 'cr-filter-info-review-rating-count'})
        if count_div:
            count_text = count_div.get_text(strip=True)
            nums = re.findall(r'(\d{1,3}(?:[,.]\d{3})*)', count_text)
            if nums:
                self.review_counts = int(nums[0].replace(',', '').replace('.', ''))
                self.pages = 10 if self.review_counts > 100 else math.ceil(self.review_counts / 10)
            else:
                self._log.info(f'没有评论：{count_text}-{self.task}')
                self.review_counts = 0
                self.pages = 1
                return []
        else:
            print('评论数解析失败')
        # 解析评论块
        blocks = soup.find_all('div', class_='a-section review aok-relative') or \
                 soup.find_all('li', {'data-hook': 'review'})

        block_htmls = [str(b) for b in blocks]
        del blocks
        soup.decompose()
        del soup

        reviews = []
        dup_count = 0
        parsed_count = 0
        invalid_count = 0
        for block_html in block_htmls:
            try:
                review = self._parse_single_review(block_html)
                if review:
                    parsed_count += 1
                if review and review['reviewId'] not in self._seen_review_ids:
                    self._seen_review_ids.add(review['reviewId'])
                    reviews.append(review)
                elif review:
                    dup_count += 1
            except Exception as e:
                invalid_count += 1
                logger.warning(f"[解析] 单条评论解析失败: {e}")
                continue

        if dup_count:
            total_parsed = len(reviews) + dup_count
            if total_parsed > 0 and dup_count / total_parsed > 0.5:
                logger.warning(f"[去重] 跳过 {dup_count}/{total_parsed} 条重复评论（翻页可能未生效）")
            else:
                logger.debug(f"[去重] 跳过 {dup_count} 条重复评论（正常页间重叠）")

        self._record_review_integrity_stats(entry, len(block_htmls), len(reviews), dup_count, invalid_count)
        return reviews

    def _extract_html_from_ajax(self, resp_text: str) -> str:
        """从 AJAX &&& 格式响应中提取 HTML 片段"""
        parts = resp_text.split('&&&')
        html_content = ''
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if ('a-unordered-list a-nostyle a-vertical' in part or
                    'cr-filter-info-review-rating-count' in part or
                    'show-more-button' in part):
                try:
                    parsed = json.loads(part)
                    if isinstance(parsed, list) and len(parsed) >= 3:
                        html_content += "\n" + parsed[2]
                except (json.JSONDecodeError, IndexError):
                    pass
        return html_content

    def _parse_single_review(self, block_html: str) -> Optional[dict]:
        """解析单个评论 HTML 块"""
        task = self.task
        review_data, missing_fields = parse_review_block_html(
            block_html,
            task,
            SITE_MAPPING,
            self._get_site_code,
            self._log,
        )
        if not review_data:
            return None

        if not missing_fields:
            return review_data
        else:
            self._log.warning(
                f"[解析] 评论字段不完整，跳过: reviewId={review_data.get('reviewId', '')}, "
                f"missing={','.join(missing_fields)}"
            )
            alert_review_parse_error(
                task=task,
                review_data=review_data,
                missing_fields=missing_fields,
                block_html=block_html,
                error_msg=f"评论字段不完整: {','.join(missing_fields)}",
                source="playwright",
                log=self._log,
            )

            mysql_db = None
            try:
                mysql_db = MySQLTaskDB()
                mysql_db.insert_reviews_error(
                    asin=task.get('asin', ''),
                    country=task.get('country', ''),
                    resp=block_html,
                    review_data=review_data,
                    task_info=task,
                    error_msg=f"评论字段不完整: {','.join(missing_fields)}"
                )
            except Exception as e:
                self._log.warning(f"[解析] 保存字段不完整评论到MySQL失败: {e}")
            finally:
                if mysql_db:
                    mysql_db.close()
            return None

    def _parse_review_date(self, review_data: dict, block):
        parse_review_date(review_data, block, self.task.get("country", ""), logger)

    def _get_site_code(self, country_code: str) -> str:
        """国家名模糊匹配站点代码"""
        for code, keywords in COUNTRY_MAPPING.items():
            for keyword in keywords:
                if keyword.lower() in country_code.lower():
                    return code
        fallback = self.task.get('country', '').upper()
        return fallback

    # ========================== 星级过滤映射 ==========================

    @staticmethod
    def _star_filter_value(star: int) -> str:
        return {5: 'five_star', 4: 'four_star', 3: 'three_star', 2: 'two_star', 1: 'one_star'}.get(star, '')

    # ========================== 日期截止辅助 ==========================

    @staticmethod
    def _parse_date_cutoff(query_conditions):
        """解析 date_from，支持相对天数 '30d' 和绝对日期 '2025-04-01'"""
        date_from = (query_conditions or {}).get('date_from', '')
        if not date_from:
            return None
        try:
            s = str(date_from).strip()
            if s.endswith('d'):
                days = int(s[:-1])
                return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
            return datetime.strptime(s, '%Y-%m-%d')
        except Exception:
            logger.warning(f"[日期截止] date_from 格式无法解析: {date_from!r}，忽略")
            return None

    def _check_page_date_cutoff(self) -> bool:
        """评论按时间倒序，末尾评论早于截止日期则裁剪旧数据并返回 True（调用方应停止翻页）"""
        date_cutoff = getattr(self, '_date_cutoff', None)
        if not date_cutoff or not self._all_reviews:
            return False
        last = self._all_reviews[-1].get('reviewDate')
        if not last:
            return False
        try:
            if datetime.strptime(last, '%Y-%m-%d') < date_cutoff:
                self._all_reviews = [r for r in self._all_reviews
                                     if r.get('reviewDate') and datetime.strptime(r['reviewDate'], '%Y-%m-%d') >= date_cutoff]
                self._date_cutoff_current_filter_hit = True
                self._log.info(f"[日期截止] 末尾评论={last} 已过截止日期，裁剪后剩 {len(self._all_reviews)} 条，停止翻页")
                return True
        except Exception:
            pass
        return False

    def _get_last_review_date(self, start_count: int = 0) -> str:
        reviews = (getattr(self, '_all_reviews', []) or [])[max(0, start_count):]
        for review in reversed(reviews):
            review_date = (review or {}).get('reviewDate')
            if review_date:
                return review_date
        return ""

    def _date_cutoff_gap_days(self, review_date: str):
        date_cutoff = getattr(self, '_date_cutoff', None)
        if not date_cutoff or not review_date:
            return None
        try:
            return (datetime.strptime(review_date, '%Y-%m-%d') - date_cutoff).days
        except Exception:
            return None

    def _validate_date_cutoff_short_page_completion(
            self,
            pages_seen: int,
            expected_pages: int,
            star_label: str,
            star_start_count: int,
    ) -> None:
        """date_from 任务未到 10 页时仍要校验翻页是否提前结束。"""
        date_cutoff = getattr(self, '_date_cutoff', None)
        if not date_cutoff or getattr(self, '_date_cutoff_current_filter_hit', False):
            return
        if pages_seen >= 10:
            return

        last_review_date = self._get_last_review_date(star_start_count)
        if expected_pages and pages_seen < expected_pages:
            gap_days = self._date_cutoff_gap_days(last_review_date)
            if gap_days is not None and abs(gap_days) <= 5:
                self._alert_date_cutoff_not_reached(
                    pages_seen,
                    star_label,
                    star_start_count,
                    allow_short=True,
                    alert_reason=(
                        f"评论任务第{pages_seen}页提前结束，但最小评论日期距 date_from "
                        f"{abs(gap_days)}天以内，未触发重试，请人工确认。"
                    ),
                )
                return
            raise Exception(
                f"[日期截止完整性] 翻页提前结束，触发重试。ASIN={(self.task or {}).get('asin', '')}, "
                f"country={(self.task or {}).get('country', '')}, date_from={date_cutoff.strftime('%Y-%m-%d')}, "
                f"star={star_label}, 已抓到第{pages_seen}页, 预计={expected_pages}页, "
                f"最后评论日期={last_review_date or '-'}, 已抓取={len(self._all_reviews)}"
            )

        if not expected_pages or expected_pages >= 10:
            return
        expected_count = min(max(int(getattr(self, 'review_counts', 0) or 0), 0), expected_pages * 10)
        if expected_count <= 0:
            return
        current_count = max(0, len(self._all_reviews) - star_start_count)
        if current_count != expected_count:
            self._log.warning(
                f"[日期截止完整性] {star_label}星 实际={current_count}, 预期={expected_count}, "
                f"最后评论日期={last_review_date or '-'}"
            )
        if current_count < 1 or (
                expected_count > 0 and current_count / expected_count < 0.95
                and expected_count - current_count > 10
        ):
            raise Exception(
                f"[日期截止完整性] 评论抓取数量不足，触发重试。ASIN={(self.task or {}).get('asin', '')}, "
                f"country={(self.task or {}).get('country', '')}, date_from={date_cutoff.strftime('%Y-%m-%d')}, "
                f"star={star_label}, 实际={current_count}, 预期={expected_count}, "
                f"最后评论日期={last_review_date or '-'}"
            )

    def _alert_date_cutoff_not_reached(
            self,
            pages_seen: int,
            star_label: str = "all",
            star_start_count: int = 0,
            allow_short: bool = False,
            alert_reason: str = "",
    ) -> None:
        """date_from 任务抓到 10 页仍未遇到旧评论时告警，交给人工后续处理。"""
        date_cutoff = getattr(self, '_date_cutoff', None)
        if not date_cutoff or getattr(self, '_date_cutoff_current_filter_hit', False):
            return
        if (pages_seen < 10 and not allow_short) or getattr(self, '_date_cutoff_alert_sent', False):
            return

        self._date_cutoff_alert_sent = True
        asin = (self.task or {}).get('asin', '')
        country = (self.task or {}).get('country', '')
        last_review_date = self._get_last_review_date(star_start_count)
        reason = alert_reason or (
            f"评论任务抓到第{pages_seen}页仍未到达 date_from，可能超过单条件100条上限，请人工处理。"
        )
        message = (
            f"[日期截止告警] {reason}ASIN={asin}, country={country}, "
            f"date_from={date_cutoff.strftime('%Y-%m-%d')}, star={star_label}, "
            f"最后评论日期={last_review_date or '-'}, 已抓取={len(self._all_reviews)}"
        )
        self._log.warning(message)
        # try:
        #     send_custom_robot_group_message(message)
        # except Exception:
        #     self._log.warning(f"[日期截止告警] 发送失败: {traceback.format_exc()[:500]}")

    # ========================== 主入口 ==========================

    def get_reviews(self, task: dict) -> List[dict]:
        """
        Playwright 版评论抓取主入口。
        真人流程：商品详情页 → 点击 "See more reviews" → 下拉框筛选（排序/变体/星级）→ 翻页
        """
        task['asin'] = task['asin'].replace('\u200e', '')
        self.task = task
        self._asin_not_found = False
        asin = task['asin']
        self._set_active_review_asin(asin)
        country = task['country'].upper()

        self._all_reviews = []
        self._seen_review_ids = set()
        self._captured_responses = []
        self._reset_review_integrity_stats()
        self._expected_sort = ''
        self._expected_star_filter = ''
        # 默认只抓当前变体；显式 all_variants=true 才抓全部变体
        self._expected_is_variant = should_use_current_format_filter(task)
        self._retry_after_seconds = None

        # 日期截止条件
        self._date_cutoff = self._parse_date_cutoff(task.get('query_conditions'))
        self._date_cutoff_current_filter_hit = False
        self._date_cutoff_alert_sent = False
        if self._date_cutoff:
            self._log.info(f"[日期截止] 仅获取 {self._date_cutoff.strftime('%Y-%m-%d')} 之后的评论")
        use_recent_sort = should_use_recent_sort(task, self._date_cutoff)
        sort_by = 'recent' if use_recent_sort else ''
        self._expected_sort = sort_by

        # 每隔几个任务随机访问一次主页，模拟真人浏览
        self._task_count += 1
        if self._task_count % random.randint(4, 5) == 0:
            self._warmup_homepage(asin)

        # ---- 1. 直接访问评论页，检测 404 / 新 ASIN ----
        actual_asin = self._visit_reviews_page(asin, country, use_recent_sort)

        if actual_asin is None:
            self._log.error(f"[{asin}] 商品页无效，跳过")
            return []

        if actual_asin != asin:
            self._log.warning(f"[ASIN变更] {asin} → {actual_asin}")
            asin = actual_asin
            self._set_active_review_asin(actual_asin)
            task['asin'] = actual_asin


        # ---- 2. 检查评论页是否有评论 ----
        if self.review_counts == 0:
            self._log.info(f"[{asin}] 评论页无评论，跳过")
            return []

        # 已在评论页，无需再次导航
        if 'product-reviews' not in self.page.url:
            self._log.info("[流程] 当前不在评论页，重新导航")
            site = SITE_MAPPING[country]
            reviews_url = add_current_format_param(
                add_recent_sort_param(
                    f"https://{site}/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_srt?pageNumber=1",
                    use_recent_sort,
                ),
                should_use_current_format_filter(task),
            )
            self._captured_responses = []
            self.page.goto(
                reviews_url,
                wait_until="domcontentloaded", timeout=30000
            )
            self._human_delay(2.5, 0.4)
            self._handle_captcha(expected_url=reviews_url)
            self._handle_login_if_needed(reviews_url)
            self._simulate_human_behavior()

        # . 检测封号
        if self._check_blocked():
            self._log.error(f"[封号] 账号无法查看评论，停止任务: {getattr(self.account_info, 'username', '')}")
            raise Exception(f'账号无法查看评论，可能被封: {getattr(self.account_info, "username", "")}')

        # 清空旧的响应数据，准备接收新页面数据
        # self._captured_responses = []

        # ---- 3. 设置筛选条件 ----
        # current_format 已经在入口 URL 中控制；变体下拉框逻辑保留，但主流程不再依赖它。

        # 3a. 排序已在入口 URL 中确定：recent 带 sortBy=recent，默认排序不带 sortBy。

        # 3d. 解析当前页面数据（排序+变体设置后的最终结果），获取评论总数
        # ❗ 必须用 wait_for_timeout 而非 time.sleep，否则 _on_response 回调不会触发
        self.page.wait_for_timeout(2000)
        self._parse_captured_responses()

        total_reviews = self.review_counts
        if total_reviews < 0:
            for _ in range(2):
                self.page.wait_for_timeout(1200)
                self._parse_captured_responses()
                total_reviews = self.review_counts
                if total_reviews >= 0:
                    break

        if total_reviews < 0:
            self._log.warning("[评论数] 未解析到总数，刷新页面后重试解析")
            self._captured_responses = []
            self.page.reload(wait_until='domcontentloaded', timeout=60000)
            self.page.wait_for_timeout(2000)
            self._parse_captured_responses()
            total_reviews = self.review_counts

            if total_reviews < 0:
                raise Exception(f"评论总数解析失败: asin={asin}")

        is_variant = self._expected_is_variant
        self._log.info(f"[任务信息] 总评论数={total_reviews}, 是否变体={is_variant}, 排序={sort_by}")

        if total_reviews == 0:
            return []

        # ---- 4. 确定星级过滤策略 ----
        star_filters = []
        original_stars = []
        if task.get('query_conditions') and task['query_conditions'].get('stars'):
            stars = task['query_conditions']['stars']
            if isinstance(stars, int):
                stars = [stars]
            original_stars = [s for s in stars if s in [1, 2, 3, 4, 5]]
            star_filters = [self._star_filter_value(s) for s in original_stars if s]

        if not star_filters:
            star_filters = ['']

        total_expected_reviews = 0
        should_filter_by_star = set(original_stars) == {1, 2, 3, 4, 5}

        # ---- 5. 抓取评论 ----
        # 计算实际最大翻页数（与 _paginate_remaining 保持一致）
        task_max_pages = task.get('max_pages', 10)

        if should_filter_by_star and total_reviews <= 100:
            # 全星级但评论少，直接获取全部（不按星级拆分）
            self._log.info(f"[抓取策略] 评论数{total_reviews}<=100，直接全量抓取")
            self._date_cutoff_current_filter_hit = False
            if not self._check_page_date_cutoff():
                self._paginate_remaining('all', 0)
            if not self._date_cutoff:
                effective_pages = min(self.pages, task_max_pages) if self.pages > 0 else task_max_pages
                total_expected_reviews = min(total_reviews, effective_pages * 10)

        elif should_filter_by_star:
            # 全星级且评论多，按星级逐个筛选
            self._log.info(f"[抓取策略] 评论数{total_reviews}>100，按星级逐个筛选")
            self._all_reviews = []
            self._seen_review_ids = set()

            for filter_star in star_filters:
                star_label = filter_star.replace('_star', '') if filter_star else 'all'
                self._log.info(f"[星级切换] 开始抓取 {star_label} 星")
                self._date_cutoff_current_filter_hit = False
                star_start_count = len(self._all_reviews)

                self._select_star_filter(filter_star)
                time.sleep(2)
                self._parse_captured_responses()

                if self.review_counts == 0:
                    self._log.info(f"[星级切换] {star_label} 星无评论，跳过")
                    continue
                else:
                    self._log.info(f"[星级切换] {star_label} 评论数{self.review_counts}")

                if not self._date_cutoff:
                    effective_pages = min(self.pages, task_max_pages) if self.pages > 0 else task_max_pages
                    total_expected_reviews += min(self.review_counts, effective_pages * 10)

                if not self._check_page_date_cutoff():
                    self._paginate_remaining(star_label, star_start_count)
                self._log.info(f"[星级完成] {star_label}星 抓取完毕，累计={len(self._all_reviews)}条")

        else:
            # 非全星级模式（指定了部分星级 或 不筛选）
            for i, filter_star in enumerate(star_filters):
                star_label = filter_star.replace('_star', '') if filter_star else 'all'
                self._log.info(f"[星级切换] 开始抓取 {star_label} 星")
                self._date_cutoff_current_filter_hit = False
                star_start_count = 0 if not filter_star and i == 0 else len(self._all_reviews)

                if filter_star:
                    self._select_star_filter(filter_star)
                    time.sleep(2)
                    self._parse_captured_responses()
                elif i > 0:
                    self._select_star_filter('all_stars')
                    time.sleep(2)
                    self._parse_captured_responses()

                if self._check_blocked():
                    self._log.error(f"[封号] 星级{star_label}检测到封号，停止任务")
                    raise Exception(f'账号无法查看评论，可能被封: {getattr(self.account_info, "username", "")}')

                if self.review_counts == 0:
                    self._log.info(f"[星级切换] {star_label} 星无评论")
                    continue

                if not self._date_cutoff:
                    effective_pages = min(self.pages, task_max_pages) if self.pages > 0 else task_max_pages
                    total_expected_reviews += min(self.review_counts, effective_pages * 10)

                if not self._check_page_date_cutoff():
                    self._paginate_remaining(star_label, star_start_count)
                self._log.info(f"[星级完成] {star_label}星 抓取完毕，累计={len(self._all_reviews)}条")

        return self._finalize(total_expected_reviews)

    def _paginate_remaining(self, star_label: str = 'all', star_start_count: int = 0):
        """自动翻页直到最后一页，持续监听并解析响应"""
        max_pages = self.pages if self.pages > 0 else 10
        max_pages = min(self.task.get('max_pages', max_pages), max_pages)
        current_page = 1  # 首页已经在导航时获取
        consecutive_empty = 0  # 连续无新数据页数

        while current_page < max_pages:
            # 清空待解析队列
            self._captured_responses = []
            count_before = len(self._all_reviews)
            url_before = self.page.url if self.page else ""

            success = self._click_next_page()
            if not success:
                self._log.warning(
                    f"[翻页] 第{current_page+1}页点击失败: "
                    f"reason={getattr(self, '_last_pagination_failure_reason', '') or '-'}, "
                    f"url={url_before}"
                )
                self._validate_date_cutoff_short_page_completion(
                    current_page, max_pages, star_label, star_start_count
                )
                break

            # 等待新数据包被拦截（必须用 wait_for_timeout 保证事件派发）
            had_responses = self._wait_for_pagination_responses(timeout_ms=5000)
            self._parse_captured_responses()

            new_count = len(self._all_reviews) - count_before

            url_after = self.page.url if self.page else ""
            if new_count == 0 and url_after != url_before and not had_responses:
                try:
                    html = self.page.content()
                    reviews = self._parse_single_response({
                        'resp': html,
                        'url': url_after,
                        'is_ajax': False,
                        'is_first_page': True,
                    })
                    if reviews:
                        self._all_reviews.extend(reviews)
                        new_count = len(self._all_reviews) - count_before
                        self._log.info(f"[翻页] document响应未捕获，改用当前页面HTML解析，新增={new_count}条")
                except Exception as e:
                    self._log.warning(f"[翻页] 当前页面HTML兜底解析失败: {e}")

            if new_count == 0 and url_after == url_before:
                # 无响应或全重复都可能是点击未生效/代理慢/Amazon返回同页，重试一次。
                self._log.warning(
                    f"[翻页] 第{current_page+1}页无新数据，重试一次。"
                    f"had_responses={had_responses}, url_changed={url_after != url_before}, "
                    f"url={url_after}"
                )
                self.page.wait_for_timeout(random.randint(2000, 4000))
                self._captured_responses = []
                retry_ok = self._click_next_page()
                if retry_ok:
                    had_responses = self._wait_for_pagination_responses(timeout_ms=6000)
                    self._parse_captured_responses()
                    new_count = len(self._all_reviews) - count_before
                else:
                    self._log.warning(
                        f"[翻页] 第{current_page+1}页重试点击失败: "
                        f"reason={getattr(self, '_last_pagination_failure_reason', '') or '-'}"
                    )

                url_after_retry = self.page.url if self.page else ""
                if new_count == 0 and url_after_retry == url_before:
                    self._captured_responses = []
                    fallback_entry = self._fetch_show_more_ajax_fallback()
                    if fallback_entry:
                        reviews = self._parse_single_response(fallback_entry)
                        if reviews:
                            self._all_reviews.extend(reviews)
                            new_count = len(self._all_reviews) - count_before
                        self._log.info(f"[翻页] show-more AJAX 兜底完成，新增={new_count}条")
                    else:
                        self._log.warning(
                            f"[翻页] 第{current_page+1}页show-more AJAX兜底失败: "
                            f"reason={getattr(self, '_last_pagination_failure_reason', '') or '-'}"
                        )
            elif new_count == 0:
                self._log.warning(
                    f"[翻页] 第{current_page+1}页URL已变化但无新数据，不重复点击以避免跳页。"
                    f"had_responses={had_responses}, url={url_after}"
                )

            current_page += 1
            self._log.info(f"[翻页] 第{current_page}页完成，新增={new_count}条，累计={len(self._all_reviews)}条")

            # 日期截止检查：若末尾评论早于截止日期则过滤并停止
            if new_count > 0 and self._check_page_date_cutoff():
                break

            if new_count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    self._log.warning(f"[翻页限流] 连续{consecutive_empty}页无新数据，停止翻页")
                    self._validate_date_cutoff_short_page_completion(
                        current_page, max_pages, star_label, star_start_count
                    )
                    break
                self._log.warning(f"[翻页] 本页无新数据，等待后继续")
                time.sleep(random.uniform(5.0, 8.0))
            else:
                consecutive_empty = 0

        self._validate_date_cutoff_short_page_completion(
            current_page, max_pages, star_label, star_start_count
        )
        self._alert_date_cutoff_not_reached(current_page, star_label, star_start_count)
        self._record_review_page_completion(current_page, max_pages)

    def _finalize(self, total_expected: int) -> List[dict]:
        """完整性检查 + 返回结果"""
        actual = len(self._all_reviews)

        if getattr(self, '_date_cutoff', None):
            self._log.info(f"[任务完成] 日期截止模式，共抓取={actual}条，跳过完整性校验")
            return self._all_reviews

        if total_expected > 0 and actual != total_expected:
            self._log.warning(
                f"[完整性] 实际={actual}, 预期={total_expected}, "
                f"raw_blocks={getattr(self, '_raw_review_slots_count', 0)}, "
                f"duplicate={getattr(self, '_duplicate_review_slots_count', 0)}, "
                f"invalid={getattr(self, '_invalid_review_slots_count', 0)}, "
                f"pages={getattr(self, '_review_expected_pages_seen', 0)}/"
                f"{getattr(self, '_review_expected_pages_total', 0)}"
            )

        if total_expected and (actual / total_expected) * 100 < 95:
            diff = total_expected - actual
            raw_slots = int(getattr(self, '_raw_review_slots_count', 0) or 0)
            duplicate_slots = int(getattr(self, '_duplicate_review_slots_count', 0) or 0)
            invalid_slots = int(getattr(self, '_invalid_review_slots_count', 0) or 0)
            pages_complete = (
                bool(getattr(self, '_review_page_completion_ok', False))
                and int(getattr(self, '_review_expected_pages_total', 0) or 0) > 0
                and int(getattr(self, '_review_expected_pages_seen', 0) or 0)
                >= int(getattr(self, '_review_expected_pages_total', 0) or 0)
            )
            if pages_complete and raw_slots > 0 and raw_slots < total_expected and invalid_slots == 0:
                self._log.warning(
                    f"[完整性] 已走完分页，但 Amazon 实际返回评论块少于展示数量，按实际可获取数据完成。"
                    f"唯一={actual}, 原始块={raw_slots}, 重复={duplicate_slots}, "
                    f"invalid={invalid_slots}, 预期={total_expected}"
                )
                self._log.info(f"[任务完成] 共抓取={actual}条唯一评论，预期={total_expected}条")
                return self._all_reviews
            if (
                    raw_slots > 0
                    and raw_slots / total_expected >= 0.95
                    and duplicate_slots >= diff
            ):
                self._log.warning(
                    f"[完整性] 唯一评论数不足但原始评论槽位已达标，判定为重复评论导致。"
                    f"唯一={actual}, 原始块={raw_slots}, 重复={duplicate_slots}, "
                    f"invalid={invalid_slots}, 预期={total_expected}"
                )
                self._log.info(f"[任务完成] 共抓取={actual}条唯一评论，预期={total_expected}条")
                return self._all_reviews
            if diff > 10:
                self._log.error(f"[数量不足] 实际={actual}, 预期={total_expected}, 差值={diff}，触发重试")
                raise Exception(
                    f'评论抓取数量不足，触发重试：实际={actual}, 预期={total_expected}, 差值={diff}'
                )
            else:
                self._log.warning(f"[数量偏差] 实际={actual}, 预期={total_expected}，差异可接受")
                send_custom_robot_group_message(
                    f'数据有部分丢失,差异不大 {actual}--{total_expected}--{self.task}',
                    at_mobiles=['17398238551']
                )

        self._log.info(f"[任务完成] 共抓取={actual}条评论，预期={total_expected}条")
        return self._all_reviews

    # ========================== 带重试的主入口 ==========================

    MAX_ACCOUNT_SWITCHES = 2  # 单个任务最多切换账号次数

    def _get_redis(self):
        """懒加载 Redis 客户端（用于事件日志，不影响主流程）"""
        if not hasattr(self, '_redis_client') or self._redis_client is None:
            try:
                import redis as redis_lib
                from app.crawlers.amazon_crawler.shuler.util.config import (
                    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
                )
                self._redis_client = redis_lib.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    username=REDIS_USERNAME, password=REDIS_PASSWORD,
                    db=REDIS_DB, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
            except Exception:
                self._redis_client = None
        return self._redis_client

    # Redis channel：其他进程订阅此频道，收到消息时立即停用对应账号
    _ACCOUNT_BAN_CHANNEL = "crawler:account_banned"

    def _mark_account_unusable(self, reason: str = ""):
        """标记当前账号不可用（state=-1）并持久化，同时广播到 Redis 让其他进程感知"""
        try:
            if not self.account_info:
                return
            username = getattr(self.account_info, 'username', '')
            self.account_info.state = -1
            account_mgr = AccountManager()
            account_mgr._save_account(self.account_info)
            logger.warning(f"[账号状态] 已标记不可用 state=-1, username={username}, reason={reason}")
            # 广播封号事件：写入 Redis SET + 发布到 pubsub channel，让其他进程快速感知
            # 其他进程在 _account_ok 里检查这个 SET，即使不订阅 channel 也能感知
            try:
                _redis = self._get_redis()
                if _redis:
                    _redis.set(f'crawler:banned:{username}', '1', ex=86400)
                    _redis.publish(self._ACCOUNT_BAN_CHANNEL, username)
            except Exception:
                pass
        except Exception:
            logger.warning(f"[账号状态] 标记不可用失败: {traceback.format_exc()}")

    def run(self, task: dict, worker_id=None, account_manager=None) -> List[dict]:
        """
        执行单个任务（支持 session 内浏览器复用 + 账号异常自动切换）。

        浏览器生命周期：
          - 首次调用或上次异常关闭后 → 自动启动浏览器
          - 任务成功 → 浏览器保持打开（下次任务复用）
          - 任务异常 → 关闭浏览器（下次重试自动重新打开）
          - 账号异常 + 传入 account_manager → 自动切换新账号重试
          - session 结束 → 调用方调用 close_session() 关闭浏览器 + BitBrowser 窗口

        :param task: 任务字典
        :param worker_id: 工作进程标识
        :param account_manager: AccountManager 实例（传入后支持自动换号重试）
        """
        self.worker_id = worker_id
        retry_left = RETRY_TIMES
        account_switches = 0
        last_error = None
        attempt = 0
        _log_start_time = datetime.now()
        asin = task.get('asin', '')
        country = task.get('country', '')
        username = getattr(self.account_info, 'username', '')
        _redis = self._get_redis()

        # 绑定上下文日志（同时赋给 self._log，供内部方法使用）
        _log = logger.bind(worker=str(worker_id or ''), account=username,
                           country=country, asin=asin, ip=self._get_proxy_ip())
        self._log = _log

        # 发射任务开始事件
        try:
            push_event(_redis, EventType.TASK_START,
                       username=username, asin=asin, country=country,
                       worker_id=str(worker_id or ''))
        except Exception:
            pass

        while retry_left >= 0:
            try:
                # 浏览器未启动（首次 / 上次异常后被关闭）→ 自动启动
                # 同时检测浏览器是否被人为关闭，若断开则清理后重新启动
                if self.page and not self._is_browser_alive():
                    _log.warning(f"[浏览器检测] 浏览器被外部关闭，尝试重新打开 "
                                 f"(账号={username})")
                    self._close_browser()

                if not self.page:
                    try:
                        self._start_browser()
                    except Exception as start_exc:
                        err_msg = (f"浏览器启动失败: 账号={username}, "
                                   f"fingerprint={self.fingerprint_id}, error={start_exc}")
                        _log.error(err_msg)
                        send_custom_robot_group_message(
                            f'[告警] {err_msg}',
                            at_mobiles=['17398238551'])
                        raise

                _log.info(
                    f"进程{worker_id}：Playwright 执行 ASIN[{asin}]，{country}"
                    f"账号[{username}]，剩余重试{retry_left}"
                )

                reviews = self.get_reviews(task)
                review_count = len(reviews)
                # 估算页面数：平均每页 10 条评论，向上取整
                pages_fetched = max(1, (review_count + 9) // 10)

                self._record_usage_log(task, _log_start_time, success=True,
                                       review_count=review_count, worker_id=worker_id)
                # 发射任务成功事件
                try:
                    push_event(_redis, EventType.TASK_SUCCESS,
                               username=username, asin=asin, country=country,
                               worker_id=str(worker_id or ''),
                               extra={"review_count": review_count, "pages_fetched": pages_fetched,
                                      "retry_count": RETRY_TIMES - retry_left})
                except Exception:
                    pass

                try:
                    reset_account_error(username, _redis)
                except Exception:
                    pass
                # InfluxDB 上报账号活跃状态
                try:
                    _rpt = get_reporter()
                    if _rpt:
                        _rpt.account.report_status(
                            account_id=username, site=country, status="active")
                except Exception:
                    pass
                self._release_page_memory_after_task()
                return reviews

            except Exception as exc:
                last_error = exc
                trace_text = traceback.format_exc()
                retry_left -= 1
                attempt += 1
                _log.error(f"任务执行失败，剩余重试{retry_left}：{trace_text}")

                self._close_browser()

                is_login_failure = '登录失败' in trace_text
                is_ban_signal = '可能被封' in trace_text

                # 指纹配置不存在 → 账号已停用，等同登录失败，触发换号而非无效重试
                from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import ProfileNotFoundError as _ProfileNotFoundError
                if isinstance(exc, _ProfileNotFoundError):
                    is_login_failure = True

                is_captcha = 'captcha' in trace_text.lower() or 'robot' in trace_text.lower()

                _err_str = str(exc)
                # _goto_with_retry 耗尽重试后抛 NetworkException，直接 isinstance 判断
                is_network_error = isinstance(exc, NetworkException)

                # 网络错误且本轮重试已耗尽 → 代理可能绑定在当前账号/指纹浏览器上，
                # 换账号比继续等待更有效
                if is_network_error and retry_left < 0 and account_manager and account_switches < self.MAX_ACCOUNT_SWITCHES:
                    is_login_failure = True

                is_account_error = is_login_failure or is_ban_signal

                if is_ban_signal:
                    self._mark_account_unusable('可能被封')

                # 发射风控相关事件 + InfluxDB 上报
                try:
                    if is_network_error:
                        # 网络异常：不计入账号风控，统计全局并发数
                        net_err_count = increment_network_error(_redis)
                        _log.warning(f"[网络异常] worker={worker_id} 全局并发网络异常数={net_err_count}: {_err_str[:200]}")
                        if net_err_count >= NETWORK_ERR_ALERT_THRESHOLD:
                            if should_alert_network_error(_redis):
                                mark_network_alert_sent(_redis)
                                send_custom_robot_group_message(
                                    f"[网络异常告警] 5分钟内 {net_err_count} 个进程出现网络错误，"
                                    f"可能需要人工检查网络/代理。最近错误: {_err_str[:300]}",
                                    at_mobiles=['17398238551']
                                )
                    elif not is_login_failure:
                        # 封号/业务异常：计入账号异常计数；登录失败不计入
                        increment_account_error(username, _redis)

                    _rpt = get_reporter()
                    if is_network_error:
                        # 网络/代理错误不推封号事件，避免污染风控统计
                        push_event(_redis, EventType.RETRY,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''),
                                   error_msg=str(exc)[:300],
                                   extra={"attempt": attempt, "retry_left": retry_left,
                                          "error_type": "network"})
                    elif is_ban_signal:
                        push_event(_redis, EventType.ACCOUNT_BANNED,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''), error_msg=str(exc)[:500])
                        if _rpt:
                            _rpt.account.report_ban(
                                account_id=username, site=country, reason=BanReason.ACCOUNT_BLOCKED)
                    elif is_login_failure:
                        push_event(_redis, EventType.RETRY,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''),
                                   error_msg=f"[登录失败] {str(exc)[:300]}",
                                   extra={"attempt": attempt, "retry_left": retry_left})
                        if _rpt:
                            _rpt.account.report_ban(
                                account_id=username, site=country, reason=BanReason.LOGIN_FAILED)
                    elif is_captcha:
                        push_event(_redis, EventType.CAPTCHA_HIT,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''))
                        if _rpt:
                            _rpt.account.report_ban(
                                account_id=username, site=country, reason=BanReason.CAPTCHA)
                    else:
                        push_event(_redis, EventType.RETRY,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''),
                                   error_msg=str(exc)[:300],
                                   extra={"attempt": attempt, "retry_left": retry_left})
                except Exception:
                    pass

                if is_account_error:
                    if account_manager and account_switches < self.MAX_ACCOUNT_SWITCHES:
                        # 有 account_manager 且还有切换次数 → 换号重试
                        account_switches += 1
                        failed_username = username
                        _log.warning(
                            f"进程{worker_id}: 账号{failed_username}{'登录失败' if is_login_failure else '异常'}，"
                            f"切换新账号重试 ({account_switches}/{self.MAX_ACCOUNT_SWITCHES})")
                        _alert_prefix = '[登录失败告警]' if is_login_failure else '[账号异常已停用]'
                        send_custom_robot_group_message(
                            f'{_alert_prefix} 账号: {failed_username} ，{trace_text[:300]}, '
                            f'ASIN={asin}, '
                            f'切换新账号重试({account_switches}/{self.MAX_ACCOUNT_SWITCHES})',
                            at_mobiles=['17398238551'])
                        if self._switch_account(account_manager, task):
                            username = getattr(self.account_info, 'username', '')
                            _log = logger.bind(worker=str(worker_id or ''), account=username,
                                               country=country, asin=asin, ip='')
                            retry_left = RETRY_TIMES  # 新账号重置重试次数
                            attempt = 0
                            # 换号后用指数退避（模拟重新登录后的思考时间）
                            time.sleep(random.uniform(3.0, 6.0))
                            continue
                        # 换号失败 → 抛出
                    self._record_usage_log(task, _log_start_time, success=False,
                                           retry_count=RETRY_TIMES - retry_left,
                                           error_msg=last_error, worker_id=worker_id)
                    raise

                if retry_left >= 0:
                    # 优先使用服务器指定的 Retry-After 等待时长
                    if self._retry_after_seconds is not None:
                        backoff = float(self._retry_after_seconds) + random.uniform(1, 5)
                        _log.warning(f"[429] 遵从 Retry-After，等待 {backoff:.1f}s")
                        self._retry_after_seconds = None
                    elif is_network_error:
                        # 网络异常退避：根据并发异常数动态加长等待
                        try:
                            net_count = get_network_error_count(_redis)
                        except Exception:
                            net_count = 1
                        if net_count >= NETWORK_ERR_MULTI_THRESHOLD:
                            backoff = min(30.0 * net_count, 300.0)
                            _log.warning(
                                f"[网络异常] 并发异常数={net_count}，延长等待 {backoff:.0f}s"
                            )
                        else:
                            backoff = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                        backoff += random.uniform(0, backoff * 0.2)
                    else:
                        # 指数退避
                        backoff = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                        backoff += random.uniform(0, backoff * 0.3)
                    _log.info(f"重试退避等待 {backoff:.1f}s (attempt={attempt}, network={is_network_error})")
                    time.sleep(backoff)

        self._record_usage_log(task, _log_start_time, success=False,
                               retry_count=RETRY_TIMES, error_msg=last_error, worker_id=worker_id)
        # 发射任务失败事件
        try:
            push_event(_redis, EventType.TASK_FAILED,
                       username=username, asin=asin, country=country,
                       worker_id=str(worker_id or ''), error_msg=str(last_error)[:500])
        except Exception:
            pass
        send_custom_robot_group_message(
            f'Playwright 任务重试耗尽: {task}-进程{worker_id}-{last_error}',
            at_mobiles=['17398238551']
        )
        raise Exception(f"Playwright 任务重试耗尽: {task}, last_error={last_error}")

    def _switch_account(self, account_manager, task: dict) -> bool:
        """
        切换到新账号：关闭当前会话 → 释放账号 → 获取新账号 → 更新 self 状态。
        :return: True=切换成功, False=无可用账号
        """
        try:
            old_username = getattr(self.account_info, 'username', '')
            # 关闭当前浏览器 + BitBrowser 窗口
            self.close_session()
            # 释放当前账号
            try:
                account_manager.force_release()
            except Exception:
                pass
            # 写 cooldown_until 到 MySQL，让其他进程也跳过这个账号 30 分钟
            try:
                if self.account_info:
                    self.account_info.cooldown_until = time.time() + 30 * 60
                    account_manager._save_account(self.account_info)
            except Exception:
                pass

            # 获取新账号
            country = task.get('country', 'US')
            new_account = account_manager.get_account({'country': country})
            if not new_account:
                logger.error(f"[换号] 无可用账号，切换失败")
                return False

            # 更新 scraper 状态
            self.account_info = new_account
            self.fingerprint_id = getattr(new_account, 'fingerprint_id', None)
            logger.info(f"[换号] 账号切换成功: {old_username} → {new_account.username}")
            return True

        except Exception as e:
            logger.error(f"[换号] 切换账号异常: {e}")
            return False

    def close_session(self):
        """
        会话结束：断开浏览器连接 + 关闭 BitBrowser 指纹浏览器窗口。
        由调用方在账号 session 结束时调用（不是每个任务后调用）。
        """
        self._close_browser()
        self._quit_fingerprint_browser()
        logger.info(f"[会话结束] 浏览器已关闭: {getattr(self.account_info, 'username', '')}")

    def _record_usage_log(self, task, start_time, success, review_count=0,
                          retry_count=0, error_msg="", worker_id=None):
        try:
            end_time = datetime.now()
            db = MySQLTaskDB()
            db.insert_usage_log(
                task_id=str(task.get('task_id', '')) if str(task.get('task_id', '')) else str(task.get('id', '')),
                asin=task.get('asin', ''),
                country=task.get('country', ''),
                username=getattr(self.account_info, 'username', ''),
                success=success,
                review_count=review_count,
                start_time=start_time.strftime('%Y-%m-%d %H:%M:%S'),
                end_time=end_time.strftime('%Y-%m-%d %H:%M:%S'),
                duration_seconds=int((end_time - start_time).total_seconds()),
                retry_count=retry_count,
                error_msg=str(error_msg)[:2000] if error_msg else '',
                worker_id=str(worker_id or ''),
                ip=self._get_proxy_ip(),
                task_type=task.get('task_type', 'review'),
            )
            db.close()
        except Exception:
            logger.warning(f"insert_usage_log failed: {traceback.format_exc()}")


def _require_debug_account(account, task):
    """直接运行本文件调试时，无账号就停止，避免空账号启动浏览器。"""
    if account:
        return account
    country = str((task or {}).get("country") or "").upper() or "-"
    diagnostics = _debug_account_availability(task)
    raise SystemExit(
        f"无可用账号 country={country}。请检查当前 APP_ENV/SC_ENV_FILE、"
        "amazon_crawler_accounts 表状态，以及 Redis 日统计/休息状态。"
        f"\n{diagnostics}"
    )


def _debug_account_availability(task):
    """直接运行调试用：输出无账号时的安全诊断信息，不打印密码。"""
    try:
        from datetime import datetime
        from urllib.parse import parse_qs, urlparse

        import redis as redis_lib

        from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB

        country = str((task or {}).get("country") or "").upper() or ""
        database_url = os.getenv("DATABASE_URL", "")
        redis_url = os.getenv("AMAZON_VOC_REDIS_URL") or os.getenv("REDIS_URL") or ""
        db_parsed = urlparse(database_url)
        redis_parsed = urlparse(redis_url)
        db_query = parse_qs(db_parsed.query)
        db_options = (db_query.get("options") or [""])[0]
        redis_db = redis_parsed.path.strip("/") if redis_parsed.path else ""

        lines = [
            "[调试诊断]",
            f"DB={db_parsed.scheme}://{db_parsed.hostname}:{db_parsed.port or '-'}"
            f"/{db_parsed.path.strip('/') or '-'} options={db_options or '-'}",
            f"Redis={redis_parsed.hostname}:{redis_parsed.port or '-'} db={redis_db or '0'}",
        ]

        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        db = MySQLTaskDB()
        rows = db.load_available_account_candidates(
            {"country": country, "platform": "amazon"},
            now_ts=now,
            limit=10,
        )
        lines.append(f"基础候选={len(rows)} country={country}")

        redis_client = None
        if redis_url:
            try:
                redis_client = redis_lib.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_timeout=5,
                )
            except Exception as exc:
                lines.append(f"Redis连接初始化失败={exc}")

        for row in rows[:5]:
            username = str(row.get("username") or "")
            stat = {}
            banned = None
            if redis_client:
                try:
                    banned = bool(redis_client.exists(f"crawler:banned:{username}"))
                    stat = redis_client.hgetall(f"acc_day:{username}:{today}") or {}
                except Exception as exc:
                    stat = {"redis_error": str(exc)}
            lines.append(
                "候选 "
                f"username={username} state={row.get('state')} is_used={row.get('is_used')} "
                f"cooldown_until={row.get('cooldown_until')} banned={banned} "
                f"rest_until={stat.get('rest_until', '-')} "
                f"task={stat.get('task_count', '-')}/{stat.get('daily_budget', '-')} "
                f"page={stat.get('page_count', '-')}/{stat.get('daily_page_budget', '-')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[调试诊断] 获取失败: {exc}"


# ========================== 直接运行测试 ==========================
if __name__ == '__main__':
    from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager as AccountManager

    task = {
        'asin': 'B0DQBJ75KV',
        'country': 'US',
        'id': '111111',
        'max_pages': 3,
        'query_conditions':{}

    }

    account_manager = AccountManager(worker_id='test')

    account = _require_debug_account(account_manager.get_account({'country': task['country']}), task)
    scraper = PlaywrightReviewScraper(account_info=account, task=task)

    result = scraper.run(task)
    print(f"\n总共抓取: {len(result)} 条评论")
    print(json.dumps(result[:3], indent=2, ensure_ascii=False))
    scraper.close_session()
    task = {
        'asin': 'B0BBQX7P5J',
        'country': 'JP',
        'id': '111111',
        'query_conditions': {'date_from': '2024-05-01', 'sort_by': 'top_reviews'},
    }
    account1 = _require_debug_account(account_manager.get_account({'country': task['country']}), task)
    if account1 !=account:
        # scraper.close_session()
        scraper = PlaywrightReviewScraper(account_info=account1, task=task)
    result = scraper.run(task)
    print(f"\n总共抓取: {len(result)} 条评论")
    print(json.dumps(result[:3], indent=2, ensure_ascii=False))
    scraper.close_session()
