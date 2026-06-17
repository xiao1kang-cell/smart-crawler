"""Vidaxl 采集器 —— 荷贝，三路径合一。

Vidaxl 站点跑在 Salesforce Commerce Cloud，反爬重，`.com` 美国站封我方网段。
本采集器按优先级自动选路：

  路径1（首选）官方 Dropshipping API：设置环境变量
      VIDAXL_API_EMAIL / VIDAXL_API_TOKEN  → 走 b2b.vidaxl.com/api_customer/products
      （合法、完整、稳定，无需对抗反爬）
  路径2 官方/供应商产品 Feed：设置 VIDAXL_US_FEED_URL / VIDAXL_FEED_URL，
      支持 CSV / JSON / XML / .gz / .zip，适合 storefront 不可访问的市场
  路径3 欧洲国家站爬取：无 API/Feed 时，解析 sitemap_index → 商品页 JSON-LD
  路径4 美国站住宅代理：vidaxl_us 站点 proxy_tier=residential，配 proxies.txt 后
      自动经住宅代理走路径2 的逻辑

详见 docs/风控策略评估.md 与 Vidaxl 研究结论。
"""
from __future__ import annotations

import gzip
import csv
import io
import json
import os
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree as ET

from curl_cffi import requests as creq

from ..antiban import BlockedError
from .base import BaseCrawler, CrawlResult

API_BASE = "https://b2b.vidaxl.com/api_customer/products"
STOREFRONT_LIMIT = int(os.environ.get("VIDAXL_LIMIT", "999999"))
DEFAULT_STOREFRONT_LIMIT = int(os.environ.get("VIDAXL_DEFAULT_LIMIT", "1000"))
RUN_TARGET_LIMIT = int(os.environ.get("VIDAXL_RUN_TARGET_LIMIT", "200"))
API_PAGE = 500
_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "IE": "EUR", "DE": "EUR",
             "IT": "EUR", "ES": "EUR", "FR": "EUR", "RO": "RON", "PT": "EUR",
             "NL": "EUR", "PL": "PLN"}


