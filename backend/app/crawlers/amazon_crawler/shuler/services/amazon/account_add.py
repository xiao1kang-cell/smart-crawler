import random

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import PROXY_MAPPING
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


def add_account():
    test_accounts = [
        {
            "username": "19017496709",
            "password": "b2hicv7b",
            "country": "us",
            "cookies": {},
            "fingerprint_id": "cde33721fb8e4703bdc0c143d5c8de61",
            "last_used_time": 0.0,
            "is_used": False
        },
        {
            "username": "19183174985",
            "password": "b2hicv7b",
            "country": "us",
            "cookies": {},
            "fingerprint_id": "8653e6478162478198724796e96ba1fd",
            "last_used_time": 0.0,
            "is_used": False
        },
        {
            "username": "16029222948",
            "password": "t1hp4tft",
            "country": "us",
            "cookies": {},
            "fingerprint_id": "8abd499421504cbba4c78b3db39c40a3",
            "last_used_time": 0.0,
            "is_used": False
        },
        {
            "username": "17856484048",
            "password": "vj0zxshe",
            "country": "us",
            "cookies": {},
            "fingerprint_id": "98dc7cc7bd814fa4bd6a134c1228a915",
            "last_used_time": 0.0,
            "is_used": False
        }
    ]
    # db = AccountDB()
    # db.accounts_coll.insert_many(test_accounts)


import requests
import json
import time
from typing import List, Dict
from urllib.parse import urlparse

# ====================== 配置 ======================
BIT_BROWSER_API_URL = "http://127.0.0.1:54345"  # 指纹浏览器默认地址
PROXY_PROXY = "gate.rola.vip"
PROXY_PORT = 2000
PROXY_ACCOUNT = "Lpr8vwo_1"  # 替换成你的
PROXY_PASSWORD = "vhRNcZZ5"  # 替换成你的

# # ====================== 你的原始账号列表 ======================
# raw_accounts = [
#     {
#         "username": "19017496709",
#         "password": "b2hicv7b",
#         "country": "us",
#         'totp_secret':'',
#         "cookies": {"cookie1": "v1", "cookie2": "v2"},
#     },
#     {
#         "username": "19183174985",
#         "password": "b2hicv7b",
#         "country": "us",
#         "cookies": {"cookie1": "v1", "cookie2": "v2"},
#     },
#     {
#         "username": "16029222948",
#         "password": "t1hp4tft",
#         "country": "ca",
#         "cookies": {"cookie1": "v1", "cookie2": "v2"},
#     },
#     {
#         "username": "17856484048",
#         "password": "vj0zxshe",
#         "country": "jp",
#         "cookies": {"cookie1": "v1", "cookie2": "v2"},
#     }
# ]
import pandas as pd


ACCOUNT_REQUIRED_COLUMNS = ("username", "password", "totp")
ACCOUNT_OPTIONAL_COLUMNS = ("browser_id", "proxy")
ACCOUNT_COLUMN_ALIASES = {
    "username": "username",
    "user": "username",
    "account": "username",
    "password": "password",
    "pwd": "password",
    "totp": "totp",
    "totp_secret": "totp",
    "two_factor": "totp",
    "2fa": "totp",
    "browser_id": "browser_id",
    "browserid": "browser_id",
    "fingerprint_id": "browser_id",
    "fingerprintid": "browser_id",
    "proxy": "proxy",
    "proxy_": "proxy",
    "static_ip": "proxy",
}


def _clean_excel_value(value) -> str:
    if pd.isna(value):
        return ""
    raw = str(value).strip()
    return "" if raw.lower() == "nan" else raw


def _normalize_account_column_name(value) -> str:
    raw = _clean_excel_value(value).strip().lower()
    raw = raw.replace(" ", "_").replace("-", "_")
    return ACCOUNT_COLUMN_ALIASES.get(raw, raw)


