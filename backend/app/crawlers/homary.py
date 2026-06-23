"""Homary 采集器 —— 爆米科技，Nuxt.js SSR 站点。

策略：
  1. 拉 sitemap 索引 → item 子 sitemap，得到全部商品 URL（即 SKU 总量）
  2. 商品页是 SSR 全渲染 HTML，从 <meta> + DOM 解析基础字段
     （__NUXT__ 是 (function(){...}) 形式无法当 JSON 解析，故走 HTML）
  3. best_sellers 子 sitemap 用于打热销标签

注：默认按 sitemap 全量抓取；HOMARY_LIMIT / HOMARY_MAX_ELAPSED_SEC 仅用于
   显式调试截断。
"""
from __future__ import annotations

import gzip
import os
import re
import time

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

_ID_RE = re.compile(r"-(\d+)\.html")
_PRICE_RE = re.compile(r"[\d,]+\.?\d*")
DEFAULT_LIMIT = int(os.environ.get("HOMARY_LIMIT", "999999"))
if DEFAULT_LIMIT <= 0 or DEFAULT_LIMIT == 2000:
    DEFAULT_LIMIT = 999999
DEFAULT_MAX_ELAPSED_SEC = int(os.environ.get("HOMARY_MAX_ELAPSED_SEC", "0"))

_CURRENCY = {"US": "USD", "UK": "GBP", "DE": "EUR", "ES": "EUR", "FR": "EUR"}


