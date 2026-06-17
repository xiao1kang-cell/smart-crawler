"""Currency helpers shared by API, exports, and data-quality checks."""
from __future__ import annotations


SITE_CURRENCY_BY_COUNTRY = {
    "US": "USD", "CA": "CAD", "UK": "GBP", "GB": "GBP",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "IE": "EUR", "PT": "EUR", "PL": "PLN", "RO": "RON",
    "SE": "SEK", "CH": "CHF", "JP": "JPY", "KR": "KRW",
    "CN": "CNY", "ID": "IDR", "MX": "MXN", "BR": "BRL",
    "AU": "AUD", "NZ": "NZD", "AR": "ARS", "CL": "CLP",
    "CO": "COP", "MY": "MYR", "SG": "SGD", "VN": "VND",
    "TH": "THB", "PH": "PHP", "HK": "HKD", "TW": "TWD",
}

SYMBOL_CURRENCY = {
    "$": "USD",
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "£": "GBP",
    "€": "EUR",
    "zł": "PLN",
    "lei": "RON",
    "kr": "SEK",
    "CHF": "CHF",
    "¥": "JPY",
    "￥": "JPY",
    "₩": "KRW",
    "R$": "BRL",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
}


def currency_for_site(site: str | None) -> str | None:
    if not site or "_" not in site:
        return None
    suffix = site.rsplit("_", 1)[-1].upper().rstrip("0123456789")
    if suffix == "GLOBAL":
        return "USD"
    return SITE_CURRENCY_BY_COUNTRY.get(suffix)


def normalize_currency_for_site(currency: str | None, site: str | None) -> str | None:
    """Return a display currency that matches the site's market when possible."""
    expected = currency_for_site(site)
    raw = str(currency or "").strip()
    if not raw:
        return expected
    mapped = SYMBOL_CURRENCY.get(raw) or SYMBOL_CURRENCY.get(raw.upper())
    if expected and (mapped or len(raw) != 3):
        return expected
    if expected and raw.upper() != expected:
        return expected
    return mapped or raw.upper()
