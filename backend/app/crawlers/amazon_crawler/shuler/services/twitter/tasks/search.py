import json
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from loguru import logger

_CREATED_AT_FMT = "%a %b %d %H:%M:%S %z %Y"


def parse_search_response(resp: dict) -> Tuple[List[Dict], Optional[str]]:
    """
    解析 SearchTimeline GraphQL 响应。
    返回 (tweets, next_cursor)，next_cursor 为 None 表示没有更多页。
    """
    tweets = []
    cursor = None
    try:
        instructions = (
            resp.get("data", {})
                .get("search_by_raw_query", {})
                .get("search_timeline", {})
                .get("timeline", {})
                .get("instructions", [])
        )
        for instr in instructions:
            if instr.get("type") != "TimelineAddEntries":
                continue
            for entry in instr.get("entries", []):
                content = entry.get("content", {})

                if content.get("cursorType") == "Bottom":
                    cursor = content.get("value")
                    continue

                tweet_result = (
                    content.get("itemContent", {})
                           .get("tweet_results", {})
                           .get("result", {})
                )
                if not tweet_result:
                    continue

                tweet = _extract_tweet(tweet_result)
                if tweet:
                    tweets.append(tweet)
    except Exception as e:
        logger.warning(f"[search] 解析响应失败: {e}")
    return tweets, cursor


def _extract_tweet(result: dict) -> Optional[Dict]:
    """从单条 tweet_result 提取标准化字段"""
    try:
        legacy = result.get("legacy", {})
        user_result = (
            result.get("core", {})
                  .get("user_results", {})
                  .get("result", {})
        )
        user_legacy = user_result.get("legacy", {})
        user_id = user_result.get("rest_id", "")

        created_at_raw = legacy.get("created_at", "")
        tweet_created_at = None
        if created_at_raw:
            try:
                dt = datetime.strptime(created_at_raw, _CREATED_AT_FMT)
                tweet_created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                tweet_created_at = None

        return {
            "tweet_id": result.get("rest_id", ""),
            "author_id": user_id,
            "author_name": user_legacy.get("name", ""),
            "author_screen": user_legacy.get("screen_name", ""),
            "content": legacy.get("full_text", ""),
            "lang": legacy.get("lang", ""),
            "like_count": int(legacy.get("favorite_count", 0)),
            "retweet_count": int(legacy.get("retweet_count", 0)),
            "reply_count": int(legacy.get("reply_count", 0)),
            "quote_count": int(legacy.get("quote_count", 0)),
            "bookmark_count": int(legacy.get("bookmark_count", 0)),
            "tweet_created_at": tweet_created_at,
            "query_keyword": "",
            "parent_tweet_id": "",
        }
    except Exception as e:
        logger.debug(f"[search] 提取推文字段失败: {e}")
        return None


def build_search_params(keyword: str, lang: str = "", cursor: str = None) -> dict:
    """构造 SearchTimeline 请求参数"""
    raw_query = keyword if not lang else f"{keyword} lang:{lang}"
    variables = {
        "rawQuery": raw_query,
        "count": 20,
        "querySource": "typed_query",
        "product": "Latest",
    }
    if cursor:
        variables["cursor"] = cursor

    features = {
        "rweb_video_screen_enabled": False,
        "rweb_cashtags_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "articles_preview_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
    }
    return {
        "variables": json.dumps(variables, ensure_ascii=False),
        "features": json.dumps(features, ensure_ascii=False),
    }
