from typing import Dict, List, Optional, Tuple

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from app.crawlers.amazon_crawler.shuler.services.twitter.doc_id_fetcher import DocIdFetcher
from app.crawlers.amazon_crawler.shuler.services.twitter.models import (
    TwitterTaskRecord,
    TwitterTaskResult,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.tasks.search import (
    build_search_params,
    parse_search_response,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.tasks.tweet_detail import (
    build_tweet_detail_params,
    parse_tweet_detail_response,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.tasks.tweet_replies import (
    build_replies_params,
    parse_replies_response,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_client import (
    DocIdExpiredError,
    TwitterClient,
)
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import (
    REPLIES_MAX_PAGES,
    SEARCH_MAX_PAGES,
    TASK_SEARCH,
    TASK_TWEET_DETAIL,
    TASK_TWEET_REPLIES,
)


def _doc_id_url(doc_fetcher: DocIdFetcher, operation_name: str) -> str:
    query_id = doc_fetcher.get(operation_name)
    if not query_id:
        doc_fetcher.trigger()
        raise DocIdExpiredError(f"doc_id 未知: {operation_name}")
    return f"https://x.com/i/api/graphql/{query_id}/{operation_name}"


def _tag_tweets(tweets: List[Dict], task: TwitterTaskRecord):
    for tweet in tweets:
        tweet["task_id"] = task.id
        tweet["task_type"] = task.task_type


class TwitterTaskHandler:
    """单类 Twitter 任务的执行单元。"""

    task_type = ""
    operation_name = ""

    def execute(
        self,
        task: TwitterTaskRecord,
        client: TwitterClient,
        doc_fetcher: DocIdFetcher,
        account: Account,
    ) -> TwitterTaskResult:
        raise NotImplementedError


class SearchTaskHandler(TwitterTaskHandler):
    task_type = TASK_SEARCH
    operation_name = "SearchTimeline"

    def __init__(self, max_pages: int = SEARCH_MAX_PAGES):
        self._max_pages = max(1, max_pages)

    def execute(
        self,
        task: TwitterTaskRecord,
        client: TwitterClient,
        doc_fetcher: DocIdFetcher,
        account: Account,
    ) -> TwitterTaskResult:
        tweets: List[Dict] = []
        cursor: Optional[str] = None
        pages_fetched = 0

        for _ in range(self._max_pages):
            url = _doc_id_url(doc_fetcher, self.operation_name)
            params = build_search_params(task.input, task.lang, cursor=cursor)
            resp = client.get(url, params, account)
            page_tweets, cursor = parse_search_response(resp)
            for tweet in page_tweets:
                tweet["query_keyword"] = task.input
            tweets.extend(page_tweets)
            pages_fetched += 1
            if not cursor:
                break

        _tag_tweets(tweets, task)
        return TwitterTaskResult(tweets=tweets, pages_fetched=pages_fetched)


class TweetDetailTaskHandler(TwitterTaskHandler):
    task_type = TASK_TWEET_DETAIL
    operation_name = "TweetResultByRestId"

    def execute(
        self,
        task: TwitterTaskRecord,
        client: TwitterClient,
        doc_fetcher: DocIdFetcher,
        account: Account,
    ) -> TwitterTaskResult:
        url = _doc_id_url(doc_fetcher, self.operation_name)
        params = build_tweet_detail_params(task.input)
        resp = client.get(url, params, account)
        tweet = parse_tweet_detail_response(resp)
        tweets = [tweet] if tweet else []
        _tag_tweets(tweets, task)
        return TwitterTaskResult(tweets=tweets, pages_fetched=1)


class TweetRepliesTaskHandler(TwitterTaskHandler):
    task_type = TASK_TWEET_REPLIES
    operation_name = "TweetDetail"

    def __init__(self, max_pages: int = REPLIES_MAX_PAGES):
        self._max_pages = max(1, max_pages)

    def execute(
        self,
        task: TwitterTaskRecord,
        client: TwitterClient,
        doc_fetcher: DocIdFetcher,
        account: Account,
    ) -> TwitterTaskResult:
        tweets: List[Dict] = []
        cursor: Optional[str] = None
        pages_fetched = 0

        for _ in range(self._max_pages):
            url = _doc_id_url(doc_fetcher, self.operation_name)
            params = build_replies_params(task.input, cursor=cursor)
            resp = client.get(url, params, account)
            page_tweets, cursor = parse_replies_response(
                resp,
                parent_tweet_id=task.input,
            )
            tweets.extend(page_tweets)
            pages_fetched += 1
            if not cursor:
                break

        _tag_tweets(tweets, task)
        return TwitterTaskResult(tweets=tweets, pages_fetched=pages_fetched)


def build_default_handlers() -> Dict[str, TwitterTaskHandler]:
    handlers: Tuple[TwitterTaskHandler, ...] = (
        SearchTaskHandler(),
        TweetDetailTaskHandler(),
        TweetRepliesTaskHandler(),
    )
    return {handler.task_type: handler for handler in handlers}
