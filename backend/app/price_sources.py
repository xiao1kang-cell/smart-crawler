"""Config-driven product price enrichment.

This module is intentionally outside individual crawlers: sites that cannot
reliably expose price from storefront/PDP can be fixed by configuring a feed
or API source in the admin console, then letting the runner apply it before
products enter the normal pipeline.
"""
from __future__ import annotations

import csv
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .pipeline import clean_text, to_price

_SKU_ALIASES = (
    "sku", "SKU", "id", "ID", "product_id", "item_id", "offer_id",
    "mpn", "MPN", "gtin", "GTIN", "ean", "EAN",
)
_SALE_PRICE_ALIASES = (
    "sale_price", "price", "current_price", "final_price", "discount_price",
    "promotion_price", "promo_price", "offer_price",
)
_ORIGINAL_PRICE_ALIASES = (
    "original_price", "list_price", "was_price", "regular_price",
    "compare_at_price", "msrp", "rrp", "pre_price",
)
_CURRENCY_ALIASES = ("currency", "currency_code", "price_currency")
_TITLE_ALIASES = ("title", "name", "product_name")


def enrich_products_from_site_config(site, products: list[dict], *,
                                     counter=None) -> tuple[list[dict], dict]:
    """Apply configured price sources to crawler output.

    Feed enrichment is best-effort: failures are returned as stats so the job
    can surface diagnostics without turning a crawl into a hard failure.
    """
    config = site.crawler_config or {}
    source_type = str(config.get("price_source_type") or "").strip().lower()
    feed_url = _first_config(config, "price_feed_url", "feed_url", "price_feed")
    api_url = _first_config(config, "pdp_price_api_url", "price_api_url")
    pdp_price_selector = _first_config(
        config, "pdp_price_selector", "price_selector")
    stats = {
        "applied": False,
        "source_type": source_type or None,
        "source": feed_url or api_url or ("pdp" if pdp_price_selector else None),
        "rows": 0,
        "matched": 0,
        "updated": 0,
        "error": None,
    }
    if not products or not (feed_url or api_url or pdp_price_selector):
        return products, stats
    if source_type and source_type not in {"feed", "csv", "json", "api", "pdp", "html", "external"}:
        stats["error"] = f"unsupported price_source_type={source_type}"
        return products, stats

    try:
        if feed_url:
            rows = _load_rows(feed_url, site=site, config=config,
                              counter=counter)
        elif api_url:
            rows = _load_api_rows(api_url, products, config, site=site,
                                  counter=counter)
        else:
            rows = _load_pdp_rows(products, config, site=site,
                                  counter=counter)
        stats["rows"] = len(rows)
        matched, updated = _apply_rows(products, rows, config)
        stats.update({"applied": True, "matched": matched, "updated": updated})
        return products, stats
    except Exception as exc:
        stats["error"] = str(exc)
        return products, stats


def _first_config(config: dict, *keys: str):
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _load_rows(source: str, *, site=None, config: dict | None = None,
               counter=None) -> list[dict]:
    raw = _load_bytes(
        source, site=site, config=config or {}, counter=counter,
        default_use_proxy=False,
    )
    text = raw.decode("utf-8-sig")
    if source.lower().endswith(".json") or text.lstrip().startswith(("{", "[")):
        return _json_rows(json.loads(text))
    return _csv_rows(text)


def _load_bytes(source: str, *, site=None, config: dict | None = None,
                counter=None, default_use_proxy: bool = False) -> bytes:
    config = config or {}
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if site is not None and _truthy(config.get("price_source_use_proxy"), default=default_use_proxy):
            from .fetching import CrawlerFetcher, FetchContext

            fetcher = CrawlerFetcher(FetchContext(
                site=site,
                kind="product",
                source="price_source",
                timeout=_int_config(config, "price_source_timeout", 30),
                use_proxy=True,
                allow_stealth=_truthy(config.get("price_source_allow_stealth")),
                retries=_int_config(config, "price_source_retries", 1),
                counter=counter,
            ))
            result = fetcher.get(source)
            if not result.ok:
                detail = result.failure.detail if result.failure else "unknown"
                raise RuntimeError(
                    f"price source fetch failed status={result.status} {detail}")
            return result.content or result.text.encode("utf-8")
        req = Request(source, headers={"User-Agent": "smart-crawler/price-source"})
        with urlopen(req, timeout=_int_config(config, "price_source_timeout", 30)) as resp:
            return resp.read()
    if parsed.scheme == "file":
        return Path(parsed.path).read_bytes()
    return Path(source).read_bytes()