class VidaxlCrawler(BaseCrawler):
    platform = "vidaxl"

    def __init__(self, site):
        super().__init__(site)
        self.base = site.url.rstrip("/")
        self.currency = _CURRENCY.get(site.country, "EUR")
        self._crawler_config = site.crawler_config or {}
        self.api_email = os.environ.get("VIDAXL_API_EMAIL") or self._config_value(
            "api_email", "vidaxl_api_email")
        self.api_token = os.environ.get("VIDAXL_API_TOKEN") or self._config_value(
            "api_token", "vidaxl_api_token")
        self.feed_url = self._resolve_feed_url()
        self.limit = self._resolve_limit(STOREFRONT_LIMIT)
        if self.limit <= 0:
            self.limit = DEFAULT_STOREFRONT_LIMIT

    def crawl(self) -> CrawlResult:
        if self.api_email and self.api_token:
            return self._crawl_api()
        if self.feed_url:
            return self._crawl_feed()
        return self._crawl_storefront()

    def _config_value(self, *keys: str) -> str | None:
        for key in keys:
            value = self._crawler_config.get(key)
            if value:
                return str(value).strip()
        return None

    def _resolve_feed_url(self) -> str | None:
        country = (self.site.country or "").upper()
        site_key = re.sub(r"\W+", "_", (self.site.site or "").upper())
        keys = [
            f"{site_key}_FEED_URL" if site_key else "",
            f"VIDAXL_{country}_FEED_URL" if country else "",
            f"VIDAXL_{site_key}_FEED_URL" if site_key else "",
            "VIDAXL_FEED_URL",
        ]
        for key in keys:
            if key and os.environ.get(key):
                return os.environ[key].strip()
        value = self._config_value("feed_url", "vidaxl_feed_url")
        if value:
            return value
        return None

    # ---------- 路径1：官方 Dropshipping API ----------
    def _crawl_api(self) -> CrawlResult:
        result = CrawlResult()
        fetcher = self.make_fetcher(kind="api", source="vidaxl", timeout=60,
                                    use_proxy=False)
        offset, total = 0, 0
        while True:
            try:
                res = fetcher.get(API_BASE, params={"limit": API_PAGE,
                                  "offset": offset},
                                  auth=(self.api_email, self.api_token))
                if not res.ok:
                    result.notes.append(
                        f"API 调用失败 offset={offset}: HTTP Error {res.status or 0}")
                    break
                self.snapshot(f"api_offset{offset}", res.text)
                items = res.json()
            except Exception as exc:
                result.notes.append(f"API 调用失败 offset={offset}: {exc}")
                break
            if isinstance(items, dict):
                items = items.get("data") or items.get("products") or []
            if not items:
                break
            for it in items:
                row = self._map_api(it)
                if row:
                    result.products.append(row)
            total += len(items)
            offset += API_PAGE
            if len(items) < API_PAGE:
                break
            self.sleep()
        result.notes.append(f"路径1 官方 API：拉取 {total} 个商品")
        return result

    # ---------- 路径2：官方/供应商 Feed ----------
    def _crawl_feed(self) -> CrawlResult:
        result = CrawlResult()
        try:
            raw = self._load_feed_bytes(self.feed_url or "")
            items = self._parse_feed_items(raw, self.feed_url or "")
        except Exception as exc:
            result.notes.append(f"Feed 读取失败: {exc}")
            return result

        total = 0
        seen: set[str] = set()
        for it in items:
            if len(result.products) >= self.limit:
                break
            row = self._map_feed(it)
            if not row:
                continue
            sku = row["sku"]
            if sku in seen:
                continue
            seen.add(sku)
            result.products.append(row)
            total += 1
        result.notes.append(
            f"路径2 官方 Feed：读取 {len(items)} 行，产出 {total} 个去重商品")
        return result

    def _load_feed_bytes(self, feed_url: str) -> bytes:
        if not feed_url:
            raise ValueError("feed_url required")
        path = feed_url[7:] if feed_url.startswith("file://") else feed_url
        if not re.match(r"^https?://", feed_url) and Path(path).exists():
            data = Path(path).read_bytes()
        else:
            fetcher = self.make_fetcher(kind="api", source="vidaxl_feed",
                                        timeout=90, use_proxy=False)
            res = fetcher.get(feed_url)
            if not res.ok:
                raise RuntimeError(f"Feed HTTP {res.status or 0}")
            data = res.content or (res.text or "").encode("utf-8")
        if feed_url.endswith(".gz") or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        if feed_url.endswith(".zip") or data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                if not names:
                    raise ValueError("Feed zip is empty")
                preferred = next(
                    (n for n in names if n.lower().endswith(
                        (".csv", ".json", ".xml", ".txt"))),
                    names[0],
                )
                data = zf.read(preferred)
        return data

    def _parse_feed_items(self, data: bytes, feed_url: str) -> list[dict]:
        text = data.decode("utf-8-sig", "replace")
        stripped = text.lstrip()
        low_url = feed_url.lower()
        if low_url.endswith(".json") or stripped.startswith(("{", "[")):
            doc = json.loads(text)
            if isinstance(doc, list):
                return [x for x in doc if isinstance(x, dict)]
            if isinstance(doc, dict):
                items = doc.get("data") or doc.get("products") or doc.get("items") or []
                return [x for x in items if isinstance(x, dict)]
            return []
        if low_url.endswith(".xml") or stripped.startswith("<"):
            return self._parse_feed_xml(text)
        return self._parse_feed_csv(text)

    @staticmethod
    def _parse_feed_csv(text: str) -> list[dict]:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        return [dict(row) for row in reader if row]

    @staticmethod
    def _parse_feed_xml(text: str) -> list[dict]:
        root = ET.fromstring(text)

        def tag_name(tag: str) -> str:
            return tag.rsplit("}", 1)[-1].lower()

        candidates = [
            node for node in root.iter()
            if tag_name(node.tag) in {"product", "item", "entry"}
        ]
        if not candidates and tag_name(root.tag) in {"product", "item", "entry"}:
            candidates = [root]
        rows: list[dict] = []
        for node in candidates:
            row: dict[str, str] = {}
            for child in node.iter():
                if child is node:
                    continue
                key = tag_name(child.tag)
                value = (child.text or "").strip()
                if not value:
                    continue
                if key in row:
                    row[key] = f"{row[key]}|{value}"
                else:
                    row[key] = value
            if row:
                rows.append(row)
        return rows

    def _map_feed(self, it: dict) -> dict | None:
        sku = _first(it, "sku", "code", "item_code", "product_code", "id", "ean", "gtin")
        if not sku:
            return None
        price = _num(_first(it, "price", "b2b_price", "selling_price", "sale_price"))
        srp = _num(_first(it, "srp", "retail_price", "rrp", "msrp", "original_price"))
        stock = _int(_first(it, "stock", "quantity", "qty", "inventory"))
        images = _split_values(_first(
            it, "images", "image", "image_url", "main_image", "picture", "pictures"))
        url = _first(it, "url", "product_url", "link", "deeplink")
        title = _first(it, "title", "name", "product_name", "item_name") or str(sku)
        return {
            "sku": str(sku),
            "spu": str(_first(it, "spu", "parent_sku", "item_group_id", "sku") or sku),
            "title": title,
            "description": _first(it, "description", "desc", "long_description"),
            "image_urls": images,
            "category_path": _first(it, "category", "category_path", "taxonomy"),
            "sale_price": price,
            "original_price": srp if srp is not None else price,
            "currency": _first(it, "currency", "price_currency") or self.currency,
            "gtin": _first(it, "ean", "gtin", "gtin13", "barcode"),
            "inventory": stock,
            "status": "on_sale" if stock is None or stock > 0 else "out_of_stock",
            "brand": _first(it, "brand", "manufacturer") or self.site.brand,
            "product_url": url,
            "site": self.site.site,
        }

    def _map_api(self, it: dict) -> dict | None:
        sku = it.get("sku") or it.get("code") or it.get("ean")
        if not sku:
            return None
        return {
            "sku": str(sku), "spu": str(it.get("sku") or sku),
            "title": it.get("title") or it.get("name"),
            "description": it.get("description"),
            "image_urls": it.get("images") or (
                [it.get("main_image")] if it.get("main_image") else []),
            "category_path": it.get("category"),
            "sale_price": _num(it.get("price") or it.get("b2b_price")),
            "original_price": _num(it.get("srp") or it.get("retail_price")
                                   or it.get("price")),
            "currency": it.get("currency") or self.currency,
            "gtin": it.get("ean") or it.get("gtin"),
            "inventory": it.get("stock"),
            "status": "on_sale" if (it.get("stock") or 0) else "out_of_stock",
            "brand": it.get("brand") or self.site.brand,
            "product_url": it.get("url"),
            "site": self.site.site,
        }

    # ---------- 路径2/3：storefront 爬取 ----------
    def _crawl_storefront(self) -> CrawlResult:
        result = CrawlResult()
        # sitemap_index + sub-sitemap 用统一 fetcher；proxy_tier 由 ProxyMiddleware 处理
        sitemap_fetcher = self.make_fetcher(kind="sitemap", source="vidaxl",
                                            timeout=30)

        sitemap_url = self.base + "/sitemap_index.xml"

        # proxy precheck（路径3 住宅代理预检）—— 逻辑保留，仅把 sess 替换为 fetcher
        _preflight_proxy: str | None = None
        if self.site.proxy_tier not in (None, "", "none"):
            try:
                from ..proxy_probe import probe_proxy_for_url
                probe = probe_proxy_for_url(
                    tier=self.site.proxy_tier,
                    site=self.site.site,
                    url=sitemap_url,
                    timeout=int(os.environ.get("PROXY_PREFLIGHT_TIMEOUT", "8")),
                )
                if not probe.ok and probe.failure:
                    failure = probe.failure
                    if (
                        self.site.site == "vidaxl_us"
                        and failure.code in ("http_401", "http_403", "http_5xx")
                    ):
                        from ..crawl_diagnostics import FailureInfo, STAGE_DISCOVER
                        failure = FailureInfo(
                            failure.code,
                            STAGE_DISCOVER,
                            "VidaXL US storefront 当前不可访问（公网/现有代理返回 "
                            f"HTTP {failure.http_status or 'blocked'}），"
                            "需官方 Dropshipping API 凭据或可访问 vidaxl.com 的美国住宅出口",
                            True,
                            "配置 VIDAXL_API_EMAIL / VIDAXL_API_TOKEN，或更换可访问 "
                            "vidaxl.com 的美国住宅代理后重跑",
                            failure.http_status,
                        )
                    _record_site_failure(
                        self.site.site,
                        self.job_id,
                        sitemap_url,
                        failure,
                        http_status=failure.http_status or probe.status_code,
                    )
                    result.notes.append(
                        f"⚠ 目标预检失败: {failure.code} · "
                        f"{failure.detail}")
                    return result
                if probe.proxy_url:
                    _preflight_proxy = probe.proxy_url
            except Exception as exc:
                # Preflight must never break legacy crawl behavior.
                result.notes.append(f"⚠ 代理预检跳过: {exc}")

        try:
            # 若预检返回了指定 proxy，透传给 fetcher；否则由 ProxyMiddleware 按 proxy_tier 选
            idx_kw = {}
            if _preflight_proxy:
                idx_kw["_proxy"] = _preflight_proxy
            idx = sitemap_fetcher.get(sitemap_url, **idx_kw)
            idx_status = idx.status or 0
            if idx_status != 200:
                # 路径 2.5：curl_cffi 被封 → fallback 到 StealthyFetcher（Camoufox patched playwright）
                if idx_status in (401, 403, 451):
                    stealth_text = self._fetch_via_stealth(sitemap_url)
                    if stealth_text:
                        result.notes.append(
                            f"✅ curl_cffi {idx_status} → StealthyFetcher 解锁成功")
                        subs = re.findall(r"<loc>\s*(.*?)\s*</loc>", stealth_text)
                    else:
                        result.notes.append(
                            f"⚠ sitemap_index 不可达（{idx_status}）+ stealth 也失败")
                        self.guard(idx_status, self.base)
                        return result
                else:
                    _record_site_status_failure(
                        self.site.site,
                        self.job_id,
                        sitemap_url,
                        idx_status,
                    )
                    self.guard(idx_status, self.base)    # 熔断检查
                    result.notes.append(
                        f"⚠ sitemap_index 不可达（{idx_status}）—— "
                        f"{'美国站需住宅代理（路径3）' if self.site.country=='US' else '站点封锁'}")
                    return result
            else:
                subs = re.findall(r"<loc>\s*(.*?)\s*</loc>", idx.text)
        except BlockedError:
            raise                              # 熔断 —— 传播到 runner
        except Exception as exc:
            _record_site_exception(
                self.site.site,
                self.job_id,
                sitemap_url,
                exc,
            )
            result.notes.append(f"⚠ 站点不可达: {exc} —— 建议走路径1 官方 API")
            return result

        prod_sitemaps = [u for u in subs if "custom-product" in u]
        if not prod_sitemaps:
            # vidaxl_ca：sitemap_index 返回 200 但 body 是空 <sitemapindex/>。
            # 实测（2026-05-19）确认根因：VidaXL 已暂停加拿大站运营，
            # 页面显示 "We're pausing orders until further notice."，
            # 类别页 0 商品，Search-FAQ 替代 Search-Show —— 不是技术问题。
            # 等 VidaXL 重开加拿大站后，sitemap 会自动填充，此处代码无需改动。
            raise RuntimeError(
                f"sitemap_index 返回 200 但无 custom-product 子 sitemap "
                f"（{len(subs)} 个 <loc>，0 个匹配）。"
                f"已知原因（vidaxl_ca）：VidaXL 已暂停该市场运营，"
                f"类别页显示 'pausing orders until further notice'，"
                f"无商品可采集，需等业务重开。")
        # 全量读 sitemap（不再受 self.limit 截断）
        # 注意：URL 中的 EAN-13 ≠ JSON-LD 里的 SKU（vidaxl 用内部 item code），
        # 所以按 URL dedup 才能让 resume 正确推进，按 EAN 反而把所有 URL 视作未抓。
        # 同一 product 的 variant URL 会在 upsert 阶段被 JSON-LD-SKU 自然去重。
        urls_seen: set[str] = set()
        urls: list[str] = []
        for sm in prod_sitemaps:
            try:
                sm_res = sitemap_fetcher.get(sm, timeout=40)
                raw = sm_res.content
                xml = (gzip.decompress(raw) if sm.endswith(".gz")
                       else raw).decode("utf-8", "ignore")
                for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml):
                    if u in urls_seen:
                        continue
                    urls_seen.add(u)
                    urls.append(u)
            except Exception:
                continue
        _persist_sitemap_total(self.site.site, len(urls))
        # Resume：按 product_url 跳过 DB 里已抓，让多轮 run 推进 sitemap
        already = _already_crawled_urls(self.site.site)
        fresh = [u for u in urls if u not in already]
        # 随机洗牌：sitemap 把同一 parent 的 variant URL 聚集在一起，
        # 顺序切片会让每个 run 只命中少量 unique parent。随机采样能让
        # 每个 run 覆盖更多 parent（vidaxl 平均每 parent ~5 变体，
        # 期望 unique parent 数 ≈ 总 parent × (1 - (1 - limit/总URL)^5)）
        import random
        random.shuffle(fresh)
        run_limit = min(self.limit, RUN_TARGET_LIMIT) if RUN_TARGET_LIMIT > 0 else self.limit
        targets = fresh[: run_limit]
        _register_frontier_targets(self.site.site, targets)
        result.notes.append(
            f"路径2 storefront：{len(prod_sitemaps)} 个 sitemap · "
            f"sitemap 总 URL {len(urls)} · 已抓 URL {len(already)} · "
            f"本次目标 {len(targets)}（VIDAXL_RUN_TARGET_LIMIT={RUN_TARGET_LIMIT}）")

        # 走代理池：并发 N 线程（默认 10，与 residential 池容量匹配）
        from .. import proxy_pool
        max_workers = int(os.environ.get("VIDAXL_CONCURRENCY", "10"))
        retries = int(os.environ.get("VIDAXL_RETRIES", "2"))
        counters = {"ok": 0, "http_4xx": 0, "http_5xx": 0,
                    "timeout": 0, "parse_none": 0, "exception": 0}
        counters_lock = threading.Lock()
        products_lock = threading.Lock()

        def _inc(key: str) -> None:
            with counters_lock:
                counters[key] = counters.get(key, 0) + 1

        def _try_fetch(url: str) -> tuple[int, str]:
            """返回 (status_code, html)；status -1 表示 timeout/连接错误。"""
            cur_proxy = proxy_pool.get_proxy(self.site.proxy_tier or "residential",
                                             site=self.site.site)
            local_sess = creq.Session(impersonate="chrome")
            if cur_proxy:
                local_sess.proxies = {"http": cur_proxy, "https": cur_proxy}
            try:
                resp = local_sess.get(url, timeout=30)
                if resp.status_code in (429, 403):
                    proxy_pool.report_failure(cur_proxy, hard=True)
                elif 500 <= resp.status_code < 600:
                    proxy_pool.report_failure(cur_proxy)
                else:
                    proxy_pool.report_success(cur_proxy)
                return resp.status_code, resp.text
            except Exception:
                proxy_pool.report_failure(cur_proxy)
                return -1, ""

        def _fetch_one(url: str) -> None:
            last_status = 0
            for attempt in range(retries + 1):
                status, html = _try_fetch(url)
                last_status = status
                if status == 200 and html:
                    self.snapshot(url.rstrip("/").split("/")[-1], html)
                    row = self._parse_jsonld(html, url)
                    if row:
                        _log_fetched(self.site.site, url, 200,
                                     parsed=True, job_id=self.job_id)
                        with products_lock:
                            result.products.append(row)
                        with counters_lock:
                            self.counter.api_calls += 1
                        _inc("ok")
                        return
                    _log_fetched(self.site.site, url, 200,
                                 parse_failed=True, job_id=self.job_id)
                    _inc("parse_none")
                    return  # 解析失败不重试（页面就那样）
                if status == -1 or status >= 500:
                    continue  # 临时错误，重试
                if 400 <= status < 500:
                    _log_fetched(self.site.site, url, status,
                                 job_id=self.job_id)
                    _inc("http_4xx")
                    return  # 4xx 不重试（除 429 已在内部 ban 代理）
            # 重试用尽 —— 即使失败也记录已尝试，避免下轮再抓
            _log_fetched(self.site.site, url, last_status if last_status > 0 else 0,
                         job_id=self.job_id)
            if last_status == -1:
                _inc("timeout")
            elif last_status >= 500:
                _inc("http_5xx")
            else:
                _inc("exception")

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_fetch_one, u) for u in targets]
            for _ in as_completed(futures):
                pass
        msg = (f"并发 {max_workers}·重试 {retries} · "
               f"成功 {counters['ok']}/{len(targets)} · "
               f"4xx={counters['http_4xx']} 5xx={counters['http_5xx']} "
               f"timeout={counters['timeout']} "
               f"parse_none={counters['parse_none']} exc={counters['exception']}")
        result.notes.append(msg)
        # 同时输出到 stdout 让 docker logs 可见
        print(f"[vidaxl/{self.site.site}] {msg}", flush=True)
        for n in result.notes:
            print(f"[vidaxl/{self.site.site}] note: {n}", flush=True)
        return result

    def _parse_jsonld(self, html: str, url: str) -> dict | None:
        for block in _LD_RE.findall(html):
            try:
                doc = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            graph = (doc if isinstance(doc, list)
                     else doc.get("@graph", [doc]) if isinstance(doc, dict)
                     else [])
            # Vidaxl JSON-LD 用 ItemPage 包裹，商品在 mainEntity 里
            expanded = []
            for node in graph:
                expanded.append(node)
                if isinstance(node, dict) and isinstance(
                        node.get("mainEntity"), dict):
                    expanded.append(node["mainEntity"])
            for it in expanded:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type")
                if t != "Product" and not (isinstance(t, list) and "Product" in t):
                    continue
                offers = it.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                brand = it.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                rating = it.get("aggregateRating") or {}
                imgs = it.get("image")
                imgs = [imgs] if isinstance(imgs, str) else (imgs or [])
                avail = str(offers.get("availability", "")).lower()
                price = _num(offers.get("price"))
                return {
                    "sku": it.get("sku") or it.get("mpn")
                    or url.rstrip("/").split("/")[-1].replace(".html", ""),
                    "spu": it.get("sku") or it.get("mpn"),
                    "title": it.get("name"),
                    "description": it.get("description"),
                    "image_urls": imgs,
                    "sale_price": price, "original_price": price,
                    "currency": offers.get("priceCurrency") or self.currency,
                    "gtin": it.get("gtin13") or it.get("gtin"),
                    "mpn": it.get("mpn"),
                    "ratings": _num(rating.get("ratingValue")),
                    "review_count": _int(rating.get("reviewCount")),
                    "status": "out_of_stock" if "outofstock" in avail
                    else "on_sale",
                    "brand": brand or self.site.brand,
                    "product_url": url,
                    "site": self.site.site,
                }
        return None

    def _fetch_via_stealth(self, url: str) -> str | None:
        """curl_cffi 被封时（401/403/451）走 Scrapling StealthyFetcher 兜底。

        反爬参数升级（2026-05-24 整合）：solve_cloudflare / hide_canvas /
        block_webrtc / dns_over_https / locale / timezone_id / per-site profile。
        参考 deliverables/scrapling_design_research.html。
        """
        try:
            from scrapling.fetchers import StealthyFetcher
            from ._stealth_config import stealth_kwargs
        except Exception as exc:
            return None
        try:
            kw = stealth_kwargs(
                proxy=self.proxy,
                country=self.site.country,
                persist_profile_key=f"vidaxl_{self.site.site}",
                timeout_ms=45000,
            )
            page = self.count_browser_fetch(
                lambda: StealthyFetcher.fetch(url, **kw),
                success=lambda p: getattr(p, "status", None) == 200,
            )
            if getattr(page, "status", None) == 200:
                return page.html_content or page.body or ""
        except Exception:
            pass
        return None


