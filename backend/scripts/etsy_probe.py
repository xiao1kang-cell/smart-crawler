"""Etsy 反爬探针 —— 一次性脚本，验证"按店铺"路径是否能稳定过 Akamai。

不写入任何库，只打印信号。用法：
    .venv/bin/python scripts/etsy_probe.py            # 直连
    ETSY_PROBE_PROXY=http://user:pass@host:port .venv/bin/python scripts/etsy_probe.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from curl_cffi import requests as creq

BASE = "https://www.etsy.com"
PROXY = os.environ.get("ETSY_PROBE_PROXY", "").strip()

_LISTING_RE = re.compile(r"/listing/(\d{6,12})/[a-zA-Z0-9_-]+")
_SHOP_RE = re.compile(r"/shop/([A-Za-z0-9_-]{2,40})\b")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_BLOCK_MARKS = ("captcha-delivery", "access denied", "rate.limit",
                "robot or human", "px-captcha", "perimeterx")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def session() -> creq.Session:
    s = creq.Session(impersonate="chrome131")
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
        "Sec-Fetch-Mode": "navigate",
    })
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s


def classify(status: int, html: str) -> str:
    low = (html or "").lower()
    if any(m in low for m in _BLOCK_MARKS):
        return "BLOCKED(challenge)"
    if status in (403, 429):
        return f"BLOCKED({status})"
    if status != 200:
        return f"HTTP {status}"
    if len(html) < 20_000:
        return f"SUSPECT(short {len(html)}B)"
    return "OK"


def get(s, url, label):
    t0 = time.time()
    try:
        r = s.get(url, timeout=30)
    except Exception as exc:
        print(f"  [{label}] EXC {type(exc).__name__}: {str(exc)[:80]}")
        return None, ""
    html = r.text or ""
    verdict = classify(r.status_code, html)
    dt = time.time() - t0
    print(f"  [{label}] {verdict} status={r.status_code} "
          f"bytes={len(html)} {dt:.1f}s")
    return r, html


def parse_jsonld(html):
    for block in _LD_RE.findall(html):
        try:
            doc = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        nodes = (doc if isinstance(doc, list)
                 else doc.get("@graph", [doc]) if isinstance(doc, dict) else [])
        for node in nodes:
            if isinstance(node, dict):
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    off = node.get("offers") or {}
                    if isinstance(off, list):
                        off = off[0] if off else {}
                    return {
                        "name": (node.get("name") or "")[:60],
                        "price": off.get("price") or off.get("lowPrice"),
                        "currency": off.get("priceCurrency"),
                        "rating": (node.get("aggregateRating") or {}).get(
                            "ratingValue"),
                        "reviews": (node.get("aggregateRating") or {}).get(
                            "reviewCount"),
                    }
    return None


def main():
    mode = f"PROXY={PROXY.split('@')[-1]}" if PROXY else "DIRECT"
    print(f"=== Etsy probe · {mode} ===")
    s = session()

    # 1) 首页 sanity
    print("\n[1] 首页 sanity")
    get(s, BASE + "/", "home")
    time.sleep(2)

    # 2) 搜索页 → 发现真实 shop 名 + listing
    print("\n[2] 搜索页（发现 shop/listing 种子）")
    r, html = get(s, BASE + "/search?q=ceramic+mug&page=1", "search")
    shops, listings = [], []
    if html:
        shops = list(dict.fromkeys(_SHOP_RE.findall(html)))
        shops = [x for x in shops if x.lower() not in (
            "policy", "legal", "about", "help")][:5]
        listings = list(dict.fromkeys(_LISTING_RE.findall(html)))[:5]
        print(f"      发现 shop 候选: {shops}")
        print(f"      发现 listing: {len(listings)} 个 e.g. {listings[:3]}")
    time.sleep(2)

    # 3) 店铺页 + 翻页（按店铺路径的核心）
    print("\n[3] 店铺页 /shop/{name}（核心路径）")
    shop = shops[0] if shops else None
    if shop:
        r, html = get(s, f"{BASE}/shop/{shop}", f"shop:{shop} p1")
        shop_listings = list(dict.fromkeys(_LISTING_RE.findall(html or "")))
        print(f"      店铺页解析出 {len(shop_listings)} 个 listing")
        time.sleep(2)
        # 翻页：Etsy 店铺分页 ?page=2 或 #items-pagination；实测 ?page=2
        r, html = get(s, f"{BASE}/shop/{shop}?page=2", f"shop:{shop} p2")
        p2 = list(dict.fromkeys(_LISTING_RE.findall(html or "")))
        print(f"      第2页解析出 {len(p2)} 个 listing，与p1重叠="
              f"{len(set(p2) & set(shop_listings))}")
        if shop_listings:
            listings = shop_listings + listings
    else:
        print("      ⚠ 没拿到 shop 种子，跳过")
    time.sleep(2)

    # 4) 详情页 JSON-LD（字段抽取）
    print("\n[4] 详情页 JSON-LD 抽取")
    ok = 0
    for lid in listings[:4]:
        r, html = get(s, f"{BASE}/listing/{lid}", f"pdp:{lid}")
        if html:
            row = parse_jsonld(html)
            if row:
                ok += 1
                print(f"      ✓ {row}")
            else:
                print(f"      ✗ 无 Product JSON-LD")
        time.sleep(2)

    print(f"\n=== 小结：详情页成功抽取 {ok}/{min(len(listings),4)} ===")


if __name__ == "__main__":
    sys.exit(main())
