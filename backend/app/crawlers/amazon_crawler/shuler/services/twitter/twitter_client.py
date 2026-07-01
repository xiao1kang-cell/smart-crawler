import base64
import os
import time
import random
from typing import Dict, Any

from curl_cffi.requests import Session
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import (
    BEARER_TOKEN, REQUEST_DELAY_MIN, REQUEST_DELAY_MAX,
)


class RateLimitError(Exception):
    pass


class AuthError(Exception):
    pass


class DocIdExpiredError(Exception):
    pass


class TwitterClient:
    """
    推特 GraphQL HTTP 客户端。
    一个 Worker 线程持有一个 TwitterClient 实例（不跨线程共享）。
    """

    def __init__(self, doc_fetcher, cookie_refresher, account_manager):
        self._doc_fetcher = doc_fetcher
        self._cookie_refresher = cookie_refresher
        self._account_manager = account_manager
        self._session = Session(impersonate="chrome120")

    def get(self, url: str, params: Dict, account: Account) -> Any:
        """发起 GET 请求，返回 JSON。遇到 401/429/404 抛出对应异常。"""
        headers = self._build_headers(account)
        cookies = {
            "auth_token": account.cookies.get("auth_token", ""),
            "ct0": account.cookies.get("ct0", ""),
        }

        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

        resp = self._session.get(
            url, headers=headers, cookies=cookies, params=params, timeout=20
        )

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 401:
            logger.warning(f"[TwitterClient] 401 账号 Cookie 过期: {account.username}")
            raise AuthError(f"401: {account.username}")

        if resp.status_code == 429:
            logger.warning(f"[TwitterClient] 429 限速: {account.username}")
            raise RateLimitError(f"429: {account.username}")

        if resp.status_code == 404:
            logger.warning(f"[TwitterClient] 404 doc_id 可能已失效: {url}")
            self._doc_fetcher.trigger()
            raise DocIdExpiredError(f"404: {url}")

        raise RuntimeError(f"推特请求异常: HTTP {resp.status_code}, url={url}")

    @staticmethod
    def _build_headers(account: Account) -> Dict[str, str]:
        ct0 = account.cookies.get("ct0", "")
        transaction_id = base64.b64encode(os.urandom(60)).decode().rstrip("=")
        return {
            "authorization": f"Bearer {BEARER_TOKEN}",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": transaction_id,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "user-agent": (
                account.user_agent or
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