_SITEMAP_TOTALS_PATH = os.environ.get(
    "SITEMAP_TOTALS_PATH", "/app/data/sitemap_totals.json")


def _persist_sitemap_total(site: str, total: int) -> None:
    """记录某站 sitemap 真实 URL 总数 —— dashboard 用它做「应抓」基准。"""
    import json
    try:
        os.makedirs(os.path.dirname(_SITEMAP_TOTALS_PATH), exist_ok=True)
        try:
            with open(_SITEMAP_TOTALS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        data[site] = int(total)
        tmp = _SITEMAP_TOTALS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SITEMAP_TOTALS_PATH)
    except Exception:
        pass


def _already_crawled_urls(site: str) -> set[str]:
    """读取该 site 已处理过的 URL —— 100% 推进 sitemap 的关键。

    统一使用 crawl_urls frontier。已解析 / 已阻断 / 有抓取尝试次数的 URL
    都不再进入本轮随机目标；Product.product_url 作为旧数据回填兜底。
    """
    try:
        from ..db import SessionLocal
        from ..models import CrawlUrl, Product
    except Exception:
        return set()
    db = SessionLocal()
    try:
        frontier = {
            r.url for r in (
                db.query(CrawlUrl.url)
                .filter(CrawlUrl.site == site)
                .filter(CrawlUrl.url.isnot(None))
                .filter((CrawlUrl.status.in_(("parsed", "blocked")))
                        | (CrawlUrl.attempts > 0))
                .all()
            )
            if r.url
        }
        products = {
            r.product_url for r in (
                db.query(Product.product_url)
                .filter(Product.site == site)
                .filter(Product.product_url.isnot(None))
                .all()
            )
            if r.product_url
        }
        return frontier | products
    finally:
        db.close()


