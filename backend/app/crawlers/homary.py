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
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from selectolax.parser import HTMLParser

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

_ID_RE = re.compile(r"-(\d+)\.html")
_PRICE_RE = re.compile(r"[\d,]+\.?\d*")
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
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
        sitemap_fetcher = self.make_fetcher(
            kind="sitemap",
            source="homary_sitemap",
            fail_fast_blocked=True,
            retries=0,
        )

        item_urls = [
            u for u in self._sitemap_urls(sitemap_fetcher, "item")
            if "/item/" in u and _ID_RE.search(u)
        ]
        best_ids = {m.group(1) for u in self._sitemap_urls(sitemap_fetcher, "best_sellers")
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

        concurrency = self._detail_concurrency()
        failed = 0
        if concurrency <= 1:
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
                    row = self._parse_product_with_new_fetcher(
                        url,
                        best_ids,
                        "homary",
                        0,
                    )
                    if row:
                        result.products.append(row)
                    else:
                        failed += 1
                        result.notes.append(f"未解析到商品: {url}")
                except BlockedError:
                    raise
                except Exception as exc:            # 单页失败不影响整体
                    failed += 1
                    result.notes.append(f"跳过 {url}: {exc}")
                if len(result.products) % 50 == 0:
                    _persist_job_progress(
                        self.job_id,
                        products_count=len(result.products),
                        total_product_count=total,
                    )
                self.sleep()
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        self._parse_product_with_new_fetcher,
                        url,
                        best_ids,
                        "homary",
                        0,
                    ): url
                    for url in targets
                }
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        row = future.result()
                        if row:
                            result.products.append(row)
                        else:
                            failed += 1
                            result.notes.append(f"未解析到商品: {url}")
                    except BlockedError:
                        raise
                    except Exception as exc:        # 单页失败不影响整体
                        failed += 1
                        result.notes.append(f"跳过 {url}: {exc}")
                    if len(result.products) % 50 == 0:
                        _persist_job_progress(
                            self.job_id,
                            products_count=len(result.products),
                            total_product_count=total,
                        )
        if len(result.products) < len(targets):
            result.coverage_complete = False
            result.coverage_code = "incomplete_detail_parse"
            result.coverage_stage = "fetch"
            result.coverage_reason = (
                f"Homary 本次计划抓取 {len(targets)} 个商品，"
                f"实际解析 {len(result.products)} 个"
            )
            result.coverage_retryable = True
            result.coverage_suggested_action = "重试未解析/失败商品，或全量重跑该站点。"
        result.notes.append(
            f"Homary PDP 抓取并发 {concurrency}，成功 {len(result.products)}/"
            f"{len(targets)}，失败 {failed}")
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
        sitemap_fetcher = self.make_fetcher(
            kind="sitemap",
            source="homary_sitemap",
            fail_fast_blocked=True,
            retries=1,
        )
        best_ids = {m.group(1) for u in self._sitemap_urls(sitemap_fetcher, "best_sellers")
                    if (m := _ID_RE.search(u))}
        failed = 0
        concurrency = self._failed_product_retry_concurrency()
        if concurrency <= 1:
            for url in targets:
                try:
                    row = self._parse_product_with_new_fetcher(
                        url,
                        best_ids,
                        "homary_failed_product_retry",
                        1,
                    )
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
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        self._parse_product_with_new_fetcher,
                        url,
                        best_ids,
                        "homary_failed_product_retry",
                        1,
                    ): url
                    for url in targets
                }
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        row = future.result()
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
        result.notes.append(
            f"失败商品重抓 {len(result.products)}/{len(targets)}，"
            f"并发 {concurrency}，失败 {failed}")
        return result

    def _parse_product_with_new_fetcher(
        self,
        url: str,
        best_ids: set,
        source: str,
        retries: int,
    ) -> dict | None:
        fetcher = self.make_fetcher(
            kind="product",
            source=source,
            fail_fast_blocked=True,
            retries=retries,
            proxy_lease_ttl_sec=self._proxy_lease_ttl_sec(default=0),
            rate_interval_sec=self._rate_interval_sec(),
        )
        return self._parse_product(fetcher, url, best_ids)

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
        if not category_path:
            category_path = self._jsonld_breadcrumb(tree) or self._jsonld_category(tree)
        if not sale and not category_path:
            return None

        images = []
        for img in tree.css("img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and "su-cdn.com" in src and src not in images:
                images.append(src)
        if image and image not in images:
            images.insert(0, image)

        out_of_stock = bool(re.search(r"out of stock|sold out", html, re.I))
        promo_labels = self._promotion_labels(tree)
        has_free_shipping = bool(re.search(
            r"free\s+(?:shipping|delivery)|livraison\s+gratuite|"
            r"spedizione\s+gratuita|env[ií]o\s+gratis|gratis\s+verzending|"
            r"kostenloser\s+versand|versandkostenfrei|包邮|免运费",
            html,
            re.I,
        ))

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
            "ratings": self._jsonld_rating_value(tree),
            "review_count": self._review_count(tree, html),
            "status": "out_of_stock" if out_of_stock else "on_sale",
            "has_video": "<video" in html,
            "has_free_shipping": has_free_shipping,
            "label": "BEST SELLER" if pid in best_ids else None,
            "attributes": {
                "promotions": promo_labels,
                "free_shipping_label": "Free shipping" if has_free_shipping else None,
            },
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
    def _promotion_labels(tree: HTMLParser) -> list[str]:
        selectors = (
            "[class*=coupon]", "[class*=promo]", "[class*=promotion]",
            "[class*=discount]", "[class*=sale]", "[class*=deal]",
            "[class*=campaign]", "[class*=offer]", "[class*=shipping]",
            "[class*=delivery]", ".tag", ".badge",
        )
        labels: list[str] = []
        seen: set[str] = set()
        promo_re = re.compile(
            r"sale|deal|discount|coupon|promo|save|off|bundle|"
            r"free\s+(?:shipping|delivery)|code|"
            r"rabatt|gutschein|remise|soldes|sconto|descuento|korting|"
            r"包邮|免运费|优惠|折扣|券",
            re.I,
        )
        for selector in selectors:
            for node in tree.css(selector):
                text = re.sub(r"\s+", " ", node.text(separator=" ", strip=True))
                if not text or len(text) > 180 or not promo_re.search(text):
                    continue
                if text not in seen:
                    seen.add(text)
                    labels.append(text)
                if len(labels) >= 6:
                    return labels
        return labels

    @classmethod
    def _jsonld_blocks(cls, tree: HTMLParser) -> list[dict]:
        blocks: list[dict] = []
        for node in tree.css('script[type="application/ld+json"]'):
            raw = node.text(strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            stack = parsed if isinstance(parsed, list) else [parsed]
            while stack:
                item = stack.pop(0)
                if not isinstance(item, dict):
                    continue
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                blocks.append(item)
        return blocks

    @classmethod
    def _jsonld_breadcrumb(cls, tree: HTMLParser) -> str | None:
        for node in cls._jsonld_blocks(tree):
            raw_type = node.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(str(t).lower() == "breadcrumblist" for t in types if t):
                continue
            names: list[str] = []
            for elem in node.get("itemListElement") or []:
                if not isinstance(elem, dict):
                    continue
                item = elem.get("item")
                name = item.get("name") if isinstance(item, dict) else None
                name = name or elem.get("name")
                if not name:
                    continue
                text = str(name).strip()
                if not text or text.lower() in {"home", "homary"}:
                    continue
                names.append(text)
            if names:
                return "/".join(names[:3])
        return None

    @classmethod
    def _jsonld_category(cls, tree: HTMLParser) -> str | None:
        for node in cls._jsonld_blocks(tree):
            raw_type = node.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(str(t).lower() == "product" for t in types if t):
                continue
            value = node.get("category")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                name = value.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None

    @classmethod
    def _jsonld_rating_value(cls, tree: HTMLParser):
        for node in cls._jsonld_blocks(tree):
            rating = node.get("aggregateRating") if isinstance(node, dict) else None
            if isinstance(rating, dict):
                try:
                    return float(rating.get("ratingValue"))
                except (TypeError, ValueError):
                    return None
        return None

    @classmethod
    def _jsonld_review_count(cls, tree: HTMLParser) -> int | None:
        for node in cls._jsonld_blocks(tree):
            rating = node.get("aggregateRating") if isinstance(node, dict) else None
            if isinstance(rating, dict):
                count = rating.get("reviewCount") or rating.get("ratingCount")
                try:
                    return int(float(count))
                except (TypeError, ValueError):
                    return None
        return None

    @classmethod
    def _review_count(cls, tree: HTMLParser, html: str) -> int | None:
        jsonld = cls._jsonld_review_count(tree)
        if jsonld is not None:
            return jsonld
        for selector in (
            "[class*=review-count]", "[class*=reviews-count]",
            "[class*=reviewCount]", "[data-review-count]",
        ):
            for node in tree.css(selector):
                text = node.attributes.get("data-review-count") or node.text(separator=" ", strip=True)
                count = cls._count_from_text(text)
                if count is not None:
                    return count
        for pattern in (
            r"\breviewCount\b\s*[:=]\s*([A-Za-z_$][\w$]*|\d+)",
            r"\bratingCount\b\s*[:=]\s*([A-Za-z_$][\w$]*|\d+)",
        ):
            match = re.search(pattern, html)
            if not match:
                continue
            token = match.group(1)
            if token.isdigit():
                return int(token)
            var_match = re.search(
                rf"\b{re.escape(token)}\b\s*=\s*['\"]?(\d+)['\"]?",
                html,
            )
            if var_match:
                return int(var_match.group(1))
        return cls._count_from_text(html) or 0

    @staticmethod
    def _count_from_text(text: str | None) -> int | None:
        if not text:
            return None
        match = re.search(r"(\d[\d,\s]*)\s*(?:reviews?|ratings?|avis|bewertungen)", text, re.I)
        if not match:
            return None
        try:
            return int(match.group(1).replace(",", "").replace(" ", ""))
        except ValueError:
            return None

    @staticmethod
    def _elapsed(started: float) -> float:
        return time.monotonic() - started

    def _detail_concurrency(self) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        if self._proxy_lease_ttl_sec(default=0) <= 0:
            return 1
        raw = (
            config.get("detail_concurrency")
            or config.get("homary_concurrency")
            or os.environ.get("HOMARY_CONCURRENCY")
        )
        if raw in (None, ""):
            return 8
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 8
        return max(1, min(value, 20))

    def _failed_product_retry_concurrency(self) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        if self._proxy_lease_ttl_sec(default=0) <= 0:
            return 1
        raw = (
            config.get("failed_product_retry_concurrency")
            or config.get("detail_concurrency")
            or os.environ.get("HOMARY_FAILED_PRODUCT_RETRY_CONCURRENCY")
        )
        if raw in (None, ""):
            return 6
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 6
        return max(1, min(value, 12))

    def _proxy_lease_ttl_sec(self, *, default: int = 300) -> int:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = config.get("proxy_lease_ttl_sec") or os.environ.get("HOMARY_PROXY_LEASE_TTL_SEC")
        if raw in (None, "") and default <= 0:
            return 0
        try:
            return max(30, min(int(raw or default), 1800))
        except (TypeError, ValueError):
            return default

    def _rate_interval_sec(self) -> float | None:
        config = self.site.crawler_config or {}
        config = config if isinstance(config, dict) else {}
        raw = config.get("rate_interval_sec") or os.environ.get("HOMARY_RATE_INTERVAL_SEC")
        if raw in (None, "") and self._proxy_lease_ttl_sec(default=0) <= 0:
            return None
        try:
            value = float(raw if raw not in (None, "") else 0.15)
        except (TypeError, ValueError):
            value = 0.15
        return max(0.03, min(value, 2.0))


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