def parse_account_excel(file_path: str, country='US') -> Dict:
    """读取账号 Excel，兼容旧前三列格式和新表头格式。"""
    df = pd.read_excel(file_path, header=None, dtype=str, keep_default_na=False)
    if df.empty:
        return {
            "format": "empty",
            "has_header": False,
            "columns": [],
            "missing_columns": list(ACCOUNT_REQUIRED_COLUMNS),
            "total_rows": 0,
            "accounts": [],
        }

    first_row = [_normalize_account_column_name(v) for v in df.iloc[0].tolist()]
    known_columns = set(ACCOUNT_REQUIRED_COLUMNS) | set(ACCOUNT_OPTIONAL_COLUMNS)
    has_header = any(name in known_columns for name in first_row)

    if has_header:
        col_map = {}
        for idx, name in enumerate(first_row):
            if name in known_columns and name not in col_map:
                col_map[name] = idx
        missing = [name for name in ACCOUNT_REQUIRED_COLUMNS if name not in col_map]
        if missing:
            raise ValueError(
                "账号Excel表头缺少列: "
                + ", ".join(missing)
                + "；需要列名 username/password/totp，可选 browser_id/proxy"
            )
        data_df = df.iloc[1:].reset_index(drop=True)
        row_no_offset = 2
        excel_format = "named"
    else:
        if df.shape[1] < 2:
            raise ValueError("账号Excel至少需要 username/password 两列")
        col_map = {"username": 0, "password": 1}
        if df.shape[1] >= 3:
            col_map["totp"] = 2
        data_df = df.reset_index(drop=True)
        row_no_offset = 1
        excel_format = "legacy"
        missing = []

    accounts = []
    for idx, row in data_df.iterrows():
        username = _clean_excel_value(row.get(col_map["username"], ""))
        password = _clean_excel_value(row.get(col_map["password"], ""))
        if not username or not password:
            continue
        totp_secret = _clean_excel_value(row.get(col_map.get("totp"), "")) if "totp" in col_map else ""
        browser_id = _clean_excel_value(row.get(col_map.get("browser_id"), "")) if "browser_id" in col_map else ""
        proxy = _clean_excel_value(row.get(col_map.get("proxy"), "")) if "proxy" in col_map else ""
        accounts.append({
            "row_no": int(idx) + row_no_offset,
            "username": username,
            "password": password,
            "country": country,
            "totp_secret": totp_secret,
            "browser_id": browser_id,
            "proxy": proxy,
            "cookies": {}
        })

    return {
        "format": excel_format,
        "has_header": has_header,
        "columns": [str(v) for v in df.iloc[0].tolist()] if has_header else [],
        "missing_columns": missing,
        "total_rows": max(0, int(len(df)) - 1) if has_header else int(len(df)),
        "accounts": accounts,
    }


def read_excel_to_accounts(file_path: str,country = 'US') -> List[Dict]:
    return parse_account_excel(file_path, country).get("accounts", [])


def _normalize_single_proxy_item(item) -> Dict:
    if isinstance(item, dict):
        proxy_url = str(item.get("http") or item.get("https") or item.get("url") or "").strip()
        if proxy_url:
            return _normalize_single_proxy_item(proxy_url)
        host = str(item.get("host") or item.get("hostname") or "").strip()
        port = int(item.get("port") or 0)
        user = str(item.get("user") or item.get("username") or "").strip()
        password = str(item.get("password") or "").strip()
        if host and port > 0 and user and password:
            return {"host": host, "port": port, "user": user, "password": password}
        return {}

    raw = str(item or "").strip()
    if not raw:
        return {}
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.hostname and parsed.port and parsed.username and parsed.password:
            return {
                "host": parsed.hostname,
                "port": int(parsed.port),
                "user": parsed.username,
                "password": parsed.password,
            }
        return {}
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) != 4:
        return {}
    host, port_str, user, password = parts
    try:
        port = int(port_str)
    except Exception:
        return {}
    if not host or port <= 0 or not user or not password:
        return {}
    return {"host": host, "port": port, "user": user, "password": password}


def normalize_static_proxy_pool(static_ip_pool: List = None) -> List[Dict]:
    normalized = []
    for item in (static_ip_pool or []):
        proxy_item = _normalize_single_proxy_item(item)
        if proxy_item:
            normalized.append(proxy_item)
    return normalized


