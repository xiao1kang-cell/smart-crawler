from typing import Protocol, runtime_checkable, Dict
from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account


@runtime_checkable
class LoginHandler(Protocol):
    platform: str

    def login(self, account: Account) -> Dict[str, str]:
        """登录并返回新 cookies dict，失败时抛出异常"""
        ...
