"""
推特 Worker 入口。

启动：
    python -m amazon_crawler.shuler.services.twitter.worker_main
"""
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

import redis as redis_lib
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.twitter.doc_id_fetcher import DocIdFetcher
from app.crawlers.amazon_crawler.shuler.services.twitter.repository import TwitterTaskRepository
from app.crawlers.amazon_crawler.shuler.services.twitter.runner import TwitterTaskRunner
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_client import TwitterClient
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import (
    EMPTY_SLEEP_SECONDS,
    TWITTER_WORKER_THREADS,
)
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager
from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_USERNAME,
    setup_logger,
)
from app.crawlers.amazon_crawler.shuler.util.cookie_refresher import CookieRefresher
from app.crawlers.amazon_crawler.shuler.util.login_handlers.twitter import TwitterLoginHandler
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


def _run_worker(
    worker_id: int,
    doc_fetcher: DocIdFetcher,
    cookie_refresher: CookieRefresher,
):
    """单个 worker 线程的主循环。"""
    mysql_db = MySQLTaskDB()
    account_manager = HumanLikeAccountManager(
        worker_id=f"twitter_worker_{worker_id}",
        platform="twitter",
    )
    client = TwitterClient(
        doc_fetcher=doc_fetcher,
        cookie_refresher=cookie_refresher,
        account_manager=account_manager,
    )
    runner = TwitterTaskRunner(
        worker_id=worker_id,
        repository=TwitterTaskRepository(mysql_db),
        client=client,
        doc_fetcher=doc_fetcher,
        cookie_refresher=cookie_refresher,
        account_manager=account_manager,
    )

    try:
        while True:
            had_work = runner.run_once()
            if not had_work:
                time.sleep(EMPTY_SLEEP_SECONDS)
    finally:
        mysql_db.close()


def _init_schema():
    db = MySQLTaskDB()
    try:
        TwitterTaskRepository(db).init_schema()
    finally:
        db.close()


def _build_redis_client():
    return redis_lib.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        username=REDIS_USERNAME,
        password=REDIS_PASSWORD,
        db=REDIS_DB,
        decode_responses=True,
    )


def start_workers():
    setup_logger("twitter_worker")
    _init_schema()

    redis_client = _build_redis_client()
    doc_fetcher = DocIdFetcher(db_factory=MySQLTaskDB)

    refresher_db = MySQLTaskDB()
    cookie_refresher = CookieRefresher(mysql_db=refresher_db, redis_client=redis_client)
    cookie_refresher.register_handler(TwitterLoginHandler())
    cookie_refresher.start()

    try:
        logger.info(f"[twitter-worker] 启动 {TWITTER_WORKER_THREADS} 个 Worker 线程")
        with ThreadPoolExecutor(max_workers=TWITTER_WORKER_THREADS) as pool:
            futures = [
                pool.submit(_run_worker, i, doc_fetcher, cookie_refresher)
                for i in range(TWITTER_WORKER_THREADS)
            ]
            done, _ = wait(futures, return_when=FIRST_EXCEPTION)
            for future in done:
                future.result()
    finally:
        refresher_db.close()


if __name__ == "__main__":
    start_workers()
