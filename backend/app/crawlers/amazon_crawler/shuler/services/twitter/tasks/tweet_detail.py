import json
from typing import Dict, Optional

from loguru import logger
from app.crawlers.amazon_crawler.shuler.services.twitter.tasks.search import _extract_tweet


def parse_tweet_detail_response(resp: dict) -> Optional[Dict]:
    """解析 TweetResultByRestId 响应，返回单条推文 dict"""
    try:
        result = resp.get("data", {}).get("tweetResult", {}).get("result", {})
        if not result:
            return None
        return _extract_tweet(result)
    except Exception as e:
        logger.warning(f"[tweet_detail] 解析失败: {e}")
        return None


def build_tweet_detail_params(tweet_id: str) -> dict:
    """构造 TweetResultByRestId 请求参数"""
    variables = {
        "tweetId": tweet_id,
        "withCommunity": False,
        "includePromotedContent": False,
        "withVoice": False,
    }
    features = {
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "articles_preview_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "longform_notetweets_consumption_enabled": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": False,
        "view_counts_everywhere_api_enabled": True,
    }
    return {
        "variables": json.dumps(variables),
        "features": json.dumps(features),
    }