def _register_frontier_targets(site: str, urls: list[str]) -> None:
    """Mirror this run's target URLs into the durable frontier.

    Vidaxl sitemaps can contain hundreds of thousands of URLs, so we register
    only the sampled targets for the current run here. The sidecar total still
    records the full sitemap size for coverage math.
    """
    if not urls:
        return
    try:
        from ..db import SessionLocal
        from ..frontier import register_urls
    except Exception:
        return
    db = SessionLocal()
    try:
        register_urls(
            db,
            site=site,
            urls=urls,
            kind="product",
            source="vidaxl_sitemap",
            priority=40,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _log_fetched(site: str, url: str, status_code: int, *,
                 parsed: bool = False,
                 parse_failed: bool = False,
                 job_id: int | None = None) -> None:
    """记录每次 URL fetch 到 crawl_urls frontier 和 crawl_failures。"""
    _record_frontier_fetch(site, url, status_code, parsed=parsed,
                           parse_failed=parse_failed, job_id=job_id)


def _record_frontier_fetch(site: str, url: str, status_code: int, *,
                           parsed: bool = False,
                           parse_failed: bool = False,
                           job_id: int | None = None) -> None:
    try:
        from ..crawl_diagnostics import (
            PARSE_NO_JSONLD,
            STAGE_PARSE,
            FailureInfo,
            classify_exception,
            classify_http_status,
            record_failure,
            record_url_state,
        )
        from ..db import SessionLocal
    except Exception:
        return
    failure = None
    status = "parsed" if parsed else "fetched"
    if parse_failed:
        failure = FailureInfo(
            PARSE_NO_JSONLD,
            STAGE_PARSE,
            "Vidaxl 商品页 200 但未解析到 Product JSON-LD",
            True,
            "检查页面快照和 JSON-LD 结构；必要时增加解析 fallback",
            status_code,
        )
        status = "failed"
    elif status_code <= 0:
        failure = classify_exception(TimeoutError("vidaxl product fetch timeout"))
        status = "failed"
    elif status_code >= 400:
        failure = classify_http_status(status_code)
        status = "blocked" if status_code in (401, 403, 429) else "failed"
    db = SessionLocal()
    try:
        record_url_state(
            db,
            site=site,
            url=url,
            kind="product",
            source="vidaxl_sitemap",
            status=status,
            http_status=status_code if status_code > 0 else None,
            failure=failure,
            fetcher="curl_cffi",
        )
        if failure:
            record_failure(
                db,
                site=site,
                job_id=job_id,
                url=url,
                info=failure,
                fetcher="curl_cffi",
                proxy_tier="residential",
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _record_site_exception(site: str, job_id: int | None, url: str,
                           exc: Exception) -> None:
    try:
        from ..crawl_diagnostics import classify_exception
    except Exception:
        return
    _record_site_failure(site, job_id, url, classify_exception(exc))


def _record_site_failure(site: str, job_id: int | None, url: str,
                         failure, http_status: int | None = None) -> None:
    try:
        from ..crawl_diagnostics import record_failure, record_url_state
        from ..db import SessionLocal
    except Exception:
        return
    db = SessionLocal()
    try:
        record_url_state(
            db,
            site=site,
            url=url,
            kind="sitemap",
            source="vidaxl_sitemap_index",
            status="blocked" if failure.code in ("http_401", "http_403", "http_429")
            else "failed",
            http_status=http_status or failure.http_status,
            failure=failure,
            fetcher="curl_cffi",
        )
        record_failure(
            db,
            site=site,
            job_id=job_id,
            url=url,
            info=failure,
            fetcher="curl_cffi",
            proxy_tier="residential",
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _record_site_status_failure(site: str, job_id: int | None, url: str,
                                status_code: int) -> None:
    try:
        from ..crawl_diagnostics import (
            classify_http_status,
            record_failure,
            record_url_state,
        )
        from ..db import SessionLocal
    except Exception:
        return
    failure = classify_http_status(status_code)
    if not failure:
        return
    _record_site_failure(site, job_id, url, failure, http_status=status_code)


def _already_crawled_skus(site: str) -> set[str]:
    """读取该 site 已落库的 SKU 集合 —— resume 推荐用 SKU 去重（不是 URL）。"""
    try:
        from ..db import SessionLocal
        from ..models import Product
    except Exception:
        return set()
    db = SessionLocal()
    try:
        rows = (db.query(Product.sku)
                .filter(Product.site == site)
                .filter(Product.sku.isnot(None))
                .all())
        return {r[0] for r in rows if r[0]}
    finally:
        db.close()


def _first(data: dict, *keys: str):
    lowered = {str(k).lower(): v for k, v in (data or {}).items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _split_values(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    parts = re.split(r"\s*[|,]\s*", text)
    return [p for p in (part.strip() for part in parts) if p]


def _num(v):
    if v is None:
        return None
    m = re.search(r"[\d.]+", str(v).replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
