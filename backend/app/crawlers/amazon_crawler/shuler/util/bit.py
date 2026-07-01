"""
指纹浏览器 - 使用工厂模式优化后的实现
pip install DrissionPage
pip install selenium==3.141.0
pip install --upgrade urllib3==1.26.16
"""
import re
import time
import math
import random
import requests
import socket
import traceback
import os
from typing import List, Tuple, Optional, Dict, Any, Union, Type
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from abc import ABC, abstractmethod
# from app.core.config import settings
from DrissionPage import WebPage, ChromiumOptions
from DrissionPage.common import Actions, Keys
from DrissionPage.errors import ElementNotFoundError
from DrissionPage import Chromium

# 服务地址 - 支持从环境变量读取（Docker 容器内可访问宿主机）
IP = os.getenv('BIT_BROWSER_IP', '127.0.0.1')
PORT = os.getenv('BIT_BROWSER_PORT', '6873')
# TOKEN = settings.VM_TOKEN


def wait_for_debugger(ip: str, port: str, timeout: float = 10.0, interval: float = 0.2) -> bool:
    url = f"http://{ip}:{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=1.0)
            if r.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(interval)
    return False


def generate_human_like_trajectory(a: float, b: float, steps: int = 30) -> List[Tuple[float, float]]:
    """
    生成类似人类的移动轨迹。

    Args:
        a: 起始点x坐标
        b: 终点x坐标
        steps: 轨迹中的步数

    Returns:
        包含(x, y)坐标的轨迹列表
    """
    trajectory = []
    x_distance = b - a
    last_y = 0

    # 生成x轴上的移动轨迹
    for i in range(steps):
        # 使用缓动函数生成x轴上的位移
        t = float(i) / steps
        ease_step = t ** 2 * (3 - 2 * t)  # 使用二次缓动函数（Quadratic Ease Out）

        # 计算当前步的x坐标
        x = a + ease_step * x_distance

        # 生成y轴上的微小随机偏移，模拟人手抖动
        y = last_y + random.uniform(-0.5, 0.5)

        # 添加当前步的坐标到轨迹列表
        trajectory.append((x, y))
        last_y = y

    # 确保最后一个点是准确的终点
    trajectory.append((b, last_y))

    return trajectory


