from datetime import datetime
from typing import Optional

from app.crawlers.amazon_crawler.shuler.services.twitter.models import TwitterTaskRecord
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import (
    MAX_TASK_RETRIES,
    TWEET_TABLE_PREFIX,
)
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


class TwitterTaskRepository:
    """Twitter 任务和结果表的数据库访问边界。"""

    def __init__(self, mysql_db: MySQLTaskDB):
        self._db = mysql_db

    @staticmethod
    def tweet_table_name(now: Optional[datetime] = None) -> str:
        current = now or datetime.now()
        return f"{TWEET_TABLE_PREFIX}{current.strftime('%Y%m%d')}"

    def init_schema(self):
        self._db.init_twitter_tables()

    def poll_task(self) -> Optional[TwitterTaskRecord]:
        row = self._db.poll_twitter_task()
        return TwitterTaskRecord.from_row(row) if row else None

    def ensure_tweet_table(self, table_name: str):
        self._db.create_twitter_tweet_table(table_name)

    def insert_tweets(self, table_name: str, rows):
        self._db.insert_twitter_tweets(table_name, rows)

    def mark_success(self, task_id: int, result_count: int):
        self._db.update_twitter_task(
            task_id,
            status=2,
            result_count=result_count,
            error_msg="",
        )

    def mark_pending(self, task_id: int, error_msg: str = ""):
        self._db.update_twitter_task(task_id, status=0, error_msg=error_msg[:500])

    def mark_retry(
        self,
        task: TwitterTaskRecord,
        error_msg: str,
        *,
        increment_retry: bool = True,
        max_retries: int = MAX_TASK_RETRIES,
    ) -> bool:
        retry_count = task.retry_count + 1 if increment_retry else task.retry_count
        if retry_count > max_retries:
            self.mark_failed(
                task.id,
                f"{error_msg}；超过最大重试次数 {max_retries}",
                retry_count=retry_count,
            )
            return False

        self._db.update_twitter_task(
            task.id,
            status=0,
            retry_count=retry_count,
            error_msg=error_msg[:500],
        )
        return True

    def mark_failed(self, task_id: int, error_msg: str, **extra):
        update = {
            "status": 3,
            "error_msg": error_msg[:500],
        }
        update.update(extra)
        self._db.update_twitter_task(task_id, **update)
