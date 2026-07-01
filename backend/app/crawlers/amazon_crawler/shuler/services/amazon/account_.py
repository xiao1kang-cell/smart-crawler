import time
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import REFRESH_TIME


# ------------------------------
# 数据模型：账号实体
# ------------------------------
@dataclass
class Account:
    username: str
    password: str
    country: str
    cookies: Dict
    fingerprint_id: str
    last_used_time: float
    is_used: bool
    fail_count: int
    last_used_time: float = 0.0
    is_used: bool = field(init=False)
    last_used_time: float = 0.0
    is_used: bool = field(init=False)
    last_used_time: float = 0.0
    is_used: bool = False
    fail_count: int = 0
    proxy_:dict = field(default_factory=dict)
    cooldown_until: float = 0.0  # 冷却结束时间戳
    totp_secret: str = ""
    state: int =1
    city: str=''
    update_time :str= time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    refresh_time: str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - REFRESH_TIME*3600))
    user_agent:str=''
    platform: str = "amazon"
    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: Dict) -> "Account":
        return cls(
            username=data["username"],
            password=data["password"],
            totp_secret=data.get("totp_secret", ''),
            state=data.get("state", 1),
            country=data.get("country", "us"),
            cookies=data.get("cookies") if isinstance(data.get("cookies"), dict) else {},
            proxy_=data.get("proxy_") if isinstance(data.get("proxy_"), dict) else {},
            fingerprint_id=data.get("fingerprint_id", ""),
            last_used_time=float(data.get("last_used_time", 0.0)),
            is_used=bool(data.get("is_used", False)),
            fail_count=int(data.get("fail_count", 0)),
            cooldown_until=float(data.get("cooldown_until", 0.0)),
            update_time=data.get('update_time', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())),
            refresh_time=data.get('refresh_time', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - REFRESH_TIME*3600))),
            user_agent=data.get('user_agent', ''),
            platform=data.get('platform', 'amazon'),
        )

    @classmethod
    def from_mongo_dict(cls, data: Dict) -> "Account":
        return cls.from_dict(data)