class HomaryCrawler(BaseCrawler):
    platform = "nuxt"

    def __init__(self, site, limit: int | None = None):
        super().__init__(site)
        self.limit = self._resolve_limit(DEFAULT_LIMIT, limit,
                                         honor_persisted=False)
        self.max_elapsed_sec = DEFAULT_MAX_ELAPSED_SEC
        self.cc = site.country.lower()

    def _headers(self) -> dict:
        """构造定制请求头（每请求透传给 CrawlerFetcher.get）。"""
        return {"User-Agent": self.ua()}

    def _sitemap_urls(self, fetcher, kind: str) -> list[str]:
        """取某类 sitemap 的全部 <loc>。kind: item / best_sellers。"""
        base = self.site.url.rstrip("/")
        url = f"{base}/sitemaps/google_sitemap_{kind}_{self.cc}.xml.gz"
        try:
            res = fetcher.get(url, headers=self._headers(), timeout=15)
            raw = res.content
            try:
                xml = gzip.decompress(raw).decode("utf-8", "ignore")
            except (OSError, gzip.BadGzipFile):
                xml = raw.decode("utf-8", "ignore")
            return re.findall(r"<loc>(.*?)</loc>", xml)
        except BlockedError:
            raise
        except Exception:
            return []

    def crawl(self) -> CrawlResult:
        result = CrawlResult()
        started = time.monotonic()
        fetcher = self.make_fetcher(
            kind="product",
            source="homary",
            fail_fast_blocked=True,
            retries=0,
        )

        item_urls = [u for u in self._sitemap_urls(fetcher, "item") if "/item/" in u]
        best_ids = {m.group(1) for u in self._sitemap_urls(fetcher, "best_sellers")
                    if (m := _ID_RE.search(u))}
        total = len(item_urls)
        targets = item_urls[: self.limit]
        result.total_product_count = total
        _persist_job_progress(self.job_id, products_count=0, total_product_count=total)
        result.notes.append(
            f"sitemap 共 {total} 商品，本次抓取 {len(targets)} 条"
            f"（HOMARY_LIMIT={self.limit}）；热销 {len(best_ids)} 款")
        max_elapsed_sec = getattr(self, "max_elapsed_sec", 0)
        if len(targets) < total:
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "sitemap"
            result.coverage_reason = (
                f"Homary sitemap 共 {total} 个商品，本次只计划抓取 {len(targets)} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "移除 HOMARY_LIMIT 后重跑。"

        for url in targets:
            if (max_elapsed_sec > 0
                    and self._elapsed(started) >= max_elapsed_sec):
                result.notes.append(
                    f"达到 Homary 总耗时上限 {max_elapsed_sec}s，"
                    f"提前停止，已解析 {len(result.products)}/{len(targets)}")
                result.coverage_complete = False
                result.coverage_code = "incomplete_detail_parse"
                result.coverage_stage = "fetch"
                result.coverage_reason = (
                    f"达到 Homary 总耗时上限 {max_elapsed_sec}s，"
                    f"本次只解析 {len(result.products)}/{len(targets)} 个商品"
                )
                result.coverage_retryable = True
                result.coverage_suggested_action = (
                    "放宽 HOMARY_MAX_ELAPSED_SEC 或拆分失败商品重抓。"
                )
                break
            try:
                row = self._parse_product(fetcher, url, best_ids)
                if row:
                    result.products.append(row)
                    if len(result.products) % 50 == 0:
                        _persist_job_progress(
                            self.job_id,
                            products_count=len(result.products),
                            total_product_count=total,
                        )
            except BlockedError:
                raise
            except Exception as exc:                # 单页失败不影响整体
                result.notes.append(f"跳过 {url}: {exc}")
            self.sleep()
        _persist_job_progress(
            self.job_id,
            products_count=len(result.products),
            total_product_count=total,
        )
        return result

    def crawl_failed_products(self, urls: list[str]) -> CrawlResult:
        """Retry only previously failed Homary PDP URLs."""
        result = CrawlResult()
        targets = [u for u in urls if u]
        result.total_product_count = len(targets)
        if not targets:
            result.notes.append("没有可重抓的 Homary 商品 URL")
            return result
        fetcher = self.make_fetcher(
            kind="product",
            source="homary_failed_product_retry",
            fail_fast_blocked=True,
            retries=1,
        )
        best_ids = {m.group(1) for u in self._sitemap_urls(fetcher, "best_sellers")
                    if (m := _ID_RE.search(u))}
        failed = 0
        for url in targets:
            try:
                row = self._parse_product(fetcher, url, best_ids)
                if row:
                    result.products.append(row)
                else:
                    failed += 1
                    result.notes.append(f"未解析到商品: {url}")
            except BlockedError:
                raise
            except Exception as exc:
                failed += 1
                result.notes.append(f"重抓失败 {url}: {exc}")
            self.sleep()
        result.notes.append(
            f"失败商品重抓 {len(result.products)}/{len(targets)}，失败 {failed}")
        return result

    def _parse_product(self, fetcher, url: str, best_ids: set) -> dict | None:
        m = _ID_RE.search(url)
        if not m:
            return None
        pid = m.group(1)
        res = fetcher.get(url, headers=self._headers(), timeout=15)
        html = res.text or ""
        self.snapshot(pid, html)                   # 原始商品页归档
        tree = HTMLParser(html)

        title = (
            self._clean_title(self._meta(tree, "og:title"))
            or self._clean_title(self._meta(tree, "twitter:title"))
            or self._clean_title(self._h1(tree))
            or self._clean_title(self._page_title(tree))
        )
        description = self._meta(tree, "og:description")
        image = self._meta(tree, "og:image")

        prices = tree.css(".price")
        sale = self._to_price(prices[0].text(strip=True)) if prices else None
        # 原价：找带删除线 / origin 的价格元素
        original = None
        for sel in (".origin-price", ".market-price", "del", ".product-price del"):
            node = tree.css_first(sel)
            if node:
                original = self._to_price(node.text(strip=True))
                if original:
                    break
        if not original:
            original = sale

        crumbs = [n.text(strip=True) for n in tree.css('[class*=breadcrumb] a')]
        crumbs = [c for c in crumbs if c and c.lower() != "home"]
        # 去重保序
        seen, path = set(), []
        for c in crumbs:
            if c not in seen:
                seen.add(c)
                path.append(c)
        category_path = "/".join(path[:3]) or None

        images = []
        for img in tree.css("img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and "su-cdn.com" in src and src not in images:
                images.append(src)
        if image and image not in images:
            images.insert(0, image)

        out_of_stock = bool(re.search(r"out of stock|sold out", html, re.I))

        return {
            "sku": pid,
            "spu": pid,
            "title": title,
            "description": description,
            "image_urls": images[:10] or ([image] if image else []),
            "category_path": category_path,
            "sale_price": sale,
            "original_price": original,
            "currency": _CURRENCY.get(self.site.country, "USD"),
            "status": "out_of_stock" if out_of_stock else "on_sale",
            "has_video": "<video" in html,
            "label": "BEST SELLER" if pid in best_ids else None,
            "product_url": url,
            "site": self.site.site,
            "brand": self.site.brand,
            "is_bestseller": pid in best_ids,
        }

    @staticmethod
    def _meta(tree: HTMLParser, prop: str) -> str | None:
        node = (tree.css_first(f'meta[property="{prop}"]')
                or tree.css_first(f'meta[name="{prop}"]'))
        return node.attributes.get("content") if node else None

    @staticmethod
    def _h1(tree: HTMLParser) -> str | None:
        node = tree.css_first("h1")
        return node.text(strip=True) if node else None

    @staticmethod
    def _page_title(tree: HTMLParser) -> str | None:
        node = tree.css_first("title")
        return node.text(strip=True) if node else None

    @staticmethod
    def _clean_title(value: str | None) -> str | None:
        if not value:
            return None
        parts = [p.strip() for p in re.split(r"[｜|]", value) if p.strip()]
        if not parts:
            return None
        title = parts[0]
        if re.fullmatch(r"homary(?:\s+[a-z]{2})?", title, re.I):
            return None
        return title

    @staticmethod
    def _to_price(text: str | None):
        """智能价格解析 —— 自适应欧式 / 美式数字格式。

        欧式: `94,99 €` (= €94.99) / `9.999,99 €` (= €9999.99)
        美式: `$94.99` / `$9,999.99`
        规则: 同时含 `,` 和 `.` → 取最右一个为小数点；
              仅含 `,` 且后跟 ≤ 2 位 → 视作小数点；否则千分位。
        """
        if not text:
            return None
        import re as _re
        m = _re.search(r"[\d.,]+", str(text))
        if not m:
            return None
        s = m.group()
        if "," in s and "." in s:
            # 最右的分隔符是小数点
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")    # 欧式 9.999,99
            else:
                s = s.replace(",", "")                       # 美式 9,999.99
        elif "," in s:
            tail = s.rsplit(",", 1)[-1]
            if len(tail) <= 2:
                s = s.replace(",", ".")                       # 欧式 94,99
            else:
                s = s.replace(",", "")                        # 美式 94,995
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _elapsed(started: float) -> float:
        return time.monotonic() - started


def _persist_job_progress(
    job_id: int | None,
    *,
    products_count: int | None = None,
    total_product_count: int | None = None,
) -> None:
    """Expose long Homary crawl progress without expensive live URL counts."""
    if not job_id:
        return
    try:
        from ..db import SessionLocal
        from ..models import CrawlJob
    except Exception:
        return
    db = SessionLocal()
    try:
        job = db.get(CrawlJob, job_id)
        if job is not None:
            if products_count is not None:
                job.products_count = max(0, int(products_count))
            if total_product_count is not None and total_product_count >= 0:
                job.total_product_count = int(total_product_count)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
