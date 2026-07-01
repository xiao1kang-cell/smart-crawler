"""
本地 Chrome 多账号浏览器管理器
使用 DrissionPage 控制本地 Chrome，每个账号通过独立 user-data-dir 隔离 session/cookies
替代付费指纹浏览器，适用于开发/测试/降本场景

认证代理方案：本地起一个无认证的代理转发器，透明转发到上游认证代理，Chrome 连本地代理即可。

用法：
    chrome = LocalChrome()
    chrome.start(account_id="user@example.com", proxy={"http": "http://user:pass@host:port"})
    chrome.page.get("https://www.amazon.com")
    chrome.quit()
"""
import base64
import hashlib
import os
import re
import select
import socket
import threading
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional, Dict

from DrissionPage import Chromium, ChromiumOptions
from loguru import logger

# ====== 默认配置 ======
DEFAULT_PROFILE_BASE = os.path.join(str(Path.home()), '.amazon_crawler_profiles')
DEFAULT_START_PORT = 19222


# ====== 本地代理转发器（解决 Chrome 不支持认证代理的问题） ======
class _ProxyForwarder:
    """
    在本地启动一个无需认证的 HTTP 代理，透明转发流量到上游认证代理。
    Chrome 连接 127.0.0.1:local_port（无需认证），本地代理自动加 Proxy-Authorization 头转发到上游。
    """

    def __init__(self, upstream_host: str, upstream_port: int, username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.auth_header = 'Basic ' + base64.b64encode(
            f'{username}:{password}'.encode()
        ).decode()
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.local_port: Optional[int] = None

    def start(self) -> int:
        """启动本地代理，返回本地监听端口"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(('127.0.0.1', 0))
        self.local_port = self._server_socket.getsockname()[1]
        self._server_socket.listen(32)
        self._server_socket.settimeout(1.0)
        self._running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(f"本地代理转发器启动: 127.0.0.1:{self.local_port} -> {self.upstream_host}:{self.upstream_port}")
        return self.local_port

    def stop(self):
        """停止本地代理"""
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("本地代理转发器已停止")

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, _ = self._server_socket.accept()
                t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, client_sock: socket.socket):
        try:
            client_sock.settimeout(30)
            # 读取客户端发来的请求头
            request_data = b''
            while b'\r\n\r\n' not in request_data:
                chunk = client_sock.recv(4096)
                if not chunk:
                    client_sock.close()
                    return
                request_data += chunk

            header_end = request_data.index(b'\r\n\r\n')
            header_part = request_data[:header_end]
            body_part = request_data[header_end + 4:]
            first_line = header_part.split(b'\r\n')[0].decode('utf-8', errors='replace')
            logger.debug(f"[ProxyForwarder] 收到请求: {first_line}")

            if first_line.upper().startswith('CONNECT'):
                self._handle_connect(client_sock, first_line, header_part)
            else:
                self._handle_http(client_sock, first_line, header_part, body_part)
        except Exception as e:
            logger.error(f"[ProxyForwarder] 处理请求异常: {e}")
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def _connect_upstream(self) -> socket.socket:
        logger.debug(f"[ProxyForwarder] 连接上游代理: {self.upstream_host}:{self.upstream_port}")
        try:
            upstream_sock = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
            upstream_sock.settimeout(60)
            logger.debug(f"[ProxyForwarder] 上游代理连接成功")
            return upstream_sock
        except Exception as e:
            logger.error(f"[ProxyForwarder] 连接上游代理失败: {e}")
            raise

    def _handle_connect(self, client_sock: socket.socket, first_line: str, header_part: bytes):
        """处理 HTTPS CONNECT 隧道"""
        # 提取目标 host:port
        target = first_line.split()[1] if len(first_line.split()) > 1 else ''
        # 连接上游代理
        try:
            upstream_sock = self._connect_upstream()
        except Exception:
            client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
            return
        try:
            # 向上游发送 CONNECT + Host + 认证头
            connect_req = (
                f"{first_line}\r\n"
                f"Host: {target}\r\n"
                f"Proxy-Authorization: {self.auth_header}\r\n"
                f"Proxy-Connection: keep-alive\r\n"
                f"\r\n"
            )
            logger.debug(f"[ProxyForwarder] CONNECT 发送到上游:\n{connect_req.strip()}")
            upstream_sock.sendall(connect_req.encode())

            # 读取上游响应
            response = b''
            while b'\r\n\r\n' not in response:
                chunk = upstream_sock.recv(4096)
                if not chunk:
                    client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    return
                response += chunk

            status_line = response.split(b'\r\n')[0]
            logger.debug(f"[ProxyForwarder] CONNECT 上游响应: {status_line}")
            if b'200' in status_line:
                client_sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self._relay(client_sock, upstream_sock)
            else:
                logger.error(f"[ProxyForwarder] CONNECT 上游拒绝: {response[:500]}")
                client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
        finally:
            try:
                upstream_sock.close()
            except Exception:
                pass

    def _handle_http(self, client_sock: socket.socket, first_line: str, header_part: bytes, body_part: bytes):
        """处理普通 HTTP 请求"""
        try:
            upstream_sock = self._connect_upstream()
        except Exception:
            client_sock.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
            return
        try:
            # 重新组装请求，注入 Proxy-Authorization 头
            lines = header_part.split(b'\r\n')
            new_lines = [lines[0]]
            for line in lines[1:]:
                # 移除客户端可能带的 Proxy-Authorization
                if line.lower().startswith(b'proxy-authorization'):
                    continue
                new_lines.append(line)
            new_lines.append(f'Proxy-Authorization: {self.auth_header}'.encode())
            new_header = b'\r\n'.join(new_lines) + b'\r\n\r\n' + body_part
            logger.debug(f"[ProxyForwarder] HTTP 转发: {first_line}")
            upstream_sock.sendall(new_header)

            # 转发上游响应给客户端
            first_chunk = True
            while True:
                try:
                    data = upstream_sock.recv(8192)
                    if not data:
                        break
                    if first_chunk:
                        logger.debug(f"[ProxyForwarder] HTTP 上游响应前200字节: {data[:200]}")
                        first_chunk = False
                    client_sock.sendall(data)
                except (socket.timeout, ConnectionError):
                    break
        finally:
            try:
                upstream_sock.close()
            except Exception:
                pass

    @staticmethod
    def _relay(sock_a: socket.socket, sock_b: socket.socket):
        """双向数据转发"""
        sockets = [sock_a, sock_b]
        try:
            while True:
                readable, _, error = select.select(sockets, [], sockets, 60)
                if error:
                    break
                if not readable:
                    break
                for sock in readable:
                    try:
                        data = sock.recv(8192)
                    except Exception:
                        return
                    if not data:
                        return
                    target = sock_b if sock is sock_a else sock_a
                    try:
                        target.sendall(data)
                    except Exception:
                        return
        except Exception:
            pass


class LocalChrome:
    """
    本地 Chrome 浏览器实例，兼容 FingerprintBrowserBase 常用接口。
    每个账号使用独立的 user-data-dir + debugging-port，实现多账号并行登录隔离。
    """
    _port_lock = threading.Lock()
    _used_ports: set = set()

    def __init__(self, profile_base: str = None):
        self.profile_base = profile_base or DEFAULT_PROFILE_BASE
        os.makedirs(self.profile_base, exist_ok=True)
        self.browser: Optional[Chromium] = None
        self.page = None
        self.debugging_port: Optional[int] = None
        self.user_data_dir: Optional[str] = None
        self.proxy_info: Optional[Dict] = None
        self.user_agent: Optional[str] = None
        self._proxy_forwarder: Optional[_ProxyForwarder] = None

    # ---------- 端口管理 ----------
    @classmethod
    def _allocate_port(cls) -> int:
        with cls._port_lock:
            port = DEFAULT_START_PORT
            while port in cls._used_ports:
                port += 1
            cls._used_ports.add(port)
            return port

    @classmethod
    def _release_port(cls, port: int):
        with cls._port_lock:
            cls._used_ports.discard(port)

    # ---------- Profile 管理 ----------
    def _get_profile_dir(self, account_id: str) -> str:
        safe_name = hashlib.md5(account_id.encode()).hexdigest()[:16]
        profile_dir = os.path.join(self.profile_base, safe_name)
        os.makedirs(profile_dir, exist_ok=True)
        return profile_dir

    @staticmethod
    def _parse_proxy_url(proxy_url: str) -> Dict:
        """解析代理 URL，兼容无 scheme 输入。"""
        url = proxy_url.strip()
        if '://' not in url:
            url = f'http://{url}'
        parsed = urlparse(url)
        return {
            'scheme': parsed.scheme or 'http',
            'host': parsed.hostname,
            'port': parsed.port,
            'username': parsed.username,
            'password': parsed.password,
        }

    # ---------- 核心方法 ----------
    def start(self, account_id: str, proxy: Dict = None, headless: bool = False) -> bool:
        """
        启动一个独立的本地 Chrome 实例

        Args:
            account_id: 账号唯一标识（如 username），用于生成独立 profile 目录
            proxy: 代理配置 {"http": "http://user:pass@host:port", "https": "..."}
            headless: 是否无头模式

        Returns:
            启动是否成功
        """
        try:
            self.debugging_port = self._allocate_port()
            self.user_data_dir = self._get_profile_dir(account_id)

            co = ChromiumOptions()
            co.set_user_data_path(self.user_data_dir)
            co.auto_port(False)

            # 基本启动参数
            co.set_argument('--no-first-run')
            co.set_argument('--no-default-browser-check')
            co.set_argument('--disable-popup-blocking')
            co.set_argument('--disable-background-timer-throttling')
            co.set_argument('--disable-session-crashed-bubble')
            co.set_address(f'127.0.0.1:{self.debugging_port}')

            if headless:
                co.headless(True)

            # 设置代理
            if proxy:
                proxy_url = proxy.get('http') or proxy.get('https')
                if proxy_url:
                    parsed = self._parse_proxy_url(proxy_url)
                    if parsed.get('username') and parsed.get('password'):
                        # 认证代理：启动本地转发器，Chrome 连本地无认证代理
                        self._proxy_forwarder = _ProxyForwarder(
                            upstream_host=parsed['host'],
                            upstream_port=parsed['port'],
                            username=parsed['username'],
                            password=parsed['password'],
                        )
                        local_port = self._proxy_forwarder.start()
                        co.set_argument(f'--proxy-server=http://127.0.0.1:{local_port}')
                    else:
                        co.set_proxy(proxy_url)
                self.proxy_info = proxy

            self.browser = Chromium(addr_or_opts=co)
            self.page = self.browser.latest_tab
            self.user_agent = self.page.run_js("return navigator.userAgent;")

            logger.info(f"本地Chrome启动成功: port={self.debugging_port}, account={account_id}")
            return True

        except Exception as e:
            logger.error(f"本地Chrome启动失败: {str(e)}")
            self._cleanup_on_failure()
            return False

    def _cleanup_on_failure(self):
        if self._proxy_forwarder:
            self._proxy_forwarder.stop()
            self._proxy_forwarder = None
        if self.debugging_port:
            self._release_port(self.debugging_port)
            self.debugging_port = None

    def quit(self) -> bool:
        """关闭浏览器实例并释放端口"""
        try:
            if self.browser:
                self.browser.quit()
                self.browser = None
                self.page = None
            if self._proxy_forwarder:
                self._proxy_forwarder.stop()
                self._proxy_forwarder = None
            if self.debugging_port:
                self._release_port(self.debugging_port)
                self.debugging_port = None
            logger.info("本地Chrome已关闭")
            return True
        except Exception as e:
            logger.error(f"关闭本地Chrome失败: {str(e)}")
            return False

    # ---------- 兼容 FingerprintBrowserBase 接口 ----------
    def quit_fingerprint(self, code: str) -> bool:
        """兼容指纹浏览器关闭接口"""
        return self.quit()

    def get_browser_detail(self, code: str) -> Dict:
        """
        兼容指纹浏览器接口：返回代理信息
        格式与 BitBrowser.get_browser_detail 一致
        """
        if not self.proxy_info:
            return {"data": {}}
        proxy_url = self.proxy_info.get('http', '')
        match = re.match(r'https?://([^:]+):([^@]+)@([^:]+):(\d+)', proxy_url)
        if match:
            return {
                "data": {
                    "proxyUserName": match.group(1),
                    "proxyPassword": match.group(2),
                    "host": match.group(3),
                    "port": match.group(4)
                }
            }
        return {"data": {}}

    def update_proxy(self, code: str, proxy_url: str) -> bool:
        """
        动态更新代理（重启浏览器方式）
        注意：本地Chrome不支持运行时切换代理，需重启实例
        """
        logger.warning("本地Chrome不支持运行时切换代理，请重启浏览器实例")
        return False
