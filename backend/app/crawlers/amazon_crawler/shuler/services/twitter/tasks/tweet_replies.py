import json
from typing import List, Dict, Optional, Tuple

from loguru import logger
from app.crawlers.amazon_crawler.shuler.services.twitter.tasks.search import _extract_tweet


def parse_replies_response(
    resp: dict, parent_tweet_id: str
) -> Tuple[List[Dict], Optional[str]]:
    """
    解析 TweetDetail（threaded_conversation）响应，提取评论列表。
    跳过父推文自身，只返回直接回复。
    """
    tweets = []
    cursor = None
    try:
        instructions = (
            resp.get("data", {})
                .get("threaded_conversation_with_injections_v2", {})
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
                    for item in content.get("items", []):
                        tr = (
                            item.get("item", {})
                                .get("itemContent", {})
                                .get("tweet_results", {})
                                .get("result", {})
                        )
                        if tr:
                            t = _extract_tweet(tr)
                            if t and t["tweet_id"] != parent_tweet_id:
                                t["parent_tweet_id"] = parent_tweet_id
                                tweets.append(t)
                    continue

                t = _extract_tweet(tweet_result)
                if t and t["tweet_id"] != parent_tweet_id:
                    t["parent_tweet_id"] = parent_tweet_id
                    tweets.append(t)
    except Exception as e:
        logger.warning(f"[tweet_replies] 解析失败: {e}")
    return tweets, cursor


def build_replies_params(tweet_id: str, cursor: str = None) -> dict:
    """构造 TweetDetail（评论模式）请求参数"""
    variables = {
        "focalTweetId": tweet_id,
        "count": 20,
        "includePromotedContent": False,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": False,
        "withBirdwatchNotes": True,
        "withVoice": True,
        "withV2Timeline": True,
    }
    if cursor:
        variables["cursor"] = cursor

    features = {
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "articles_preview_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "view_counts_everywhere_api_enabled": True,
    }
    return {
        "variables": json.dumps(variables),
        "features": json.dumps(features),
    }