def proxy_to_url(proxy_: Dict) -> str:
    if not proxy_:
        return ""
    return f'http://{proxy_["user"]}:{proxy_["password"]}@{proxy_["host"]}:{proxy_["port"]}'


def normalize_account_username(username: str, country: str) -> str:
    """保持旧导入规则：JP 补 +，其他站点补 +1。"""
    raw = str(username or "").strip()
    country_code = str(country or "").strip().upper()
    if not raw:
        return raw
    if country_code == "JP":
        return raw if "+" in raw else f"+{raw}"
    return raw if "+" in raw else f"+1{raw}"


def build_account_record(account: Dict, browser_id: str, proxy_: Dict, static_ip: str = "") -> Dict:
    """构建 crawler_accounts 入库结构。"""
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    return {
        "username": account["username"],
        "password": account["password"],
        "country": account["country"],
        "cookies": account["cookies"],
        "totp_secret": account["totp_secret"],
        "fingerprint_id": browser_id,
        "proxy_": proxy_,
        "static_ip": static_ip,
        "state": 1,
        "last_used_time": 0.0,
        "is_used": False,
        "fail_count": 0,
        "cooldown_until": 0.0,
        "quota_factor": 1.00,
        "label": "",
        "create_time": now,
        "update_time": now,
    }


def _static_proxy_for_suffix(static_ip_pool: List = None, static_ip_count: int = 0, suffix=1) -> Dict:
    pool = normalize_static_proxy_pool(static_ip_pool)
    if static_ip_count and static_ip_count > 0:
        pool = pool[:static_ip_count]
    try:
        proxy_index = int(suffix) - 1
    except Exception:
        proxy_index = 0
    if 0 <= proxy_index < len(pool):
        return pool[proxy_index]
    return {}

# ====================== 工具：生成代理字符串 ======================
def build_proxy_user(account: Dict, suffix, static_ip_count: int = 0, static_ip_pool: List = None) -> str:
    """
    生成代理用户名，自动匹配国家
    """
    # PROXY_HOST = "gate.rola.vip"
    # PROXY_PORT = 2000
    # PROXY_PREFIX = "Lpr8vwo"  # 如 Lpr8vwo
    # PROXY_PASSWORD = 'vhRNcZ5Z'  # 如 vhRNcZ5Z
    # PROXY_SESS_TIME = 30  # IP 固定时长
    # country = account["country"].lower()



    static_proxy = _static_proxy_for_suffix(static_ip_pool, static_ip_count, suffix)
    if static_proxy:
        return static_proxy

    # proxy_user = f"{PROXY_PREFIX}_{suffix}-sessTime-{PROXY_SESS_TIME}-country-{country}"
    # 动态ip
    country = str(account.get("country", "")).strip().upper()
    state_ = random.choice(['colorado', 'california', 'florida', 'georgia'])
    session_id = int(time.time() * 1000000)
    # 美国添加州信息，其他国家保持原格式
    country_code = PROXY_MAPPING.get(country, country.lower() or "us")
    if country_code.lower() == "us":
        region_part = f"{country_code}-st-{state_}"
    else:
        region_part = country_code
    proxy_user = f'PsFaJMphAU0hH1s20E-zone-custom-region-{region_part}-session-{session_id}-sessTime-10'
    # 日本 a477c1a8e06d7ff8.fjt.as.grassdata.net:2333

    # proxy_user = 'a477c1a8e06d7ff8.qzc.na.grassdata.net:2333:PsFaJMphAU0hH1s20E-zone-custom-region-us-st-colorado-session-153f3w4uh-sessTime-10:iWrz7GbWhm'
    return {
        "host": 'a477c1a8e06d7ff8.qzc.na.grassdata.net',
        "port": 2333,
        "user": proxy_user,
        "password": 'iWrz7GbWhm'
    }