def is_port_in_use(host, port):
    """
    检查指定主机的端口是否正在被监听。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect((host, port))
            return True  # 端口已开放
        except (socket.timeout, ConnectionRefusedError):
            return False  # 端口未开放或被拒绝


class FingerprintBrowserBase(ABC):
    """指纹浏览器基类 - 抽象基类"""

    def __init__(
            self,
            webdriver_path: Optional[str] = None,
            debugging_port: Optional[str] = None,
            user_agent: Optional[str] = None,
            is_init_selenium: bool = True,
            is_init_drission: bool = True,
            is_init_selenium_wire: bool = True
    ):
        """
        初始化指纹浏览器基类

        Args:
            webdriver_path: webdriver路径
            debugging_port: 调试端口
            user_agent: 用户代理
            is_init_selenium: 是否初始化selenium
            is_init_drission: 是否初始化DrissionPage
            is_init_selenium_wire: 是否初始化selenium-wire
        """
        self.webdriver_path = webdriver_path
        self.debugging_port = debugging_port
        self.user_agent = user_agent
        self.driver: Optional[webdriver.Chrome] = None  # selenium对象
        self.driver_wire: Optional[webdriver.Chrome] = None  # seleniumwire框架对象
        self.page: Optional[WebPage] = None  # WebPage对象 操作浏览器
        self.browser: Optional[Chromium] = None  # Actions对象 可以控制所有（控制浏览器自动化、接口发包、鼠标键盘）
        self.is_init_selenium = is_init_selenium
        self.is_init_drission = is_init_drission
        self.is_init_selenium_wire = is_init_selenium_wire
        self.chrome_version = None

    @abstractmethod
    def start_fingerprint(self, code: str) -> bool:
        """
        启动指纹浏览器

        Args:
            code: 指纹标识码

        Returns:
            启动是否成功
        """
        pass

    @abstractmethod
    def quit_fingerprint(self, code: str) -> bool:
        """
        关闭指纹浏览器

        Args:
            code: 指纹标识码

        Returns:
            关闭是否成功
        """
        pass

    def quit_other_window_handles(self) -> None:
        """关闭除当前窗口外的所有标签页"""
        try:
            if not self.driver:
                print("driver未初始化，无法关闭其他窗口")
                return

            # 获取当前窗口句柄（窗口A）
            handle = self.driver.current_window_handle
            # 获取当前所有窗口句柄（窗口A、B）
            handles = self.driver.window_handles
            # 对窗口进行遍历
            for new_handle in handles:
                # 筛选新打开的窗口B
                if new_handle != handle:
                    # 切换到新打开的窗口B
                    self.driver.switch_to.window(new_handle)
                    # 关闭当前窗口B
                    self.driver.close()
            # 切换回窗口A
            self.driver.switch_to.window(handle)
        except Exception:
            print(f'quit_other_window_handles 失败:{traceback.format_exc()}')

    def get_seleniumwire_driver(self) -> None:
        """初始化seleniumwire driver"""
        try:
            from seleniumwire.webdriver import Chrome
            from seleniumwire.webdriver import ChromeOptions

            if not wait_for_debugger(IP, self.debugging_port, timeout=150):
                raise TimeoutError(f"等待 Chrome DevTools {IP}:{self.debugging_port} 就绪超时")

            # 获取webdriver
            options = ChromeOptions()
            options.add_argument("disable-blink-features=AutomationControlled")  # 去掉webdriver痕迹
            options.add_experimental_option("debuggerAddress", f'{IP}:{self.debugging_port}')
            self.driver_wire = Chrome(self.webdriver_path, options=options)
            self.user_agent = self.driver_wire.execute_script("return navigator.userAgent;")
        except ImportError:
            print("seleniumwire未安装，请使用pip install selenium-wire安装")
        except Exception as e:
            print(f"初始化seleniumwire driver失败: {str(e)}")

    def get_selenium_driver(self) -> None:
        """初始化selenium driver"""
        port_to_check = int(self.debugging_port)
        timeout_seconds = 30  # 总等待超时时间
        check_interval = 1  # 每秒检查一次

        start_time = time.time()
        port_ready = False
        while time.time() - start_time < timeout_seconds:
            print(f"线程 {port_to_check}: 正在检查端口 {IP}:{port_to_check} 是否就绪...")
            if is_port_in_use(IP, port_to_check):
                print(f"线程 {port_to_check}: 端口已就绪！")
                port_ready = True
                break
            time.sleep(check_interval)

        if port_ready:
            try:
                options = webdriver.ChromeOptions()
                options.add_argument("disable-blink-features=AutomationControlled")
                options.add_experimental_option("debuggerAddress", f'{IP}:{port_to_check}')
                service = Service(executable_path=self.webdriver_path)

                self.driver = webdriver.Chrome(service=service, options=options)
                # self.user_agent = self.driver.execute_script("return navigator.userAgent;")
                print(f"线程 {port_to_check}: WebDriver连接成功！")
            except Exception as e:
                print(f"线程 {port_to_check}: 端口虽然开放，但WebDriver连接失败: {e}")
                # 这里可能出现端口开放了，但浏览器内部服务还没完全准备好的情况，可以再加一层简单的重试
                raise
        else:
            print(f"线程 {port_to_check}: 在 {timeout_seconds} 秒内端口未就绪，任务失败。")
            raise ConnectionError(f"等待端口 {IP}:{port_to_check} 超时")

    def get_drission_driver(self) -> None:
        """初始化DrissionPage"""
        try:
            co = ChromiumOptions()
            co.set_browser_path(self.webdriver_path)
            co.set_address(f'{IP}:{self.debugging_port}')
            co.existing_only(True)
            self.browser = Chromium(addr_or_opts=co)
            self.page = self.browser.latest_tab  # 获取最新打开的标签页
            self.browser.activate_tab(self.page)  # 使一个标签页显示到前端
            # self.ac = Actions(self.page)
            self.user_agent = self.page.run_js("return navigator.userAgent;")
            return True
        except Exception as e:
            print(f"初始化DrissionPage失败: {str(e)}")

    def start(self, code: str, ip: Optional[str] = None, post: Optional[str] = None) -> bool:
        """
        启动指纹并获取selenium-DrissionPage-seleniumwire对象

        Args:
            code: 指纹标识码
            ip: 服务IP地址
            post: 服务端口

        Returns:
            启动是否成功
        """
        global IP, PORT
        if ip:
            IP = ip
        if post:
            PORT = post

        result = self.start_fingerprint(code=code)
        if not result:
            return False

        if self.is_init_drission:
            if not self.get_drission_driver():
                return False
        if self.is_init_selenium:
            if not self.get_selenium_driver():
                return False
        if self.is_init_selenium_wire:
            if not self.get_seleniumwire_driver():
                return False

        return True

    def ac_input(self, xpath: str, text: str, offset_x: Optional[int] = None, offset_y: Optional[int] = None) -> None:
        """
        模拟输入

        Args:
            xpath: 元素xpath
            text: 输入文本
            offset_x: x偏移量
            offset_y: y偏移量
        """
        try:
            if not self.ac or not self.page:
                print("DrissionPage未初始化，无法执行模拟输入")
                return

            self.ac_move_click(xpath=xpath, offset_x=offset_x, offset_y=offset_y)
            time.sleep(random.uniform(0.5, 1.5))  # 更自然的延迟
            self.page.ele(xpath).input(text)
        except Exception as e:
            print(f"模拟输入失败: {str(e)}")

    def ac_move_click(self, xpath: str, offset_x: Optional[int] = None, offset_y: Optional[int] = None) -> None:
        """
        模拟真实移动并点击

        Args:
            xpath: 元素xpath
            offset_x: x偏移量
            offset_y: y偏移量
        """
        try:
            if not self.ac or not self.page:
                print("DrissionPage未初始化，无法执行模拟点击")
                return

            ele = self.page.ele(xpath)
            self.ac.move_to(ele_or_loc=ele, offset_x=offset_x, offset_y=offset_y)
            time.sleep(random.uniform(0.3, 0.8))  # 更自然的延迟
            self.ac.click(ele)
        except ElementNotFoundError:
            print(f"未找到元素: {xpath}")
        except Exception as e:
            print(f"模拟点击失败: {str(e)}")

    def create_browser(self, group_id: str = "", name: str = "", args: List = None, queue: bool = False) -> Dict:
        """
        创建浏览器窗口 (可选实现)

        Args:
            group_id: 窗口所属组的ID
            name: 窗口名称
            args: 浏览器启动参数
            queue: 是否以队列方式创建

        Returns:
            创建结果信息
        """
        print(f"当前浏览器类型不支持创建新窗口")
        return None

    def close_all_windows(self) -> Dict:
        """
        关闭所有浏览器窗口 (可选实现)

        Returns:
            关闭结果信息
        """
        print(f"当前浏览器类型不支持批量关闭窗口")
        return None

    def get_browser_detail(self,code: str) -> Dict:
        """
        获取浏览器的窗口信息
        :return:
        """
        return None

class HubStudio(FingerprintBrowserBase):
    """HubStudio指纹浏览器实现类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def start_fingerprint(self, code: str) -> bool:
        """
        启动HubStudio浏览器

        Args:
            code: 指纹标识码

        Returns:
            启动是否成功
        """
        url = f'http://{IP}:{PORT}/api/v1/browser/start'  # 打开环境
        open_data = {"containerCode": code}  # 填写参数

        try:
            open_res = requests.post(url, json=open_data, timeout=30).json()

            if open_res['code'] != 0:
                print(f'环境打开失败: {open_res}')
                return False

            self.webdriver_path = open_res['data']['webdriver']  # 获取webdriver路径
            self.debugging_port = open_res['data']['debuggingPort']  # 获取调试端口
            return True
        except Exception as e:
            print(f"启动HubStudio浏览器失败: {str(e)}")
            return False

    def quit_fingerprint(self, code: str) -> bool:
        """
        关闭HubStudio浏览器

        Args:
            code: 指纹标识码

        Returns:
            关闭是否成功
        """
        url = f'http://{IP}:{PORT}/api/v1/browser/stop'  # 关闭环境
        open_data = {"containerCode": code}

        try:
            open_res = requests.post(url, json=open_data, timeout=30).json()

            if open_res['code'] != 0:
                print(f'环境关闭失败: {open_res}')
                return False

            return True
        except Exception as e:
            print(f"关闭HubStudio浏览器失败: {str(e)}")
            return False

    @staticmethod
    def get_hub_list() -> List[Dict[str, Any]]:
        """
        获取所有HubStudio浏览器数据

        Returns:
            浏览器数据列表
        """
        data_list = []
        page_size = 200
        url = f'http://{IP}:{PORT}/api/v1/env/list'  # 获取列表

        def fetch_page(page_num: int, data_list_: List) -> Dict:
            """获取分页数据"""
            request_data = {
                "current": page_num,
                "size": page_size
            }

            try:
                response = requests.post(url, json=request_data, timeout=30).json()

                if response['code'] != 0:
                    print(f'获取环境列表失败: {response}')
                    return None

                data_list_.extend(response['data']['list'])
                return response
            except Exception as e:
                print(f"获取环境列表失败: {str(e)}")
                return None

        # 获取第一页并计算总页数
        first_page = fetch_page(1, data_list)
        if not first_page:
            return data_list

        total_pages = math.ceil(first_page['data']['total'] / page_size)

        # 获取剩余页
        for page in range(2, total_pages + 1):
            fetch_page(page, data_list)

        # 打印SQL语句
        for item in data_list:
            container_code = item.get('containerCode', '')
            container_name = item.get('containerName', '')
            if container_code and container_name:
                sql = f"UPDATE crawler_account SET container_code = '{container_code}' WHERE username = '{container_name}';"
                print(sql)

        return data_list

    def ac_slider(self) -> None:
        """模拟滑块验证"""
        try:
            if not self.ac or not self.page:
                print("DrissionPage未初始化，无法执行滑块验证")
                return

            self.page.get('https://havanalogin.taobao.com/unify_oauth.htm?appName=taobao-oauth&appEntrance'
                          '=cainiao&type=taobao&state=idc_1KYn08fiCqGsgXpMaR4rs7g&lang=zh_CN&redirectUri='
                          'https%3A%2F%2Fpassport.cainiao.com%2Foauth_sign.htm%3Ftype%3Dtaobao%26return_'
                          'url%3Dhttps%253A%252F%252Fcnlogin.cainiao.com%252FdoLogin%253FredirectURL%253Dh'
                          'ttp%25253A%25252F%25252Fg.cainiao.com%25252Fstore%25252Fone-plate-inventory-que'
                          'ry%25252Fbatch-inventory-query-v2%2526isNewLogin%253Dfalse&redirectWhileLogged=false#taobao')

            trajectory = generate_human_like_trajectory(0, 330, steps=10)
            # 点击并按住滑块
            self.ac.hold('@aria-label=滑块')
            # 模拟人类滑动轨迹
            for step_x, step_y in trajectory:
                self.ac.move(step_x, step_y)
            # 释放滑块
            self.ac.release()
        except Exception as e:
            print(f"滑块验证失败: {str(e)}")


