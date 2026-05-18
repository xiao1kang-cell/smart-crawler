"""Google Shopping 采集器 —— 模块四（规格 §4.4）。

Google Shopping 的 udm=28 统一购物结果页，用 patchright（stealth 修补版
Playwright）渲染。实测我方 IP 直连可达（200，Google 不封消费级 IP）。

⚠ 已知局限：Google Shopping 商品卡的 CSS 类名是动态混淆的（bCOlv/IZE3Td…），
盲解析不稳。生产环境建议把 _extract 换成 SERP API（SerpApi 商用 或自托管
Novexity）—— 见 research/github-crawler-survey.md 第 6 节。当前 _extract 为
尽力解析版，模块四的关键词管理 / 入库 / 竞品占有率分析 / API 均已就绪。
"""
from __future__ import annotations

import re
import urllib.parse

from selectolax.parser import HTMLParser

from ..proxy import get_proxy

_PRICE_RE = re.compile(r"[\$£€]\s?([\d,]+(?:\.\d{1,2})?)")
_NUM_RE = re.compile(r"[\d,]+")


class GoogleShoppingCrawler:
    platform = "google_shopping"

    def __init__(self, keyword: str, max_results: int = 60):
        self.keyword = keyword
        self.max_results = max_results
        self.proxy = get_proxy("residential")        # 有就用，没有直连
        self.notes: list[str] = []

    def crawl(self) -> list[dict]:
        try:
            from patchright.sync_api import sync_playwright
        except Exception as exc:
            self.notes.append(f"patchright 未安装: {exc}")
            return []

        url = ("https://www.google.com/search?udm=28&q="
               + urllib.parse.quote(self.keyword))
        try:
            with sync_playwright() as p:
                launch_kw = {"headless": True}
                if self.proxy:
                    launch_kw["proxy"] = {"server": self.proxy}
                browser = p.chromium.launch(**launch_kw)
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    page.mouse.wheel(0, 6000)
                    page.wait_for_timeout(1500)
                    html = page.content()
                finally:
                    browser.close()
        except Exception as exc:
            self.notes.append(f"抓取异常: {exc}")
            return []

        results = self._extract(html)
        self.notes.append(f"关键词「{self.keyword}」采集 {len(results)} 个商品")
        return results

    def _extract(self, html: str) -> list[dict]:
        tree = HTMLParser(html)
        cards = []
        for sel in ('div[role="listitem"]', ".njFjte", ".MtXiu"):
            found = tree.css(sel)
            if found:
                cards = found
                break

        results, pos = [], 0
        for c in cards:
            text = c.text(separator="\n") or ""
            pm = _PRICE_RE.search(text)
            if not pm:                              # 无价格 → 非商品卡
                continue
            pos += 1
            if pos > self.max_results:
                break
            price = float(pm.group(1).replace(",", ""))
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            title = max((ln for ln in lines if not _PRICE_RE.match(ln)),
                        key=len, default=None)
            link = self._first_attr(c, "a", "href")
            img = self._first_attr(c, "img", "src")
            merchant = self._merchant(lines)
            rating, reviews = self._rating(text)
            results.append({
                "keyword": self.keyword, "position": pos,
                "product_title": title, "product_image": img,
                "price": price, "currency": _currency(pm.group(0)),
                "merchant": merchant, "merchant_url": None,
                "product_url": link,
                "rating": rating, "review_count": reviews,
                "shipping_info": "Free" if "free" in text.lower()
                or "免费" in text else None,
                "promotion_label": "SALE" if re.search(
                    r"sale|deal|% off|促销", text, re.I) else None,
            })
        return results

    @staticmethod
    def _first_attr(card, tag: str, attr: str):
        el = card.css_first(tag)
        return (el.attributes.get(attr) if el is not None else None)

    @staticmethod
    def _merchant(lines: list[str]) -> str | None:
        for ln in lines:
            if 2 < len(ln) < 40 and not _PRICE_RE.search(ln) \
                    and not re.search(r"rating|review|\d{3,}", ln, re.I):
                if any(k in ln for k in (".com", "·")) or ln.istitle():
                    return ln.split("·")[0].strip()
        return None

    @staticmethod
    def _rating(text: str):
        m = re.search(r"([0-5]\.\d)\s*[\(（]?\s*([\d,]+)", text)
        if m:
            try:
                return float(m.group(1)), int(m.group(2).replace(",", ""))
            except ValueError:
                pass
        return None, None


def _currency(sym: str) -> str:
    return {"$": "USD", "£": "GBP", "€": "EUR"}.get(sym.strip()[0], "USD")