def resolve_account_proxy(account: Dict, suffix, static_ip_count: int = 0, static_ip_pool: List = None):
    row_proxy_raw = str(account.get("proxy") or "").strip()
    if row_proxy_raw:
        row_proxy = _normalize_single_proxy_item(row_proxy_raw)
        if not row_proxy:
            raise ValueError("proxy 格式错误，应为 hostname:port:username:password 或 http://user:pass@host:port")
        return row_proxy, proxy_to_url(row_proxy)

    use_static_proxy = bool(_static_proxy_for_suffix(static_ip_pool, static_ip_count, suffix))
    proxy_ = build_proxy_user(account, suffix, static_ip_count=static_ip_count, static_ip_pool=static_ip_pool)
    return proxy_, proxy_to_url(proxy_) if use_static_proxy else ""


def get_random_os_config():
    """
    根据真实人群分布，随机返回一套操作系统配置
    """
    # 定义几种常见的配置模板
    # 权重设置：Win10 最多，Mac 其次，Win11 少量
    configs = [
        # 1. Windows 10 (主力) - 权重 60%
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "10", "coreProduct": "chrome"},

        # 2. macOS (高价值用户) - 权重 20%
        # 注意：Mac 的 platform 是 MacIntel，且没有 osVersion 细分（通常留空或填具体版本）
        {"os": "MacIntel", "osVersion": "", "coreProduct": "chrome"},
        {"os": "MacIntel", "osVersion": "", "coreProduct": "chrome"},

        # 3. Windows 11 (新用户) - 权重 15%
        {"os": "Win32", "osVersion": "11", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "11", "coreProduct": "chrome"},
        {"os": "Win32", "osVersion": "11", "coreProduct": "chrome"},

        # 4. Linux (极客/开发者) - 权重 5%
        {"os": "Linux x86_64", "osVersion": "", "coreProduct": "chrome"}
    ]

    return random.choice(configs)


def create_diverse_browsers(count=10):
    url = "http://127.0.0.1:53325/browser/update"

    for i in range(count):
        # 1. 获取随机系统配置
        os_config = get_random_os_config()

        # 2. 构建指纹 (只传核心字段，其余交给系统)
        fingerprint = {
            "coreProduct": os_config["coreProduct"],
            "coreVersion": "130",  # 内核版本保持最新
            "ostype": "PC",
            "os": os_config["os"],  # 随机系统 (Win/Mac/Linux)
            "osVersion": os_config["osVersion"],  # 随机版本
            "version": "130"
        }

        payload = {
            "name": f"Amazon_{os_config['os']}_{i}",
            "groupId": "你的分组ID",
            "browserFingerPrint": fingerprint,
            "proxyMethod": 0
        }

        response = requests.post(url, json=payload, headers={'Content-Type': 'application/json'})
        print(f"创建 {os_config['os']}: {response.json().get('success')}")



# ====================== 工具：创建指纹浏览器 ======================
def create_browser(account: Dict, suffix, static_ip_count: int = 0, static_ip_pool: List = None) -> str:
    """
    调用BitBrowser接口创建浏览器，返回 browser_id
    """
    username = account["username"]
    country = account["country"]

    cookies = account["cookies"]

    # 生成代理信息
    proxy_, static_ip = resolve_account_proxy(account, suffix, static_ip_count, static_ip_pool)

    os_config = get_random_os_config()

    # 2. 构建指纹 (只传核心字段，其余交给系统)
    fingerprint = {
        "coreProduct": os_config["coreProduct"],
        "coreVersion": "146",  # 内核版本保持最新
        "ostype": "PC",
        "os": os_config["os"],  # 随机系统 (Win/Mac/Linux)
        "osVersion": os_config["osVersion"],  # 随机版本
        "version": "146"
    }

    # 构造请求参数
    data = {
        # "id": f"{country}_{username}",  # 唯一ID
        "name": f"{country}_{username}",
        "remark": f"{country}_{username}",
        "browserFingerPrint":fingerprint,
        "proxyMethod": 2,
        "proxyType": "http",
        "host": proxy_["host"],
        "port": proxy_["port"],
        "ipCheckService":'IP2Location',
        "proxyUserName": proxy_['user'],
        "proxyPassword": proxy_['password'],
        'url':'',
        "cookie": cookies,
        'abortImage':True, #禁止加载图片
        "abortMedia":True

    }

    try:
        resp = requests.post(f"{BIT_BROWSER_API_URL}/browser/update", json=data)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("success"):
                browser_id = result["data"]["id"]
                print(f"✅ 创建成功：{username} | browser_id: {browser_id}")
                proxy = proxy_to_url(proxy_)
                proxies = {
                    'http': proxy,
                    'https': proxy,
                }
                return browser_id, proxies, static_ip
            else:
                print(f"❌ 创建失败：{username} | {result}")
        else:
            print(f"❌ 请求失败：{username} | {resp.text}")
    except Exception as e:
        print(f"❌ 异常：{username} | {str(e)}")
    return None, None, ""

