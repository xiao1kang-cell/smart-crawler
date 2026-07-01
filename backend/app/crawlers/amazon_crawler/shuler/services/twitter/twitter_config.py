import os

# 先加载本地 env / Nacos，后续 os.getenv 才能拿到配置中心的值。
from app.crawlers.amazon_crawler.shuler.util import config as _global_config  # noqa: F401

BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")

REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.0
COOLDOWN_429_SECONDS = 900
MAX_CONSEC_401 = 3
MAX_TASK_RETRIES = int(os.getenv("TWITTER_MAX_TASK_RETRIES", "5"))

TWITTER_WORKER_THREADS = int(os.getenv("TWITTER_WORKER_THREADS", "5"))
EMPTY_SLEEP_SECONDS = 10
NO_ACCOUNT_SLEEP_SECONDS = int(os.getenv("TWITTER_NO_ACCOUNT_SLEEP_SECONDS", "30"))
AUTH_REFRESH_WAIT_SECONDS = int(os.getenv("TWITTER_AUTH_REFRESH_WAIT_SECONDS", "120"))
DOC_ID_REFRESH_WAIT_SECONDS = int(os.getenv("TWITTER_DOC_ID_REFRESH_WAIT_SECONDS", "30"))

SEARCH_MAX_PAGES = int(os.getenv("TWITTER_SEARCH_MAX_PAGES", "1"))
REPLIES_MAX_PAGES = int(os.getenv("TWITTER_REPLIES_MAX_PAGES", "1"))

TWEET_TABLE_PREFIX = "twitter_tweets_"

DOC_ID_DEBOUNCE_SECONDS = 60

TASK_SEARCH = "search"
TASK_TWEET_DETAIL = "tweet_detail"
TASK_TWEET_REPLIES = "tweet_replies"
