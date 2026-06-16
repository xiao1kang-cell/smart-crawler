"""Etsy 真浏览器探针 —— StealthyFetcher(camoufox)能否过 DataDome。

复用现有反爬栈(_stealth_config.stealth_kwargs)。本地无住宅代理时直连跑;
NAS 上可 export ETSY_PROBE_PROXY 走住宅。

DataDome ≠ Cloudflare → solve_cloudflare=False(见 nas-deploy skill 提示)。

用法:
    .venv/bin/python scripts/etsy_probe_browser.py
    ETSY_PROBE_PROXY=http://u:p@host:port .venv/bin/python scripts/etsy_probe_browser.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.crawlers._stealth_config import stealth_kwargs  # noqa: E402

BASE = "https://www.etsy.com"
PROXY = os.environ.get("ETSY_PROBE_PROXY", "").strip() or None

_LISTING_RE = re.compile(r"/listing/(\d{6,12})")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_BLOCK_MARKS = ("captcha-delivery", "datadome", "access denied",
                "robot or human", "px-captcha", "verifying you are human")


def render(url: str) -> tuple[str, int]:
    """真浏览器渲染,返回 (html, 页面字节数)。"""
    from scrapling.fetchers import StealthyFetcher
    kw = stealth_kwargs(proxy=PROXY, country="US", solve_cloudflare=False,
                        network_idle=True, timeout_ms=60000)
    page = StealthyFetcher.fetch(url, **kw)
    html = page.html_content or getattr(page, "body", "") or ""
    return html, len(html)


def verdict(html: str, n: int) -> str:
    low = html.lower()
    # DataDome 挑战页通常短、含 captcha-delivery iframe
    if any(m in low for m in _BLOCK_MARKS) and n < 80_000:
        return "BLOCKED(datadome challenge)"
    if n < 15_000:
        return f"SUSPECT(short {n}B)"
    return "OK"


def parse_jsonld(html: str):
    for block in _LD_RE.findall(html):
        try:
            doc = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        nodes = (doc if isinstance(doc, list)
                 else doc.get("@graph", [doc]) if isinstance(doc, dict) else [])
        for n in nodes:
            if isinstance(n, dict) and (
                    n.get("@type") == "Product"
                    or (isinstance(n.get("@type"), list)
                        and "Product" in n.get("@type"))):
                o = n.get("offers") or {}
                if isinstance(o, list):
                    o = o[0] if o else {}
                agg = n.get("aggregateRating") or {}
                return {
                    "name": (n.get("name") or "")[:50],
                    "price": o.get("price") or o.get("lowPrice"),
                    "cur": o.get("priceCurrency"),
                    "rating": agg.get("ratingValue"),
                    "reviews": agg.get("reviewCount"),
                }
    return None


def main():
    mode = f"PROXY={PROXY.split('@')[-1]}" if PROXY else "DIRECT"
    print(f"=== Etsy BROWSER probe (StealthyFetcher/camoufox) · {mode} ===\n")

    # [1] 首页
    print("[1] 首页")
    h, n = render(BASE + "/")
    print(f"    {verdict(h, n)} bytes={n}")
    listings = list(dict.fromkeys(_LISTING_RE.findall(h)))[:4]
    print(f"    捞到 listing 种子: {listings}")

    # [2] 详情页(curl_cffi 在此全 403,看浏览器能否过)
    print("\n[2] 详情页 PDP")
    ok = 0
    shop = None
    for lid in listings:
        h, n = render(f"{BASE}/listing/{lid}")
        v = verdict(h, n)
        row = parse_jsonld(h) if v == "OK" else None
        if row:
            ok += 1
        if shop is None:
            m = re.search(r'"shop_name":"([A-Za-z0-9_-]{2,40})"', h)
            if m:
                shop = m.group(1)
        print(f"    {lid}: {v} bytes={n} row={row}")

    # [3] 店铺页 + 翻页(按店铺核心路径)
    print("\n[3] 店铺页 /shop/{name}")
    if shop:
        for pg in (1, 2):
            h, n = render(f"{BASE}/shop/{shop}?page={pg}")
            lids = list(dict.fromkeys(_LISTING_RE.findall(h)))
            print(f"    {shop} p{pg}: {verdict(h, n)} bytes={n} "
                  f"listings={len(lids)}")
    else:
        print("    ⚠ 未从详情页拿到 shop 名,跳过")

    print(f"\n=== 小结: PDP 成功抽取 {ok}/{len(listings)} ===")


if __name__ == "__main__":
    main()
