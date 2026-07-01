import time
from typing import Dict, Optional

from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.twitter.doc_id_fetcher import DocIdFetcher
from app.crawlers.amazon_crawler.shuler.services.twitter.repository import TwitterTaskRepository
from app.crawlers.amazon_crawler.shuler.services.twitter.task_handlers import (
    TwitterTaskHandler,
    build_default_handlers,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_client import (
    AuthError,
    DocIdExpiredError,
    RateLimitError,
    TwitterClient,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import (
    AUTH_REFRESH_WAIT_SECONDS,
    COOLDOWN_429_SECONDS,
    DOC_ID_REFRESH_WAIT_SECONDS,
    MAX_CONSEC_401,
    NO_ACCOUNT_SLEEP_SECONDS,
)
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager
from app.crawlers.amazon_crawler.shuler.util.cookie_refresher import CookieRefresher


class TwitterTaskRunner:
    """单个 worker 线程内的任务编排器。"""

    def __init__(
        self,
        worker_id: int,
        repository: TwitterTaskRepository,
        client: TwitterClient,
        doc_fetcher: DocIdFetcher,
        cookie_refresher: CookieRefresher,
        account_manager: HumanLikeAccountManager,
        handlers: Optional[Dict[str, TwitterTaskHandler]] = None,
    ):
        self._worker_id = worker_id
        self._repository = repository
        self._client = client
        self._doc_fetcher = doc_fetcher
        self._cookie_refresher = cookie_refresher
        self._account_manager = account_manager
        self._handlers = handlers or build_default_handlers()
        self._consec_401 = 0

    def run_once(self) -> bool:
        task = self._repository.poll_task()
        if task is None:
            return False

        handler = self._handlers.get(task.task_type)
        if handler is None:
            self._repository.mark_failed(task.id, f"未知 Twitter 任务类型: {task.task_type}")
            logger.error(f"[twitter-worker-{self._worker_id}] 未知任务类型: {task.task_type}")
            return True

        account = self._account_manager.get_account({"platform": "twitter"})
        if not account:
            logger.warning(f"[twitter-worker-{self._worker_id}] 无可用账号，任务{task.id}放回")
            self._repository.mark_pending(task.id, "无可用账号")
            time.sleep(NO_ACCOUNT_SLEEP_SECONDS)
            return True

        try:
            table_name = self._repository.tweet_table_name()
            self._repository.ensure_tweet_table(table_name)

            result = handler.execute(task, self._client, self._doc_fetcher, account)
            if result.tweets:
                self._repository.insert_tweets(table_name, result.tweets)

            self._repository.mark_success(task.id, len(result.tweets))
            self._account_manager.release_account(
                account,
                asin=task.input,
                success=True,
                task_id=str(task.id),
                pages_fetched=result.pages_fetched,
            )
            self._consec_401 = 0
            logger.info(
                f"[twitter-worker-{self._worker_id}] 任务{task.id}完成，"
                f"抓取{len(result.tweets)}条，页数{result.pages_fetched}"
            )
            return True

        except AuthError:
            self._handle_auth_error(task, account)
            return True

        except RateLimitError:
            self._handle_rate_limit(task, account)
            return True

        except DocIdExpiredError as exc:
            self._handle_doc_id_error(task, exc)
            return True

        except Exception as exc:
            logger.error(f"[twitter-worker-{self._worker_id}] 任务{task.id}异常: {exc}")
            self._repository.mark_failed(task.id, str(exc))
            self._account_manager.release_account(
                account,
                asin=task.input,
                success=False,
                task_id=str(task.id),
            )
            return True

    def _handle_auth_error(self, task, account):
        self._consec_401 += 1
        logger.warning(
            f"[twitter-worker-{self._worker_id}] 401 "
            f"({self._consec_401}/{MAX_CONSEC_401}): {account.username}"
        )
        event = self._cookie_refresher.request_refresh(account.username, "twitter")
        event.wait(timeout=AUTH_REFRESH_WAIT_SECONDS)
        self._repository.mark_retry(task, f"401 Cookie 失效: {account.username}")
        if self._consec_401 >= MAX_CONSEC_401:
            logger.error(
                f"[twitter-worker-{self._worker_id}] 连续{MAX_CONSEC_401}次 401，短暂停顿"
            )
            time.sleep(NO_ACCOUNT_SLEEP_SECONDS)
            self._consec_401 = 0

    def _handle_rate_limit(self, task, account):
        logger.warning(
            f"[twitter-worker-{self._worker_id}] 429，账号冷却 "
            f"{COOLDOWN_429_SECONDS}s: {account.username}"
        )
        account.cooldown_until = time.time() + COOLDOWN_429_SECONDS
        self._account_manager.force_release()
        self._repository.mark_retry(
            task,
            f"429 账号限速: {account.username}",
            increment_retry=False,
        )

    def _handle_doc_id_error(self, task, exc: DocIdExpiredError):
        logger.warning(
            f"[twitter-worker-{self._worker_id}] doc_id 失效或缺失: {exc}，等待更新后重试"
        )
        self._doc_fetcher.trigger()
        self._repository.mark_retry(task, f"doc_id 失效或缺失: {exc}")
        time.sleep(DOC_ID_REFRESH_WAIT_SECONDS)
