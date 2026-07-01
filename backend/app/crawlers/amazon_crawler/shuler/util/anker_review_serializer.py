"""Normalize Amazon review rows to Anker's expected field contract."""
from __future__ import annotations

import re
from typing import Any


COUNTRY_REGION_NAMES = {
    "JP": "日本",
    "JAPAN": "日本",
    "US": "United States",
    "UNITED STATES": "United States",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "UNITED KINGDOM": "United Kingdom",
    "DE": "Deutschland",
    "GERMANY": "Deutschland",
    "FR": "France",
    "CA": "Canada",
    "IT": "Italia",
    "ES": "España",
}


def normalize_review_result(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    return [_normalize_one(row) for row in rows if isinstance(row, dict)]


def _normalize_one(row: dict[str, Any]) -> dict[str, Any]:
    asin = _str_value(row.get("asin") or row.get("real_asin"))
    rating = _rating_value(row.get("rating", row.get("star_rating", row.get("score", row.get("star_rate")))))
    images = _image_list(row.get("images", row.get("image_urls")))
    videos = _list_value(row.get("videos", row.get("video_urls")))
    is_locale_review = _bool_value(row.get("isReviewLocal", row.get("is_locale_review")), default=True)
    attributes = _attributes(row.get("dimension", row.get("attributes")))
    color = _color_value(row.get("color"), attributes)
    return {
        "review_id": _str_value(row.get("reviewId", row.get("review_id"))),
        "title": _str_value(row.get("reviewTitle", row.get("title", row.get("review_title")))),
        "useful_num": _int_value(row.get("helpfulNum", row.get("useful_num", row.get("helpful_num")))),
        "score": rating,
        "star_rate": rating,
        "date": _str_value(row.get("reviewDate", row.get("date", row.get("review_date")))),
        "region": _region_name(row),
        "is_locale_review": is_locale_review,
        "author": _str_value(row.get("reviewerName", row.get("author", row.get("reviewer_name")))),
        "author_id": _str_value(row.get("reviewerId", row.get("author_id", row.get("reviewer_id")))),
        "is_purchased": _bool_value(row.get("isVP", row.get("is_purchased", row.get("verified_purchase")))),
        "color": color,
        "asin": asin,
        "real_asin": _str_value(row.get("real_asin")) or asin,
        "variations": _variations(row, asin, attributes),
        "is_hall_of_fame": _bool_value(row.get("is_hall_of_fame")),
        "is_from_outside": _bool_value(row.get("is_from_outside"), default=not is_locale_review),
        "review_text": _str_value(row.get("comment", row.get("review_text", row.get("review_body", row.get("content"))))),
        "comment_num": _int_value(row.get("comment_num")),
        "has_image": _bool_value(row.get("has_image"), default=bool(images)),
        "images": images,
        "has_video": _bool_value(row.get("hasVideo", row.get("has_video")), default=bool(videos)),
        "is_early_reviewer_rewards": _bool_value(row.get("earlyReviewer", row.get("is_early_reviewer_rewards"))),
        "is_vine_voice": _bool_value(row.get("isVineVoice", row.get("is_vine_voice"))),
        "is_vine_customer_review_of_free_product": _bool_value(
            row.get("is_vine_customer_review_of_free_product")
        ),
    }


def _str_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def _int_value(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _rating_value(value: Any) -> str:
    if value is None or value == "":
        return "0.0"
    try:
        return f"{float(str(value).strip()):.1f}"
    except (TypeError, ValueError):
        return _str_value(value)


def _list_value(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _image_list(value: Any) -> list[str]:
    images: list[str] = []
    for item in _list_value(value):
        raw = _str_value(item)
        if not raw:
            continue
        parts = re.findall(r"https?://.+?(?=https?://|$)", raw)
        images.extend(part.strip() for part in parts if part.strip())
    return images


def _attributes(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [f"{key}: {val}" for key, val in value.items() if val not in (None, "")]
    return [_str_value(item) for item in _list_value(value) if _str_value(item)]


def _color_value(value: Any, attributes: list[str]) -> str:
    explicit = _str_value(value)
    if explicit:
        return explicit
    for attr in attributes:
        if ":" not in attr:
            continue
        key, val = attr.split(":", 1)
        if key.strip().lower() in {"color", "colour", "カラー", "色"}:
            return val.strip()
    return ""


def _variations(row: dict[str, Any], asin: str, attributes: list[str]) -> list[dict[str, Any]]:
    variations = row.get("variations")
    if isinstance(variations, list) and variations:
        return variations
    return [{"asin": _str_value(row.get("real_asin")) or asin, "attributes": attributes}]


def _region_name(row: dict[str, Any]) -> str:
    raw = _str_value(row.get("region", row.get("countryCode", row.get("country"))))
    if not raw:
        return ""
    return COUNTRY_REGION_NAMES.get(raw.upper(), raw)