def _json_rows(value) -> list[dict]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("products", "items", "data", "rows", "offers"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [x for x in nested if isinstance(x, dict)]
    return []


def _load_api_rows(template: str, products: list[dict], config: dict, *,
                   site=None, counter=None) -> list[dict]:
    rows: list[dict] = []
    for item in _limited_products(products, config, default_limit=200):
        url = _format_template(template, item)
        if not url:
            continue
        data = json.loads(_load_bytes(
            url, site=site, config=config, counter=counter,
            default_use_proxy=False,
        ).decode("utf-8-sig"))
        nested = _json_rows(data)
        if nested:
            for row in nested:
                row.setdefault("sku", item.get("sku"))
                rows.append(row)
        elif isinstance(data, dict):
            data.setdefault("sku", item.get("sku"))
            rows.append(data)
    return rows


def _load_pdp_rows(products: list[dict], config: dict, *, site=None,
                   counter=None) -> list[dict]:
    price_selector = _first_config(config, "pdp_price_selector", "price_selector")
    title_selector = _first_config(config, "pdp_title_selector", "title_selector")
    if not price_selector:
        return []
    rows: list[dict] = []
    for item in _limited_products(products, config, default_limit=50):
        url = item.get("product_url")
        if not url:
            continue
        html = _load_bytes(
            str(url), site=site, config=config, counter=counter,
            default_use_proxy=True,
        ).decode("utf-8-sig", errors="ignore")
        row = {
            "sku": item.get("sku"),
            "sale_price": _selector_text(html, price_selector),
            "title": _selector_text(html, title_selector) if title_selector else None,
        }
        rows.append(row)
    return rows


def _csv_rows(text: str) -> list[dict]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return [dict(row) for row in reader]


def _index_rows(rows: list[dict], config: dict) -> dict[str, dict]:
    sku_keys = _field_candidates(config, "price_feed_sku_field", _SKU_ALIASES)
    out: dict[str, dict] = {}
    for row in rows:
        sku = _normalize_sku(_first_value(row, sku_keys))
        if sku and sku not in out:
            out[sku] = row
    return out


def _apply_rows(products: list[dict], rows: list[dict],
                config: dict) -> tuple[int, int]:
    by_sku = _index_rows(rows, config)
    overwrite = _truthy(config.get("price_source_overwrite"))
    updated = matched = 0
    for item in products:
        sku = _normalize_sku(item.get("sku"))
        if not sku:
            continue
        row = by_sku.get(sku)
        if row is None:
            continue
        matched += 1
        changed = _apply_row(item, row, config, overwrite=overwrite)
        if changed:
            updated += 1
    return matched, updated


def _field_candidates(config: dict, configured_key: str,
                      defaults: tuple[str, ...]) -> tuple[str, ...]:
    configured = config.get(configured_key)
    if configured:
        return (str(configured), *defaults)
    return defaults


def _apply_row(item: dict, row: dict, config: dict, *, overwrite: bool) -> bool:
    changed = False
    sale = to_price(_first_value(
        row, _field_candidates(config, "price_feed_sale_price_field",
                               _SALE_PRICE_ALIASES)))
    original = to_price(_first_value(
        row, _field_candidates(config, "price_feed_original_price_field",
                               _ORIGINAL_PRICE_ALIASES)))
    currency = clean_text(_first_value(
        row, _field_candidates(config, "price_feed_currency_field",
                               _CURRENCY_ALIASES)))
    title = clean_text(_first_value(
        row, _field_candidates(config, "price_feed_title_field",
                               _TITLE_ALIASES)))
    for key, value in (
        ("sale_price", sale),
        ("original_price", original),
        ("currency", currency),
        ("title", title),
    ):
        if value is None:
            continue
        if overwrite or item.get(key) in (None, "", [], {}):
            if item.get(key) != value:
                item[key] = value
                changed = True
    return changed


def _first_value(row: dict, keys: tuple[str, ...]):
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        value = lower.get(str(key).strip().lower())
        if value not in (None, ""):
            return value
    return None


def _normalize_sku(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower()


def _truthy(value, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _selector_text(html: str, selector: str | None) -> str | None:
    selector = (selector or "").strip()
    if not selector:
        return None
    try:
        from bs4 import BeautifulSoup

        node = BeautifulSoup(html, "html.parser").select_one(selector)
        return node.get_text(" ", strip=True) if node else None
    except Exception:
        pass
    for candidate in [s.strip() for s in selector.split(",") if s.strip()]:
        parser = _SimpleSelectorTextParser(candidate)
        parser.feed(html)
        text = parser.text()
        if text:
            return text
    return None


class _SimpleSelectorTextParser(HTMLParser):
    """Tiny fallback for common selectors when bs4 is unavailable."""

    def __init__(self, selector: str):
        super().__init__(convert_charrefs=True)
        self.selector = selector
        self.capture_depth = 0
        self.parts: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if self.done:
            return
        if self.capture_depth:
            self.capture_depth += 1
            return
        if self._matches(tag, dict(attrs)):
            self.capture_depth = 1

    def handle_endtag(self, tag: str):
        if self.capture_depth:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.done = True

    def handle_data(self, data: str):
        if self.capture_depth and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str | None:
        text = " ".join(self.parts).strip()
        return re.sub(r"\s+", " ", text) if text else None

    def _matches(self, tag: str, attrs: dict[str, str | None]) -> bool:
        selector = self.selector
        if selector.startswith("."):
            wanted = selector[1:]
            classes = str(attrs.get("class") or "").split()
            return wanted in classes
        if selector.startswith("#"):
            return attrs.get("id") == selector[1:]
        attr = re.fullmatch(r"\[([A-Za-z0-9_:-]+)(?:=['\"]?([^'\"]+)['\"]?)?\]", selector)
        if attr:
            key, value = attr.groups()
            return key in attrs if value is None else attrs.get(key) == value
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", selector):
            return tag.lower() == selector.lower()
        return False


def _limited_products(products: list[dict], config: dict, *,
                      default_limit: int) -> list[dict]:
    limit = _int_config(config, "price_source_max_items", default_limit)
    return products[:max(0, limit)]


def _int_config(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key) or default)
    except (TypeError, ValueError):
        return default


def _format_template(template: str, item: dict) -> str | None:
    sku = item.get("sku")
    product_url = item.get("product_url")
    values = {
        "sku": sku or "",
        "product_url": product_url or "",
        "sku_urlencoded": quote(str(sku or ""), safe=""),
        "product_url_urlencoded": quote(str(product_url or ""), safe=""),
    }
    try:
        return template.format(**values)
    except KeyError:
        return None
