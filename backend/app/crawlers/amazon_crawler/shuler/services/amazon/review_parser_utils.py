import re
import time
from datetime import datetime

from bs4 import BeautifulSoup


PRODUCT_REVIEW_ASIN_RE = re.compile(r"/product-reviews/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
_review_parse_alerted_asins = set()


def truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def should_use_current_format_filter(task: dict) -> bool:
    """Default to current variant; only all_variants=true fetches all variants."""
    query_conditions = (task or {}).get("query_conditions") or {}
    return not truthy(query_conditions.get("all_variants", False))


def should_use_recent_sort(task: dict, date_cutoff=None) -> bool:
    query_conditions = (task or {}).get("query_conditions") or {}
    return not (query_conditions.get("sort_by") == "top_reviews" and not date_cutoff)


def add_recent_sort_param(url: str, enabled: bool) -> str:
    if not enabled or "sortBy=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}sortBy=recent"


def add_current_format_param(url: str, enabled: bool) -> str:
    if not enabled or "formatType=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}formatType=current_format"


def extract_review_asin(block) -> str:
    """Extract the reviewed product ASIN from links inside one review block."""
    candidates = []
    candidates.extend(block.find_all(attrs={"data-hook": "format-strip"}))
    candidates.extend(block.find_all("a", href=True))

    seen = set()
    for elem in candidates:
        elem_id = id(elem)
        if elem_id in seen:
            continue
        seen.add(elem_id)

        href = elem.get("href", "")
        match = PRODUCT_REVIEW_ASIN_RE.search(href)
        if match:
            return match.group(1).upper()

        for link in elem.find_all("a", href=True):
            match = PRODUCT_REVIEW_ASIN_RE.search(link.get("href", ""))
            if match:
                return match.group(1).upper()

    return ""


def infer_review_is_local(block) -> bool:
    """Foreign reviews contain customer_review_foreign-*; all others are local."""
    for elem in block.find_all(id=True):
        if elem.get("id", "").startswith("customer_review_foreign-"):
            return False
    return True


def extract_common_review_fields(block) -> dict:
    """Extract review fields shared by curl and Playwright parsing paths."""
    video_urls = extract_review_videos(block)
    review_asin = extract_review_asin(block)
    images = extract_review_images(block)
    dimension = extract_review_dimension(block)
    fields = {
        "isReviewLocal": 1 if infer_review_is_local(block) else 0,
        "reviewerName": extract_reviewer_name(block),
        "reviewerId": extract_reviewer_id(block),
        "rating": extract_review_rating(block),
        "reviewTitle": extract_review_title(block),
        "isVP": 1 if block.find("span", {"data-hook": "avp-badge"}) else 0,
        "isVineVoice": 1 if block.find("span", {"data-hook": "vine-badge"}) else 0,
        "earlyReviewer": 1 if block.find("span", {"data-hook": "early-reviewer-badge"}) else 0,
        "comment": extract_review_body(block),
        "helpfulNum": extract_helpful_num(block),
        "images": images,
        "has_image": bool(images),
        "hasVideo": bool(video_urls),
        "has_video": bool(video_urls),
        "dimension": dimension,
        "color": extract_review_color(dimension),
        "comment_num": 0,
        "is_hall_of_fame": False,
        "is_vine_customer_review_of_free_product": False,
    }
    if video_urls:
        fields["videos"] = video_urls
    if review_asin:
        fields["asin"] = review_asin
    return fields


def build_review_data(task: dict) -> dict:
    """Build the default review record shape used by both crawlers."""
    task = task or {}
    review_data = {
        "id": task.get("id"),
        "task_id": task.get("task_id"),
        "review_url": "",
        "countryCode": "",
        "country": task.get("country", ""),
        "isReviewLocal": None,
        "reviewDate": "",
        "hasVideo": False,
        "reviewId": "",
        "videos": None,
        "reviewTitle": "",
        "asin": task.get("asin", ""),
        "real_asin": task.get("asin", ""),
        "variations": [{"asin": task.get("asin", ""), "attributes": []}],
        "helpfulNum": "0",
        "reviewerName": "",
        "isVP": 0,
        "isVineVoice": 0,
        "images": None,
        "has_image": False,
        "rating": None,
        "comment": "",
        "comment_num": 0,
        "earlyReviewer": 0,
        "reviewerId": "",
        "dimension": [],
        "color": "",
        "has_video": False,
        "is_hall_of_fame": False,
        "is_from_outside": False,
        "is_vine_customer_review_of_free_product": False,
        "create_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    review_data.update(task)
    enrich_review_data(review_data)
    return review_data


def enrich_review_data(review_data: dict) -> dict:
    asin = (review_data.get("asin") or review_data.get("real_asin") or "").strip()
    real_asin = (review_data.get("real_asin") or asin).strip()
    attributes = review_data.get("dimension") or []
    if isinstance(attributes, dict):
        attributes = [f"{key}: {val}" for key, val in attributes.items() if val not in (None, "")]
    elif not isinstance(attributes, list):
        attributes = [str(attributes)] if attributes else []
    review_data["asin"] = asin
    review_data["real_asin"] = real_asin
    variations = review_data.get("variations")
    if (
            not variations
            or (
                isinstance(variations, list)
                and variations
                and not (variations[0] or {}).get("attributes")
                and attributes
            )
    ):
        review_data["variations"] = [{"asin": real_asin or asin, "attributes": attributes}]
    if not review_data.get("color"):
        review_data["color"] = extract_review_color(attributes)
    images = review_data.get("images") or []
    videos = review_data.get("videos") or []
    review_data["has_image"] = bool(images)
    review_data["has_video"] = bool(review_data.get("hasVideo") or videos)
    review_data["comment_num"] = int(review_data.get("comment_num") or 0)
    review_data["is_hall_of_fame"] = bool(review_data.get("is_hall_of_fame") or False)
    is_local = review_data.get("isReviewLocal")
    review_data["is_from_outside"] = bool(review_data.get("is_from_outside") or (is_local == 0))
    review_data["is_vine_customer_review_of_free_product"] = bool(
        review_data.get("is_vine_customer_review_of_free_product") or False
    )
    return review_data


def validate_review_data(review_data: dict) -> list:
    missing_fields = []
    if not review_data.get("reviewDate"):
        missing_fields.append("reviewDate")
    if not review_data.get("reviewId"):
        missing_fields.append("reviewId")
    if not review_data.get("countryCode"):
        missing_fields.append("countryCode")
    if not review_data.get("rating"):
        missing_fields.append("rating")
    if not review_data.get("review_url"):
        missing_fields.append("review_url")
    if not (
        (review_data.get("reviewTitle") or "").strip()
        or (review_data.get("comment") or "").strip()
    ):
        missing_fields.append("reviewTitle/comment")
    return missing_fields


def alert_review_parse_error(
        *,
        task: dict,
        review_data: dict,
        missing_fields: list,
        block_html: str = "",
        error_msg: str = "",
        source: str = "",
        log=None,
) -> None:
    """Send one incomplete-review alert per ASIN in the current process."""
    task = task or {}
    review_data = review_data or {}
    missing_fields = missing_fields or []
    asin = task.get("asin") or review_data.get("asin") or ""
    if asin in _review_parse_alerted_asins:
        return
    _review_parse_alerted_asins.add(asin)

    date_text = ""
    try:
        block = BeautifulSoup(block_html or "", "html.parser")
        date_elem = block.find("span", {"data-hook": "review-date"})
        date_text = date_elem.get_text(strip=True) if date_elem else ""
    except Exception:
        date_text = ""

    try:
        from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
        country = task.get("country") or review_data.get("country") or ""
        task_id = task.get("task_id") or task.get("id") or review_data.get("task_id") or ""
        review_id = review_data.get("reviewId") or ""
        message = (
            f"[评论解析告警] source={source or '-'}, country={country}, asin={asin}, "
            f"task_id={task_id}, reviewId={review_id}, "
            f"missing={','.join(missing_fields) or '-'}, "
            f"date={date_text[:120] or '-'}, error={(error_msg or '-')[:200]}"
        )
        send_custom_robot_group_message(message)
    except Exception as exc:
        if log:
            log.warning(f"[评论解析告警] 发送失败: {exc}")


def parse_review_block_html(
    block_html: str,
    task: dict,
    site_mapping: dict,
    get_site_code,
    log=None,
):
    """Parse one review block HTML and return (review_data, missing_fields)."""
    block = BeautifulSoup(block_html, "html.parser").find()
    if not block:
        return None, ["block"]

    task = task or {}
    review_data = build_review_data(task)
    review_data["reviewId"] = block.get("id", "")
    review_data.update(extract_common_review_fields(block))

    parse_review_date(review_data, block, task.get("country", ""), log)

    if review_data["countryCode"]:
        site_code = (get_site_code(review_data["countryCode"]) if get_site_code else "") or ""
        country_upper = (task.get("country") or "").upper()
        site = site_mapping.get(site_code.upper(), site_mapping.get(country_upper, "")) if site_mapping else ""
    if site:
        review_data["review_url"] = f'https://{site}/gp/customer-reviews/{review_data["reviewId"]}'

    enrich_review_data(review_data)
    return review_data, validate_review_data(review_data)


def extract_reviewer_name(block) -> str:
    elem = block.find("span", class_="a-profile-name")
    return elem.get_text(strip=True) if elem else ""


def extract_reviewer_id(block) -> str:
    elem = block.find(None, class_="a-profile")
    if not elem or "href" not in elem.attrs:
        return ""
    match = re.findall(r"\.account\.([A-Z0-9]+)", elem["href"])
    return match[0] if match else ""


def extract_review_rating(block):
    elem = block.find(None, {"data-hook": re.compile(r"review-star-rating$")})
    if not elem or not elem.find(None, {"class": "a-icon-alt"}):
        return None
    match = re.search(r"a-star-(\d+)", " ".join(elem.get("class", [])))
    return int(float(match.group(1))) if match else None


def extract_review_title(block) -> str:
    elem = block.find(None, {"data-hook": "review-title"})
    if not elem:
        return ""
    full_text = elem.get_text()
    star_alt = elem.find(None, {"class": "a-icon-alt"})
    star_text = star_alt.get_text().strip() if star_alt else ""
    return " ".join(full_text.replace(star_text, "").strip().split())


def extract_review_body(block) -> str:
    elem = block.find("span", {"data-hook": "review-body"})
    return elem.get_text(strip=True) if elem else ""


def extract_helpful_num(block) -> str:
    elem = block.find("span", {"data-hook": "helpful-vote-statement"})
    if not elem:
        return "0"
    match = re.search(r"(\d+(,\d+)?)", elem.get_text(strip=True))
    return match.group(1).replace(",", "") if match else "1"


def extract_review_images(block) -> list:
    elems = block.find_all("img", {"data-hook": re.compile(r"review-image-tile$")})
    return [img.get("src") for img in elems] if elems else []


def extract_review_videos(block) -> list:
    elems = block.find_all("div", attrs={"data-video-url": True})
    return [elem.get("data-video-url") for elem in elems]


def extract_review_dimension(block) -> list:
    elem = block.find(None, {"data-hook": "format-strip"}) or block.find(
        "a",
        class_="a-size-mini a-link-normal a-color-secondary",
    )
    if not elem:
        return []
    text = elem.get_text(strip=True)
    return [text] if text else []


def extract_review_color(dimension: list) -> str:
    for item in dimension or []:
        text = str(item or "").strip()
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        if key.strip().lower() in {"color", "colour", "カラー", "色"}:
            return value.strip()
    return ""


def parse_review_date(review_data: dict, block, task_country: str, log=None):
    """Parse Amazon review date/country into review_data."""
    date_obj = None
    review_data["countryCode"] = ""
    review_data["reviewDate"] = ""

    date_elem = block.find("span", {"data-hook": "review-date"})
    if not date_elem:
        return
    date_text = date_elem.get_text(strip=True)
    if not date_text:
        return

    try:
        country_upper = (task_country or "").upper()

        if "Reviewed in " in date_text and " on " in date_text:
            country_part = date_text.split("Reviewed in ", 1)[1].split(" on ", 1)[0].strip()
            if country_part.lower().startswith("the "):
                country_part = country_part[4:].strip()
            date_str = date_text.split(" on ")[1].strip()
            review_data["countryCode"] = country_part
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
                try:
                    date_obj = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    pass

        elif country_upper == "DE":
            country_part = ""
            german_months = {
                "Januar": "01", "Februar": "02", "März": "03", "April": "04",
                "Mai": "05", "Juni": "06", "Juli": "07", "August": "08",
                "September": "09", "Oktober": "10", "November": "11", "Dezember": "12",
            }
            month_pattern = "|".join(german_months.keys())
            de_date_match = re.search(rf"(\d{{1,2}})\.\s*({month_pattern})\s+(\d{{4}})", date_text)

            if de_date_match:
                day = de_date_match.group(1)
                month = german_months[de_date_match.group(2)]
                year = de_date_match.group(3)
                date_obj = datetime.strptime(f"{day.zfill(2)}.{month}.{year}", "%d.%m.%Y")

                remaining_after = date_text[de_date_match.end():].strip()
                if remaining_after:
                    for kw in ["in ", "aus "]:
                        if remaining_after.startswith(kw):
                            country_part = remaining_after[len(kw):].strip()
                            break
                else:
                    for kw in [" aus ", " in "]:
                        if kw in date_text:
                            after_kw = date_text.split(kw, 1)[1]
                            for dk in [" vom ", " am"]:
                                if dk in after_kw:
                                    country_part = after_kw.split(dk, 1)[0].strip()
                                    break
                            break
            elif "Reviewed in " in date_text and " on " in date_text:
                country_part = date_text.split("Reviewed in ")[1].split(" on ")[0].strip()
                date_str = date_text.split(" on ")[1].strip()
                try:
                    date_obj = datetime.strptime(date_str, "%d %B %Y")
                except ValueError:
                    date_obj = datetime.strptime(date_str, "%d %b %Y")

            review_data["countryCode"] = country_part.strip()

        elif country_upper == "IT":
            country_part = ""
            date_part = ""
            for prefix in [" in ", " negli ", " nel ", " nei "]:
                if prefix in date_text:
                    after_prefix = date_text.split(prefix, 1)[1]
                    for sep in [" il ", " in data "]:
                        if sep in after_prefix:
                            country_part, date_part = after_prefix.split(sep, 1)
                            break
                    if date_part:
                        break

            review_data["countryCode"] = country_part.strip()
            italian_months = {
                "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
                "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
                "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12",
            }
            date_parts = date_part.strip().split()
            if len(date_parts) == 3 and date_parts[1].lower() in italian_months:
                month = italian_months[date_parts[1].lower()]
                date_obj = datetime.strptime(f"{date_parts[0].strip().zfill(2)}.{month}.{date_parts[2]}", "%d.%m.%Y")

        elif country_upper == "JP":
            jp_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_text)
            if jp_match:
                year, month, day = jp_match.groups()
                review_data["countryCode"] = "Japan"
                date_obj = datetime.strptime(f"{year}-{month.zfill(2)}-{day.zfill(2)}", "%Y-%m-%d")

        elif country_upper == "FR":
            country_part = ""
            date_part = ""
            if " le " in date_text:
                before_le, date_part = date_text.rsplit(" le ", 1)
                for sep in [" en ", " au ", " aux ", " à ", " a "]:
                    if sep in before_le:
                        country_part = before_le.rsplit(sep, 1)[1].strip()
                        break
                if not country_part:
                    country_part = before_le.strip()

            review_data["countryCode"] = country_part.strip()
            fr_months = {
                "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
                "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
                "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
                "novembre": "11", "décembre": "12", "decembre": "12",
            }
            fr_match = re.search(r"(\d{1,2})(?:er)?\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})", date_part.strip())
            if fr_match:
                day, month_name, year = fr_match.groups()
                month = fr_months.get(month_name.lower())
                if month:
                    date_obj = datetime.strptime(f"{day.zfill(2)}.{month}.{year}", "%d.%m.%Y")

        elif country_upper in ["ES", "MX"]:
            country_part = ""
            date_part = ""
            if " en " in date_text:
                after_en = date_text.rsplit(" en ", 1)[1]
                if " el " in after_en:
                    country_part, date_part = after_en.split(" el ", 1)
                elif " le " in after_en:
                    country_part, date_part = after_en.split(" le ", 1)

            review_data["countryCode"] = country_part.strip()
            es_fr_months = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
                "janvier": "01", "février": "02", "mars": "03", "avril": "04",
                "mai": "05", "juin": "06", "juillet": "07", "août": "08",
                "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
            }
            date_match1 = re.search(r"(\d+) de (\w+) de (\d{4})", date_part.strip())
            date_match2 = re.search(r"(\d+) (\w+) (\d{4})", date_part.strip())
            if date_match1:
                day, month_name, year = date_match1.groups()
            elif date_match2:
                day, month_name, year = date_match2.groups()
            else:
                day = month_name = year = ""

            if month_name and month_name.lower() in es_fr_months:
                month = es_fr_months[month_name.lower()]
                date_obj = datetime.strptime(f"{day.zfill(2)}.{month}.{year}", "%d.%m.%Y")

        elif country_upper == "BR":
            country_part = ""
            date_part = ""
            pt_months = {
                "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
                "abril": "04", "maio": "05", "junho": "06", "julho": "07",
                "agosto": "08", "setembro": "09", "outubro": "10", "novembro": "11",
                "dezembro": "12",
            }
            if " em " in date_text:
                parts = date_text.rsplit(" em ", 1)
                date_part = parts[1].strip()
                country_raw = parts[0].strip()
                for prep in [" no ", " na ", " nos ", " nas ", " em "]:
                    if prep in country_raw:
                        country_part = country_raw.rsplit(prep, 1)[1].strip()
                        break
                if not country_part:
                    country_part = country_raw

            review_data["countryCode"] = country_part.strip()
            pt_match = re.search(r"(\d{1,2})\s+(?:de\s+)?([A-Za-zÀ-ÿ]+)\s+(?:de\s+)?(\d{4})", date_part)
            if pt_match:
                day, month_name, year = pt_match.groups()
                month = pt_months.get(month_name.lower())
                if month:
                    date_obj = datetime.strptime(f"{day.zfill(2)}.{month}.{year}", "%d.%m.%Y")

        elif country_upper in ["SA", "AE"]:
            country_part = ""
            date_part = ""
            ar_months = {
                "يناير": "01", "كانون الثاني": "01",
                "فبراير": "02", "شباط": "02",
                "مارس": "03", "آذار": "03",
                "أبريل": "04", "نيسان": "04",
                "مايو": "05", "أيار": "05",
                "يونيو": "06", "حزيران": "06",
                "يوليو": "07", "تموز": "07",
                "أغسطس": "08", "آب": "08",
                "سبتمبر": "09", "أيلول": "09",
                "أكتوبر": "10", "تشرين الأول": "10",
                "نوفمبر": "11", "تشرين الثاني": "11",
                "ديسمبر": "12", "كانون الأول": "12",
            }
            if " يوم " in date_text:
                after_yawm = date_text.split(" يوم ", 1)[1]
                date_match = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", after_yawm)
                if date_match:
                    day, month_name, year = date_match.groups()
                    date_part = f"{day} {month_name} {year}"

                if " في " in date_text:
                    before_yawm = date_text.split(" يوم ", 1)[0]
                    if " في " in before_yawm:
                        country_part = before_yawm.rsplit(" في ", 1)[1].strip()

            review_data["countryCode"] = country_part.strip()
            if date_part:
                date_match = re.search(r"(\d{1,2})\s+([\u0600-\u06FF]+)\s+(\d{4})", date_part.strip())
                if date_match:
                    day, month_name, year = date_match.groups()
                    month = ar_months.get(month_name, "")
                    if month:
                        date_obj = datetime.strptime(f"{day.zfill(2)}.{month}.{year}", "%d.%m.%Y")

        if date_obj:
            review_data["reviewDate"] = date_obj.strftime("%Y-%m-%d")
        else:
            raise ValueError("no supported date format matched")

    except Exception as exc:
        if log:
            log.error(f"Review date parse failed: country={task_country} text={date_text} error={exc}")
        review_data["reviewDate"] = ""