# ====================== 主流程 ======================
def batch_create_and_build_accounts(file, country, static_ip_count: int = 0, limit: int = 0,
                                    return_report: bool = False, static_ip_pool: List = None) -> List[Dict]:
    """
    批量创建指纹浏览器，并生成可入库的账号结构
    """
    final_accounts = []
    failed_items = []
    raw_accounts = read_excel_to_accounts(file,country)
    db = MySQLTaskDB()
    attempted_rows = 0
    static_pool_assign_index = 0
    existing_browser_count = 0
    file_proxy_count = 0
    try:
        db.ensure_static_ip_column()
        for acc in raw_accounts:
            if limit and len(final_accounts) >= int(limit):
                break
            attempted_rows += 1
            acc['username'] = normalize_account_username(acc.get("username"), country)

            try:
                row_has_proxy = bool(str(acc.get("proxy") or "").strip())
                if row_has_proxy:
                    proxy_suffix = attempted_rows
                    file_proxy_count += 1
                else:
                    static_pool_assign_index += 1
                    proxy_suffix = static_pool_assign_index

                if acc.get("browser_id"):
                    browser_id = str(acc.get("browser_id") or "").strip()
                    proxy_, static_ip = resolve_account_proxy(
                        acc,
                        proxy_suffix,
                        static_ip_count=static_ip_count,
                        static_ip_pool=static_ip_pool,
                    )
                    proxy_url = proxy_to_url(proxy_)
                    proxy_ = {"http": proxy_url, "https": proxy_url}
                    existing_browser_count += 1
                else:
                    browser_id, proxy_, static_ip = create_browser(
                        acc,
                        proxy_suffix,
                        static_ip_count=static_ip_count,
                        static_ip_pool=static_ip_pool,
                    )
                if not browser_id:
                    failed_items.append({
                        "username": str(acc.get("username", "")),
                        "reason": "create_browser_failed",
                    })
                    continue
                final_account = build_account_record(acc, browser_id, proxy_, static_ip)
                final_accounts.append(final_account)
                time.sleep(0.5)  # 避免请求过快
                db.insert_account(final_account)
            except Exception as e:
                failed_items.append({
                    "username": str(acc.get("username", "")),
                    "reason": str(e)[:240],
                })
    finally:
        db.close()
    if return_report:
        return {
            "source_rows": len(raw_accounts),
            "attempted_rows": attempted_rows,
            "success_count": len(final_accounts),
            "failed_count": len(failed_items),
            "failed_items": failed_items,
            "accounts": final_accounts,
            "existing_browser_count": existing_browser_count,
            "file_proxy_count": file_proxy_count,
        }
    return final_accounts

# ====================== 执行 ======================
if __name__ == "__main__":
    # build_proxy_user(raw_accounts[0])
    accounts = batch_create_and_build_accounts('/Users/edy/Documents/us2.xlsx','UK')
    # print("\n" + "="*50)
    # print("最终可入库账号列表：")
    # print(json.dumps(accounts, indent=1, ensure_ascii=False))

    # print(_normalize_single_proxy_item('82.29.20.99:2333:PsFaJMphAU0hH1s20E:iWrz7GbWhm'))
    # db = MongoAccountDB()
    # db.coll_accounts.insert_many(accounts)