class BitBrowser(FingerprintBrowserBase):
    """比特浏览器实现类"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 支持从环境变量读取 API 地址（Docker 容器内可访问宿主机）
        bit_api_ip = os.getenv('BIT_BROWSER_IP', '127.0.0.1')
        self.api_base_url = f"http://{bit_api_ip}:54345"

    def start_fingerprint(self, code: str) -> bool:
        """
        启动比特浏览器

        Args:
            code: 指纹标识码(窗口ID)

        Returns:
            启动是否成功
        """
        try:
            url = f"{self.api_base_url}/browser/open"
            # data = {
            #     "id": code,
            #     "args": [],
            #     "queue": True
            #     # "args": ["--headless"],
            #     # "queue": True,
            #     # "ignoreDefaultUrls": True // 一定要加这个参数，以及不要配置newPageUrl，窗口打开后自己通过脚本打开page页面即可
            #
            # }
            data = {
              "id": code,
              "ignoreDefaultUrls": True,  # 忽略同步URL、工作台与缓存页（关键）
              "newPageUrl": "about:blank", # 指定唯一空白页，无外部请求
              "queue": True,               # 防并发报错，建议保留
              "args": [                    # 禁用系统级多余页面
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble"
              ]
            }
            response = requests.post(url, json=data, timeout=300)
            result = response.json()

            if not result.get("success"):
                print(f'环境打开失败: {result}')
                return False

            # 获取必要的配置信息
            self.debugging_port = result.get("data", {}).get("http").split(':')[1]
            self.webdriver_path = result.get("data", {}).get("driver")

            return True

        except Exception as e:
            print(f"启动比特浏览器失败: {str(e)}")
            return False

    def get_pid(self, code):
        """
        获取已打开窗口的进程 pid 集合，也可以用来判断窗口是否已打开，支持批量查询
        :param ids:
        :return:
        """

        url = f"{self.api_base_url}/browser/pids"
        data = {
            "ids": [code]
        }

        try:
            response = requests.post(url, json=data)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"获取已打开窗口失败: {e}")
            return None

    def quit_fingerprint(self, code: str) -> bool:
        """
        关闭比特浏览器

        Args:
            code: 指纹标识码(窗口ID)

        Returns:
            关闭是否成功
        """
        try:
            result_pid = self.get_pid(code)
            if result_pid.get("success"):
                if result_pid.get("data").get(code) is None:
                    print(f'环境未打开: {result_pid}')
                    return False
            url = f"{self.api_base_url}/browser/close"
            data = {"id": code}

            response = requests.post(url, json=data, timeout=30)
            result = response.json()
            print('启动指纹信息：', result)
            if result.get("success") != True:
                print(f'环境关闭失败: {result}')
                return False

            return True

        except Exception as e:
            print(f"关闭比特浏览器失败: {str(e)}")
            return False

    def create_browser(self, group_id: str = "", name: str = "", args: List = None, queue: bool = False) -> Dict:
        """
        创建浏览器窗口

        Args:
            group_id: 窗口所属组的ID
            name: 窗口名称
            args: 浏览器启动参数
            queue: 是否以队列方式创建

        Returns:
            创建结果信息
        """
        if args is None:
            args = []

        url = f"{self.api_base_url}/browser/create"
        data = {
            "group_id": group_id,
            "name": name,
            "args": args,
            "queue": queue
        }

        try:
            response = requests.post(url, json=data)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"创建浏览器窗口失败: {e}")
            return None

    def close_all_windows(self) -> Dict:
        """
        关闭所有浏览器窗口

        Returns:
            关闭结果信息
        """
        url = f"{self.api_base_url}/browser/closeAll"
        try:
            response = requests.post(url)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"关闭所有窗口失败: {e}")
            return None

    def get_browser_detail(self, code: str) -> Dict:
        url = f"{self.api_base_url}/browser/detail"
        try:
            data = {
                "id": code
            }
            response = requests.post(url,data=data)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"关闭所有窗口失败: {e}")
            return None

    def update_proxy(self,code: str,proxy) -> Dict:
        try:
            print('切换代理')
            # proxy = f'http://PsFaJMphAU0hH1s20E-zone-custom-region-{PROXY_MAPPING[self.task["country"].upper()]}-session-{int(time.time() * 1000)}-sessTime-5:iWrz7GbWhm@a477c1a8e06d7ff8.qzc.na.grassdata.net:2333'
            info = proxy[7:].split('@')
            username = info[0].split(':')[0]
            psw = info[0].split(':')[1]
            host = info[1].split(':')[0]
            port = info[1].split(':')[1]
           #更改代理ip
            current_timestamp = str(int(time.time() * 1000))
            pattern = r'(session-)(\d+)(-)'
            new_username = re.sub(pattern, r'\1' + current_timestamp + r'\3', username)
            url = f"{self.api_base_url}/browser/proxy/update"
            data = {
                "ids": [code],
                "ipCheckService": "IP2Location",
                "proxyMethod": 2,
                "proxyType": "http",
                "host": host,
                "port": port,
                "proxyUserName": new_username,
                "proxyPassword": psw,
            }
            response = requests.post(url, data=data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"切换代理失败: {e}")
            return None

class FingerprintBrowserFactory:
    """指纹浏览器工厂类"""

    # 注册可用的浏览器类型
    _registry = {
        'hubstudio': HubStudio,
        # 'vmlogin': VmLogin,
        'bitbrowser': BitBrowser,
    }

    @classmethod
    def register_browser(cls, key: str, browser_class: Type[FingerprintBrowserBase]) -> None:
        """
        注册新的浏览器类型

        Args:
            key: 浏览器类型标识
            browser_class: 浏览器类
        """
        cls._registry[key] = browser_class

    @classmethod
    def detect_browser_type(cls, code: str) -> str:
        """
        根据code自动检测浏览器类型

        Args:
            code: 指纹标识码

        Returns:
            浏览器类型
        """
        if '-' in code:
            return 'vmlogin'
        elif code.isdigit():
            return 'hubstudio'
        else:
            return 'bitbrowser'

    @classmethod
    def create_browser(
            cls,
            code: str,
            browser_type: Optional[str] = None,
            webdriver_path: Optional[str] = None,
            debugging_port: Optional[str] = None,
            user_agent: Optional[str] = None,
            is_init_selenium: bool = True,
            is_init_drission: bool = True,
            is_init_selenium_wire: bool = True
    ) -> FingerprintBrowserBase:
        """
        创建指纹浏览器实例

        Args:
            code: 指纹标识码
            browser_type: 浏览器类型，为None时自动检测
            webdriver_path: 浏览器驱动地址
            debugging_port: 调试端口
            user_agent: 用户代理
            is_init_selenium: 是否初始化selenium
            is_init_drission: 是否初始化DrissionPage
            is_init_selenium_wire: 是否初始化selenium-wire

        Returns:
            指纹浏览器实例
        """
        if browser_type is None:
            browser_type = cls.detect_browser_type(code)

        if browser_type not in cls._registry:
            raise ValueError(f"不支持的浏览器类型: {browser_type}")

        browser_class = cls._registry[browser_type]
        return browser_class(
            webdriver_path=webdriver_path,
            debugging_port=debugging_port,
            user_agent=user_agent,
            is_init_selenium=is_init_selenium,
            is_init_drission=is_init_drission,
            is_init_selenium_wire=is_init_selenium_wire
        )

    @classmethod
    def start(
            cls,
            code: str,
            browser_type: Optional[str] = None,
            webdriver_path: Optional[str] = None,
            debugging_port: Optional[str] = None,
            user_agent: Optional[str] = None,
            is_init_selenium: bool = True,
            is_init_drission: bool = True,
            is_init_selenium_wire: bool = True
    ) -> FingerprintBrowserBase:
        """
        启动指纹浏览器

        Args:
            code: 指纹标识码
            browser_type: 浏览器类型，为None时自动检测
            webdriver_path: 浏览器驱动地址
            debugging_port: 调试端口
            user_agent: 用户代理
            is_init_selenium: 是否初始化selenium
            is_init_drission: 是否初始化DrissionPage
            is_init_selenium_wire: 是否初始化selenium-wire

        Returns:
            浏览器实例
        """
        browser = cls.create_browser(
            code=code,
            browser_type=browser_type,
            webdriver_path=webdriver_path,
            debugging_port=debugging_port,
            user_agent=user_agent,
            is_init_selenium=is_init_selenium,
            is_init_drission=is_init_drission,
            is_init_selenium_wire=is_init_selenium_wire
        )

        success = browser.start(code=code)
        if not success:
            print(f"启动指纹浏览器失败: {code}")

        return browser

    @classmethod
    def quit_browser(cls, browser: FingerprintBrowserBase, code: str) -> bool:
        """
        关闭指纹浏览器

        Args:
            browser: 浏览器实例
            code: 指纹标识码

        Returns:
            关闭是否成功
        """
        return browser.quit_fingerprint(code=code)

    @classmethod
    def create_new_window(
            cls,
            browser: FingerprintBrowserBase,
            group_id: str = "",
            name: str = "",
            args: List = None,
            queue: bool = False
    ) -> Dict:
        """
        创建新的浏览器窗口

        Args:
            browser: 浏览器实例
            group_id: 窗口所属组的ID
            name: 窗口名称
            args: 浏览器启动参数
            queue: 是否以队列方式创建

        Returns:
            创建结果信息
        """
        return browser.create_browser(
            group_id=group_id,
            name=name,
            args=args,
            queue=queue
        )

    @classmethod
    def close_all_windows(cls, browser: FingerprintBrowserBase) -> Dict:
        """
        关闭所有浏览器窗口

        Args:
            browser: 浏览器实例

        Returns:
            关闭结果信息
        """
        return browser.close_all_windows()


# 保持原始接口兼容
def start_fingerprint(
        code: str,
        webdriver_path: Optional[str] = None,
        debugging_port: Optional[str] = None,
        user_agent: Optional[str] = None,
        is_init_selenium: bool = True,
        is_init_drission: bool = True,
        is_init_selenium_wire: bool = True,
        fp_type: str = 'auto'
) -> FingerprintBrowserBase:
    """
    入口函数，能够根据code自动识别指纹类型，启动对应指纹

    Args:
        code: 启动指纹浏览器id，能够自动识别哪家指纹
        webdriver_path: 浏览器驱动地址，一般通过接口获取，调试阶段可以手动设置文件路径
        debugging_port: 指纹浏览器远程debug开启端口，正常不需要设置，调试的时候设置
        user_agent: 默认不需要配置
        is_init_selenium: 是否连接selenium，如果要过selenium检测，就不要连接，通过检测页面后再连接
        is_init_drission: 同上
        is_init_selenium_wire: 同上
        fp_type: 指纹类型 hubstudio\vmlogin\auto

    Returns:
        指纹浏览器实例
    """
    # 处理自动检测类型
    if fp_type == 'auto':
        browser_type = None
    else:
        browser_type = fp_type

    # 使用工厂创建浏览器实例
    fp = FingerprintBrowserFactory.create_browser(
        code=code,
        browser_type=browser_type,
        webdriver_path=webdriver_path,
        debugging_port=debugging_port,
        user_agent=user_agent,
        is_init_selenium=is_init_selenium,
        is_init_drission=is_init_drission,
        is_init_selenium_wire=is_init_selenium_wire
    )

    # 启动浏览器
    start_success = fp.start(code=code)
    if not start_success:
        print(f"启动指纹浏览器失败: {code}")

    return fp


def test(code: str) -> None:
    """
    测试代码

    Args:
        code: 指纹标识码
    """
    try:
        chrome = start_fingerprint(code=code, fp_type='bitbrowser')
        chrome.driver.get("https://www.baidu.com/")
        chrome.driver.implicitly_wait(10)

        # 输入搜索内容
        search_input = chrome.driver.find_element_by_class_name('s_ipt')
        search_input.send_keys('hubstudio')
        time.sleep(random.uniform(0.5, 1.5))

        # 点击搜索按钮
        search_button = chrome.driver.find_element_by_id("su")
        search_button.click()
        time.sleep(6)

        # 获取搜索结果
        print("搜索完成，当前页面标题:", chrome.driver.title)
    except Exception as e:
        print(f"测试失败: {str(e)}")
        traceback.print_exc()


def bite_test():
    # 创建比特浏览器实例
    browser = FingerprintBrowserFactory.create_browser(code='929c77937a4f4d15b8829eabcc82bbac',
                                                       browser_type='bitbrowser',
                                                       debugging_port=54345)

    # 创建新窗口
    # result = browser.create_browser(name="test_window")
    browser.get("https://www.baidu.com/")
    # 关闭所有窗口
    browser.close_all_windows()

    # 对于不支持这些功能的浏览器
    vmlogin_browser = FingerprintBrowserFactory.create_browser(code='your_code', browser_type='vmlogin')
    vmlogin_browser.create_browser()  # 会打印"当前浏览器类型不支持创建新窗口"


if __name__ == '__main__':
    # test('929c77937a4f4d15b8829eabcc82bbac')
    # 使用DrissionPage（不初始化selenium和selenium_wire）
    dp = start_fingerprint(code='866538e6a560446ca29ff9c52b12be5e', is_init_selenium=True, is_init_selenium_wire=False,
                      is_init_drission=True)
    # print(dp.get_browser_detail('cde33721fb8e4703bdc0c143d5c8de61'))
