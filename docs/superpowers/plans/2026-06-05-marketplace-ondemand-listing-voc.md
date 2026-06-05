# 跨境电商平台「指定 URL → listing + VOC」按需抓取 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 smart-crawler 新增美客多 / Lazada / 虾皮(Shopee)三平台的「指定 URL → 抓 listing + 评论原文」按需抓取能力,贯通 底层 / CLI / MCP / Web 四层入口。

**Architecture:** 新增独立 `app/ondemand/` 子系统,核心是 `fetch(url)` 纯编排函数。每平台采集器把「HTTP 拉取」与「解析」分离 —— 解析为纯函数(用提交到仓库的 fixture 做单元测试),HTTP 部分走 smoke 测试。listing 入现有 `Product` 表(经 `pipeline.upsert_products`),评论入现有 `Review` 表(沿用 `review_runner._upsert_reviews` 同款去重)。复用现有 `proxy`/`antiban`/`snapshot`/`config`。

**Tech Stack:** Python 3.12 · curl_cffi(chrome 指纹直连)· SQLAlchemy · FastMCP · FastAPI · pytest(unit/smoke 双 marker)· 现有 Vue3 单文件前端。

**对应设计文档:** `docs/superpowers/specs/2026-06-05-marketplace-ondemand-listing-voc-design.md`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/app/ondemand/__init__.py` | 导出 `fetch`、`FetchResult` |
| `backend/app/ondemand/registry.py` | `detect_platform(url)` 按域名识别;`classify_url(url)` 判 product/listing |
| `backend/app/ondemand/base.py` | `OnDemandResult` 数据类;`BaseOnDemand` 抽象基类(复用 proxy/antiban/snapshot/sleep) |
| `backend/app/ondemand/mercadolibre.py` | URL→itemId 解析 + items/reviews 解析(纯函数)+ HTTP 抓取 |
| `backend/app/ondemand/lazada.py` | URL→itemId 解析 + pdp/review 解析(纯函数)+ HTTP 抓取 |
| `backend/app/ondemand/shopee.py` | URL→(shopid,itemid)解析 + pdp/ratings 解析(纯函数)+ HTTP 抓取 |
| `backend/app/ondemand/runner.py` | `fetch(url, *, max_items, review_limit)` 编排 + 入库(Product/Review) |
| `backend/app/cli.py`(改) | 新增 `fetch-url` 子命令 |
| `backend/app/mcp_server.py`(改) | 新增 `fetch_listing_voc` MCP 工具 |
| `backend/app/api/routes.py`(改) | 新增 `POST /api/ondemand/fetch` 端点 |
| `frontend/index.html`(改) | 新增「指定 URL 抓取」输入框 + 结果展示 |
| `backend/tests/test_ondemand_registry.py` | registry 单测 |
| `backend/tests/test_ondemand_mercadolibre.py` | 美客多解析单测 |
| `backend/tests/test_ondemand_lazada.py` | Lazada 解析单测 |
| `backend/tests/test_ondemand_shopee.py` | Shopee 解析单测 |
| `backend/tests/test_ondemand_runner.py` | 编排+入库单测(注入假采集器) |
| `backend/tests/fixtures/ondemand/*.json` | 各平台 API 响应样本 |

**关键约定(贯穿全部采集器):**
- listing 解析产出 dict 必须含 `pipeline.REQUIRED = ("sku","title","product_url","site")` 四个字段,否则被 `upsert_products` 跳过。`site` 用 `"ondemand_<platform>"`(如 `ondemand_shopee`),`sku` 用平台 itemId。
- 评论解析产出 dict 字段对齐 `review_runner._upsert_reviews` 读取的键:`review_id`/`platform`/`site`/`reviewer_name`/`rating`/`title`/`content`/`review_date`/`product_url` 等。`platform` 用 `"ondemand_<platform>"`,`review_id` 用平台评论 ID。
- 解析函数签名统一:`parse_listing(data: dict, url: str) -> dict`、`parse_reviews(data: dict, item_id: str, url: str) -> list[dict]`、`parse_item_id(url: str) -> str`(Shopee 返回 `tuple[str,str]`)。

---

## Task 1: ondemand 包骨架 + 结果数据类

**Files:**
- Create: `backend/app/ondemand/__init__.py`
- Create: `backend/app/ondemand/base.py`
- Test: `backend/tests/test_ondemand_registry.py`(本任务先放结果类的测试)

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_registry.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_ondemand_result_accumulates():
    from app.ondemand.base import OnDemandResult

    r = OnDemandResult()
    r.add_listing({"sku": "X1", "title": "Chair", "site": "ondemand_shopee",
                   "product_url": "u"})
    r.add_reviews([{"review_id": "r1"}, {"review_id": "r2"}])
    r.note("done")

    assert len(r.listings) == 1
    assert len(r.reviews) == 2
    assert r.notes == ["done"]
    assert r.summary()["listings"] == 1
    assert r.summary()["reviews"] == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/ondemand/__init__.py`:

```python
"""按需(on-demand)抓取子系统 —— 指定 URL → listing + VOC。

与整站枚举(crawlers/ + runner.py)解耦:输入一条 URL(单品或列表页),
抓取该 listing 的商品信息 + 评论原文。支持 美客多 / Lazada / 虾皮。
"""
from __future__ import annotations

from .base import OnDemandResult

__all__ = ["OnDemandResult"]
```

创建 `backend/app/ondemand/base.py`:

```python
"""按需抓取的结果容器与采集器基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class OnDemandResult:
    """一次 fetch(url) 的产出:listing 列表 + 评论列表 + 备注。"""

    def __init__(self):
        self.listings: list[dict] = []
        self.reviews: list[dict] = []
        self.notes: list[str] = []

    def add_listing(self, listing: dict) -> None:
        if listing:
            self.listings.append(listing)

    def add_reviews(self, reviews: list[dict]) -> None:
        self.reviews.extend(r for r in (reviews or []) if r)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def summary(self) -> dict:
        return {"listings": len(self.listings),
                "reviews": len(self.reviews),
                "notes": list(self.notes)}


class BaseOnDemand(ABC):
    """平台采集器基类。子类实现解析(纯函数)与 HTTP 抓取。

    platform:    平台标识,如 "mercadolibre" / "lazada" / "shopee"
    proxy_tier:  默认代理档,被 runner 用于取 proxy
    """

    platform = "base"
    proxy_tier = "none"

    @staticmethod
    @abstractmethod
    def parse_item_id(url: str):
        """从商品 URL 解析平台商品 ID。Shopee 返回 (shopid, itemid)。"""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def parse_listing(data: dict, url: str) -> dict:
        """把平台商品 JSON 解析成可入 Product 表的标准 dict。"""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        """把平台评论 JSON 解析成可入 Review 表的 dict 列表。"""
        raise NotImplementedError
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_registry.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/ondemand/__init__.py backend/app/ondemand/base.py backend/tests/test_ondemand_registry.py
git commit -m "feat(ondemand): add package skeleton and OnDemandResult"
```

---

## Task 2: 平台识别与 URL 分类(registry)

**Files:**
- Create: `backend/app/ondemand/registry.py`
- Test: `backend/tests/test_ondemand_registry.py`(追加)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ondemand_registry.py` 末尾追加:

```python
def test_detect_platform_by_domain():
    from app.ondemand.registry import detect_platform

    assert detect_platform("https://articulo.mercadolibre.com.mx/MLM-123") == "mercadolibre"
    assert detect_platform("https://www.lazada.com.my/products/x-i123-s456.html") == "lazada"
    assert detect_platform("https://shopee.com.my/product-i.111.222") == "shopee"
    assert detect_platform("https://example.com/foo") is None


def test_classify_url_product_vs_listing():
    from app.ondemand.registry import classify_url

    # 美客多:商品页含 MLM-/MLB-/MLA- 编码
    assert classify_url("https://articulo.mercadolibre.com.mx/MLM-123456789-chair") == "product"
    # 美客多:店铺/搜索页
    assert classify_url("https://listado.mercadolibre.com.mx/sillas") == "listing"
    # Shopee:单品 i.shopid.itemid
    assert classify_url("https://shopee.com.my/product-i.111.222") == "product"
    # Shopee:店铺页
    assert classify_url("https://shopee.com.my/shop123") == "listing"
    # Lazada:/products/...html 为单品
    assert classify_url("https://www.lazada.com.my/products/x-i123-s456.html") == "product"
    # Lazada:类目页
    assert classify_url("https://www.lazada.com.my/shop/abc/") == "listing"


def test_get_crawler_returns_platform_class():
    from app.ondemand.registry import get_crawler

    assert get_crawler("mercadolibre").platform == "mercadolibre"
    assert get_crawler("lazada").platform == "lazada"
    assert get_crawler("shopee").platform == "shopee"
    with pytest.raises(ValueError):
        get_crawler("unknown")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand.registry'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/ondemand/registry.py`:

```python
"""平台识别与 URL 分类 —— 按域名选采集器,按路径判单品/列表页。"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# 域名关键字 → 平台。Shopee/Lazada/美客多 各有多国域名。
_DOMAIN_MARKERS = (
    ("mercadolibre", "mercadolibre"),
    ("mercadolivre", "mercadolibre"),   # 巴西站
    ("lazada", "lazada"),
    ("shopee", "shopee"),
)

# 单品 URL 特征(命中即 product,否则按 listing)
_PRODUCT_PATTERNS = {
    "mercadolibre": re.compile(r"/ML[A-Z]-?\d+", re.I),   # MLM-123 / MLB123
    "lazada": re.compile(r"/products/.+\.html", re.I),
    "shopee": re.compile(r"-i\.\d+\.\d+|/product/\d+/\d+", re.I),
}


def detect_platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for marker, platform in _DOMAIN_MARKERS:
        if marker in host:
            return platform
    return None


def classify_url(url: str) -> str:
    """返回 'product' 或 'listing'。无法识别平台时默认 'product'。"""
    platform = detect_platform(url)
    pat = _PRODUCT_PATTERNS.get(platform)
    if pat and pat.search(url):
        return "product"
    return "listing"


def get_crawler(platform: str):
    if platform == "mercadolibre":
        from .mercadolibre import MercadoLibreOnDemand
        return MercadoLibreOnDemand()
    if platform == "lazada":
        from .lazada import LazadaOnDemand
        return LazadaOnDemand()
    if platform == "shopee":
        from .shopee import ShopeeOnDemand
        return ShopeeOnDemand()
    raise ValueError(f"未知按需抓取平台: {platform}")
```

> 注:`get_crawler` 在 Task 3-5 实现各平台类前会因 import 失败。本步只需 registry 三个纯函数测试通过;`test_get_crawler_returns_platform_class` 会失败 —— **先在该测试加 `@pytest.mark.skip(reason="平台类待 Task 3-5 实现")`,Task 5 完成后移除 skip。**

- [ ] **Step 4: 跑测试确认通过(get_crawler 用例暂 skip)**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_registry.py -v`
Expected: PASS(`test_get_crawler_returns_platform_class` 显示 SKIPPED)

- [ ] **Step 5: 提交**

```bash
git add backend/app/ondemand/registry.py backend/tests/test_ondemand_registry.py
git commit -m "feat(ondemand): add platform detection and url classification"
```

---

## Task 3: 美客多采集器(解析纯函数 + HTTP)

**Files:**
- Create: `backend/app/ondemand/mercadolibre.py`
- Create: `backend/tests/fixtures/ondemand/ml_item.json`
- Create: `backend/tests/fixtures/ondemand/ml_reviews.json`
- Test: `backend/tests/test_ondemand_mercadolibre.py`

- [ ] **Step 1: 建 fixture**

创建 `backend/tests/fixtures/ondemand/ml_item.json`(美客多 `items/{id}` 响应精简样本):

```json
{
  "id": "MLM123456789",
  "title": "Silla de Oficina Ergonómica Negra",
  "price": 1299.0,
  "original_price": 1899.0,
  "currency_id": "MXN",
  "permalink": "https://articulo.mercadolibre.com.mx/MLM-123456789-silla",
  "thumbnail": "https://http2.mlstatic.com/D_NQ_123-O.jpg",
  "pictures": [
    {"url": "https://http2.mlstatic.com/D_NQ_123-O.jpg"},
    {"url": "https://http2.mlstatic.com/D_NQ_456-O.jpg"}
  ],
  "available_quantity": 50,
  "sold_quantity": 120,
  "condition": "new"
}
```

创建 `backend/tests/fixtures/ondemand/ml_reviews.json`(美客多 `reviews/item/{id}` 响应精简样本):

```json
{
  "paging": {"total": 2},
  "reviews": [
    {
      "id": "rev-1",
      "rate": 5,
      "title": "Excelente",
      "content": "Muy cómoda y resistente.",
      "date_created": "2026-04-10T12:00:00.000-04:00",
      "reviewer_id": "U1",
      "valorization": 8
    },
    {
      "id": "rev-2",
      "rate": 2,
      "title": "Regular",
      "content": "Llegó con un rayón.",
      "date_created": "2026-03-01T09:30:00.000-04:00",
      "reviewer_id": "U2",
      "valorization": 1
    }
  ]
}
```

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_ondemand_mercadolibre.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ondemand.mercadolibre import MercadoLibreOnDemand

pytestmark = pytest.mark.unit

FX = Path(__file__).parent / "fixtures" / "ondemand"


def test_parse_item_id_from_url():
    f = MercadoLibreOnDemand.parse_item_id
    assert f("https://articulo.mercadolibre.com.mx/MLM-123456789-silla") == "MLM123456789"
    assert f("https://produto.mercadolivre.com.br/MLB-987654321-mesa") == "MLB987654321"
    with pytest.raises(ValueError):
        f("https://articulo.mercadolibre.com.mx/sin-codigo")


def test_parse_listing_maps_required_fields():
    data = json.loads((FX / "ml_item.json").read_text(encoding="utf-8"))
    p = MercadoLibreOnDemand.parse_listing(data, data["permalink"])

    assert p["sku"] == "MLM123456789"
    assert p["title"] == "Silla de Oficina Ergonómica Negra"
    assert p["sale_price"] == 1299.0
    assert p["original_price"] == 1899.0
    assert p["currency"] == "MXN"
    assert p["site"] == "ondemand_mercadolibre"
    assert p["product_url"] == data["permalink"]
    assert p["image_urls"] == [
        "https://http2.mlstatic.com/D_NQ_123-O.jpg",
        "https://http2.mlstatic.com/D_NQ_456-O.jpg",
    ]
    # 入库必填四字段齐全
    for k in ("sku", "title", "product_url", "site"):
        assert p[k]


def test_parse_reviews_maps_fields():
    data = json.loads((FX / "ml_reviews.json").read_text(encoding="utf-8"))
    rs = MercadoLibreOnDemand.parse_reviews(data, "MLM123456789",
                                            "https://x/MLM-123456789")
    assert len(rs) == 2
    first = rs[0]
    assert first["review_id"] == "rev-1"
    assert first["platform"] == "ondemand_mercadolibre"
    assert first["site"] == "ondemand_mercadolibre"
    assert first["rating"] == 5
    assert first["title"] == "Excelente"
    assert first["content"] == "Muy cómoda y resistente."
    assert first["sku"] == "MLM123456789"
    assert first["review_date"] == "2026-04-10T12:00:00.000-04:00"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_mercadolibre.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand.mercadolibre'`

- [ ] **Step 4: 写最小实现**

创建 `backend/app/ondemand/mercadolibre.py`:

```python
"""美客多(MercadoLibre)按需采集器。

listing:  GET https://api.mercadolibre.com/items/{id}
reviews:  GET https://api.mercadolibre.com/reviews/item/{id}
URL→id:   商品页 URL 含 MLM-123 / MLB-123 / MLA-123 编码,去掉短横即 itemId。
反爬:     公开 API,最宽松,默认直连(proxy_tier=none)。
"""
from __future__ import annotations

import re

from curl_cffi import requests as creq

from ..antiban import check_blocked
from .base import BaseOnDemand

_API = "https://api.mercadolibre.com"
_ID_RE = re.compile(r"(ML[A-Z])-?(\d+)", re.I)
PLATFORM = "mercadolibre"
SITE = f"ondemand_{PLATFORM}"


class MercadoLibreOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "none"

    @staticmethod
    def parse_item_id(url: str) -> str:
        m = _ID_RE.search(url)
        if not m:
            raise ValueError(f"美客多 URL 无商品编码: {url}")
        return (m.group(1) + m.group(2)).upper()

    @staticmethod
    def parse_listing(data: dict, url: str) -> dict:
        return {
            "sku": data.get("id"),
            "title": data.get("title"),
            "sale_price": data.get("price"),
            "original_price": data.get("original_price") or data.get("price"),
            "currency": data.get("currency_id"),
            "image_urls": [p.get("url") for p in (data.get("pictures") or [])
                           if p.get("url")],
            "inventory": str(data.get("available_quantity"))
            if data.get("available_quantity") is not None else None,
            "status": "on_sale" if data.get("condition") == "new" else data.get("condition"),
            "product_url": url,
            "site": SITE,
            "brand": PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        out = []
        for r in (data.get("reviews") or []):
            out.append({
                "review_id": r.get("id"),
                "platform": SITE,
                "site": SITE,
                "reviewer_name": r.get("reviewer_id"),
                "rating": r.get("rate"),
                "title": r.get("title"),
                "content": r.get("content"),
                "review_date": r.get("date_created"),
                "sku": item_id,
                "product_url": url,
            })
        return out

    # ---- HTTP(smoke 路径,单测不覆盖)----
    def _session(self, proxy: str | None) -> "creq.Session":
        s = creq.Session(impersonate="chrome")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def fetch_listing(self, item_id: str, url: str, proxy=None) -> dict:
        s = self._session(proxy)
        resp = s.get(f"{_API}/items/{item_id}", timeout=30)
        check_blocked(resp.status_code, f"ml/items/{item_id}")
        resp.raise_for_status()
        return self.parse_listing(resp.json(), url)

    def fetch_reviews(self, item_id: str, url: str, limit: int = 100,
                      proxy=None) -> list[dict]:
        s = self._session(proxy)
        resp = s.get(f"{_API}/reviews/item/{item_id}", timeout=30)
        check_blocked(resp.status_code, f"ml/reviews/{item_id}")
        resp.raise_for_status()
        return self.parse_reviews(resp.json(), item_id, url)[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100,
                          proxy=None) -> list[str]:
        """列表/搜索页枚举 itemId。美客多搜索 API:
        GET /sites/{SITE_ID}/search?q=... 或店铺 API。首版用页面内 ML 编码兜底。"""
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "ml/listing")
        resp.raise_for_status()
        ids = []
        for m in _ID_RE.finditer(resp.text):
            iid = (m.group(1) + m.group(2)).upper()
            if iid not in ids:
                ids.append(iid)
            if len(ids) >= max_items:
                break
        return ids
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_mercadolibre.py -v`
Expected: PASS(4 passed)

- [ ] **Step 6: 提交**

```bash
git add backend/app/ondemand/mercadolibre.py backend/tests/test_ondemand_mercadolibre.py backend/tests/fixtures/ondemand/ml_item.json backend/tests/fixtures/ondemand/ml_reviews.json
git commit -m "feat(ondemand): add MercadoLibre crawler with parse functions"
```

---

## Task 4: Lazada 采集器(解析纯函数 + HTTP)

**Files:**
- Create: `backend/app/ondemand/lazada.py`
- Create: `backend/tests/fixtures/ondemand/lazada_pdp.json`
- Create: `backend/tests/fixtures/ondemand/lazada_reviews.json`
- Test: `backend/tests/test_ondemand_lazada.py`

- [ ] **Step 1: 建 fixture**

创建 `backend/tests/fixtures/ondemand/lazada_pdp.json`(Lazada PDP 模块数据精简样本,对应页面内 `__moduleData__`):

```json
{
  "data": {
    "root": {
      "fields": {
        "product": {
          "items": [
            {
              "itemId": "1234567890",
              "skuId": "9876543210",
              "name": "Foldable Storage Box 3-Tier",
              "price": "39.90",
              "originalPrice": "59.90",
              "currency": "MYR",
              "image": "https://img.lazcdn.com/a.jpg",
              "images": ["https://img.lazcdn.com/a.jpg", "https://img.lazcdn.com/b.jpg"],
              "stock": 200
            }
          ]
        }
      }
    }
  }
}
```

创建 `backend/tests/fixtures/ondemand/lazada_reviews.json`(Lazada `getReviewList` 响应精简样本):

```json
{
  "model": {
    "items": [
      {
        "reviewId": "L-rev-1",
        "rating": 5,
        "reviewContent": "Good quality, fast delivery.",
        "reviewTitle": "",
        "reviewTime": "10 Apr 2026",
        "buyerName": "Ali",
        "upVotes": 3
      },
      {
        "reviewId": "L-rev-2",
        "rating": 3,
        "reviewContent": "Box is smaller than expected.",
        "reviewTitle": "",
        "reviewTime": "01 Mar 2026",
        "buyerName": "Siti",
        "upVotes": 0
      }
    ]
  }
}
```

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_ondemand_lazada.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ondemand.lazada import LazadaOnDemand

pytestmark = pytest.mark.unit

FX = Path(__file__).parent / "fixtures" / "ondemand"


def test_parse_item_id_from_url():
    f = LazadaOnDemand.parse_item_id
    assert f("https://www.lazada.com.my/products/box-i1234567890-s9876543210.html") == "1234567890"
    with pytest.raises(ValueError):
        f("https://www.lazada.com.my/shop/foo/")


def test_parse_listing_maps_required_fields():
    data = json.loads((FX / "lazada_pdp.json").read_text(encoding="utf-8"))
    url = "https://www.lazada.com.my/products/box-i1234567890-s9876543210.html"
    p = LazadaOnDemand.parse_listing(data, url)

    assert p["sku"] == "1234567890"
    assert p["title"] == "Foldable Storage Box 3-Tier"
    assert p["sale_price"] == 39.90
    assert p["original_price"] == 59.90
    assert p["currency"] == "MYR"
    assert p["site"] == "ondemand_lazada"
    assert p["product_url"] == url
    assert p["image_urls"][0] == "https://img.lazcdn.com/a.jpg"
    for k in ("sku", "title", "product_url", "site"):
        assert p[k]


def test_parse_reviews_maps_fields():
    data = json.loads((FX / "lazada_reviews.json").read_text(encoding="utf-8"))
    rs = LazadaOnDemand.parse_reviews(data, "1234567890", "https://x")
    assert len(rs) == 2
    assert rs[0]["review_id"] == "L-rev-1"
    assert rs[0]["platform"] == "ondemand_lazada"
    assert rs[0]["rating"] == 5
    assert rs[0]["content"] == "Good quality, fast delivery."
    assert rs[0]["reviewer_name"] == "Ali"
    assert rs[0]["review_date"] == "10 Apr 2026"
    assert rs[0]["sku"] == "1234567890"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_lazada.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand.lazada'`

- [ ] **Step 4: 写最小实现**

创建 `backend/app/ondemand/lazada.py`:

```python
"""Lazada 按需采集器。

listing:  商品页内嵌 JSON(__moduleData__ / pdp data)解析;HTTP 取页面后正则抠 JSON。
reviews:  GET https://my.lazada.com.my/pdp/review/getReviewList?itemId=...&pageNo=N
URL→id:   /products/<slug>-i<itemId>-s<skuId>.html
反爬:     中-高,默认住宅代理(proxy_tier=residential),有滑块风险。
"""
from __future__ import annotations

import json
import re

from curl_cffi import requests as creq

from ..antiban import check_blocked
from .base import BaseOnDemand

_ID_RE = re.compile(r"-i(\d+)(?:-s\d+)?\.html", re.I)
_MODULE_RE = re.compile(r"__moduleData__\s*=\s*(\{.*?\});", re.S)
PLATFORM = "lazada"
SITE = f"ondemand_{PLATFORM}"


def _to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


class LazadaOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "residential"

    @staticmethod
    def parse_item_id(url: str) -> str:
        m = _ID_RE.search(url)
        if not m:
            raise ValueError(f"Lazada URL 无 itemId: {url}")
        return m.group(1)

    @staticmethod
    def _first_item(data: dict) -> dict:
        items = (data.get("data", {}).get("root", {}).get("fields", {})
                 .get("product", {}).get("items", []))
        return items[0] if items else {}

    @staticmethod
    def parse_listing(data: dict, url: str) -> dict:
        it = LazadaOnDemand._first_item(data)
        imgs = it.get("images") or ([it["image"]] if it.get("image") else [])
        return {
            "sku": it.get("itemId"),
            "title": it.get("name"),
            "sale_price": _to_float(it.get("price")),
            "original_price": _to_float(it.get("originalPrice")) or _to_float(it.get("price")),
            "currency": it.get("currency"),
            "image_urls": imgs,
            "variant_id": it.get("skuId"),
            "inventory": str(it.get("stock")) if it.get("stock") is not None else None,
            "status": "on_sale",
            "product_url": url,
            "site": SITE,
            "brand": PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        out = []
        for r in (data.get("model", {}).get("items") or []):
            out.append({
                "review_id": r.get("reviewId"),
                "platform": SITE,
                "site": SITE,
                "reviewer_name": r.get("buyerName"),
                "rating": r.get("rating"),
                "title": r.get("reviewTitle"),
                "content": r.get("reviewContent"),
                "review_date": r.get("reviewTime"),
                "sku": item_id,
                "product_url": url,
            })
        return out

    # ---- HTTP(smoke 路径)----
    def _session(self, proxy):
        s = creq.Session(impersonate="chrome")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def fetch_listing(self, item_id: str, url: str, proxy=None) -> dict:
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "lazada/pdp")
        resp.raise_for_status()
        m = _MODULE_RE.search(resp.text)
        if not m:
            raise ValueError("Lazada PDP 未找到 __moduleData__")
        return self.parse_listing(json.loads(m.group(1)), url)

    def fetch_reviews(self, item_id: str, url: str, limit: int = 100,
                      proxy=None) -> list[dict]:
        s = self._session(proxy)
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        out, page = [], 1
        while len(out) < limit and page <= 20:
            api = (f"https://{host}/pdp/review/getReviewList"
                   f"?itemId={item_id}&pageSize=20&pageNo={page}")
            resp = s.get(api, timeout=30)
            check_blocked(resp.status_code, "lazada/reviews")
            resp.raise_for_status()
            batch = self.parse_reviews(resp.json(), item_id, url)
            if not batch:
                break
            out.extend(batch)
            page += 1
        return out[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100, proxy=None):
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "lazada/listing")
        resp.raise_for_status()
        ids = []
        for m in _ID_RE.finditer(resp.text):
            if m.group(1) not in ids:
                ids.append(m.group(1))
            if len(ids) >= max_items:
                break
        return ids
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_lazada.py -v`
Expected: PASS(3 passed)

- [ ] **Step 6: 提交**

```bash
git add backend/app/ondemand/lazada.py backend/tests/test_ondemand_lazada.py backend/tests/fixtures/ondemand/lazada_pdp.json backend/tests/fixtures/ondemand/lazada_reviews.json
git commit -m "feat(ondemand): add Lazada crawler with parse functions"
```

---

## Task 5: 虾皮(Shopee)采集器(解析纯函数 + HTTP)

**Files:**
- Create: `backend/app/ondemand/shopee.py`
- Create: `backend/tests/fixtures/ondemand/shopee_pdp.json`
- Create: `backend/tests/fixtures/ondemand/shopee_ratings.json`
- Test: `backend/tests/test_ondemand_shopee.py`
- Modify: `backend/tests/test_ondemand_registry.py`(移除 Task 2 的 skip)

- [ ] **Step 1: 建 fixture**

创建 `backend/tests/fixtures/ondemand/shopee_pdp.json`(Shopee `get_pc` 响应精简样本):

```json
{
  "data": {
    "item": {
      "itemid": 222,
      "shopid": 111,
      "name": "Wireless Mouse Ergonomic",
      "price": 1599000,
      "price_before_discount": 2999000,
      "currency": "VND",
      "stock": 80,
      "image": "abc123",
      "images": ["abc123", "def456"],
      "item_rating": {"rating_star": 4.7, "rating_count": [500, 2, 3, 10, 85, 400]},
      "historical_sold": 1200
    }
  }
}
```

创建 `backend/tests/fixtures/ondemand/shopee_ratings.json`(Shopee `get_ratings` 响应精简样本):

```json
{
  "data": {
    "ratings": [
      {
        "cmtid": 555,
        "rating_star": 5,
        "comment": "Works great, very smooth.",
        "ctime": 1744300800,
        "author_username": "buyer_a",
        "like_count": 4
      },
      {
        "cmtid": 556,
        "rating_star": 2,
        "comment": "Disconnects sometimes.",
        "ctime": 1740000000,
        "author_username": "buyer_b",
        "like_count": 0
      }
    ]
  }
}
```

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_ondemand_shopee.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ondemand.shopee import ShopeeOnDemand

pytestmark = pytest.mark.unit

FX = Path(__file__).parent / "fixtures" / "ondemand"


def test_parse_item_id_returns_shopid_itemid():
    f = ShopeeOnDemand.parse_item_id
    assert f("https://shopee.com.my/Mouse-i.111.222") == ("111", "222")
    assert f("https://shopee.vn/product/111/222") == ("111", "222")
    with pytest.raises(ValueError):
        f("https://shopee.com.my/shop-page")


def test_parse_listing_maps_required_fields():
    data = json.loads((FX / "shopee_pdp.json").read_text(encoding="utf-8"))
    url = "https://shopee.com.my/Mouse-i.111.222"
    p = ShopeeOnDemand.parse_listing(data, url)

    assert p["sku"] == "111_222"
    assert p["title"] == "Wireless Mouse Ergonomic"
    # Shopee 价格放大 100000 倍
    assert p["sale_price"] == 15.99
    assert p["original_price"] == 29.99
    assert p["currency"] == "VND"
    assert p["site"] == "ondemand_shopee"
    assert p["product_url"] == url
    assert p["ratings"] == 4.7
    assert p["image_urls"] == [
        "https://cf.shopee.com.my/file/abc123",
        "https://cf.shopee.com.my/file/def456",
    ]
    for k in ("sku", "title", "product_url", "site"):
        assert p[k]


def test_parse_reviews_maps_fields():
    data = json.loads((FX / "shopee_ratings.json").read_text(encoding="utf-8"))
    rs = ShopeeOnDemand.parse_reviews(data, ("111", "222"), "https://x")
    assert len(rs) == 2
    assert rs[0]["review_id"] == "555"
    assert rs[0]["platform"] == "ondemand_shopee"
    assert rs[0]["rating"] == 5
    assert rs[0]["content"] == "Works great, very smooth."
    assert rs[0]["reviewer_name"] == "buyer_a"
    assert rs[0]["sku"] == "111_222"
    # ctime(epoch 秒)→ ISO 字符串
    assert rs[0]["review_date"].startswith("2025-") or rs[0]["review_date"].startswith("2026-")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_shopee.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand.shopee'`

- [ ] **Step 4: 写最小实现**

创建 `backend/app/ondemand/shopee.py`:

```python
"""虾皮(Shopee)按需采集器。

listing:  GET https://{host}/api/v4/pdp/get_pc?shop_id={s}&item_id={i}
reviews:  GET https://{host}/api/v2/item/get_ratings?shopid={s}&itemid={i}&offset=N&limit=20
URL→id:   单品 URL 形如  .../<slug>-i.<shopid>.<itemid>  或  /product/<shopid>/<itemid>
反爬:     最强,强制住宅代理(proxy_tier=residential)+ 拟人头/限速;失败由 runner 切代理重试。
价格:     Shopee 价格字段放大 100000 倍,解析时除回。
图片:     字段是 hash,需拼 https://cf.{host}/file/<hash>;单测固定用 shopee.com.my。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from curl_cffi import requests as creq

from ..antiban import check_blocked
from .base import BaseOnDemand

_ID_DOT_RE = re.compile(r"-i\.(\d+)\.(\d+)")
_ID_PATH_RE = re.compile(r"/product/(\d+)/(\d+)")
_PRICE_SCALE = 100000
PLATFORM = "shopee"
SITE = f"ondemand_{PLATFORM}"
_IMG_BASE = "https://cf.shopee.com.my/file/"


class ShopeeOnDemand(BaseOnDemand):
    platform = PLATFORM
    proxy_tier = "residential"

    @staticmethod
    def parse_item_id(url: str):
        m = _ID_DOT_RE.search(url) or _ID_PATH_RE.search(url)
        if not m:
            raise ValueError(f"Shopee URL 无 shopid.itemid: {url}")
        return m.group(1), m.group(2)

    @staticmethod
    def _img(hash_or_url: str) -> str:
        if not hash_or_url:
            return ""
        if hash_or_url.startswith("http"):
            return hash_or_url
        return _IMG_BASE + hash_or_url

    @staticmethod
    def parse_listing(data: dict, url: str) -> dict:
        it = data.get("data", {}).get("item", {}) or {}
        shopid, itemid = it.get("shopid"), it.get("itemid")
        imgs = it.get("images") or ([it["image"]] if it.get("image") else [])
        rating = (it.get("item_rating") or {}).get("rating_star")
        return {
            "sku": f"{shopid}_{itemid}",
            "title": it.get("name"),
            "sale_price": (it.get("price") or 0) / _PRICE_SCALE or None,
            "original_price": (it.get("price_before_discount") or it.get("price") or 0)
            / _PRICE_SCALE or None,
            "currency": it.get("currency"),
            "image_urls": [ShopeeOnDemand._img(h) for h in imgs],
            "ratings": rating,
            "inventory": str(it.get("stock")) if it.get("stock") is not None else None,
            "status": "on_sale",
            "product_url": url,
            "site": SITE,
            "brand": PLATFORM,
        }

    @staticmethod
    def parse_reviews(data: dict, item_id, url: str) -> list[dict]:
        if isinstance(item_id, tuple):
            sku = f"{item_id[0]}_{item_id[1]}"
        else:
            sku = str(item_id)
        out = []
        for r in (data.get("data", {}).get("ratings") or []):
            ctime = r.get("ctime")
            rdate = (datetime.fromtimestamp(ctime, tz=timezone.utc).isoformat()
                     if ctime else None)
            out.append({
                "review_id": str(r.get("cmtid")),
                "platform": SITE,
                "site": SITE,
                "reviewer_name": r.get("author_username"),
                "rating": r.get("rating_star"),
                "title": None,
                "content": r.get("comment"),
                "review_date": rdate,
                "sku": sku,
                "product_url": url,
            })
        return out

    # ---- HTTP(smoke 路径)----
    def _session(self, proxy):
        s = creq.Session(impersonate="chrome")
        s.headers.update({"Referer": "https://shopee.com/",
                          "X-Requested-With": "XMLHttpRequest"})
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def _host(self, url: str) -> str:
        return re.sub(r"^https?://", "", url).split("/")[0]

    def fetch_listing(self, item_id, url: str, proxy=None) -> dict:
        shopid, itemid = item_id
        s = self._session(proxy)
        api = (f"https://{self._host(url)}/api/v4/pdp/get_pc"
               f"?shop_id={shopid}&item_id={itemid}")
        resp = s.get(api, timeout=30)
        check_blocked(resp.status_code, "shopee/pdp")
        resp.raise_for_status()
        return self.parse_listing(resp.json(), url)

    def fetch_reviews(self, item_id, url: str, limit: int = 100, proxy=None):
        shopid, itemid = item_id
        s = self._session(proxy)
        out, offset = [], 0
        while len(out) < limit:
            api = (f"https://{self._host(url)}/api/v2/item/get_ratings"
                   f"?shopid={shopid}&itemid={itemid}&offset={offset}&limit=20")
            resp = s.get(api, timeout=30)
            check_blocked(resp.status_code, "shopee/ratings")
            resp.raise_for_status()
            batch = self.parse_reviews(resp.json(), item_id, url)
            if not batch:
                break
            out.extend(batch)
            offset += 20
        return out[:limit]

    def enumerate_listing(self, url: str, max_items: int = 100, proxy=None):
        """店铺/类目页枚举。Shopee 店铺 API:
        GET /api/v4/shop/search_items?shopid=...&limit=...  首版用页面正则兜底。"""
        s = self._session(proxy)
        resp = s.get(url, timeout=30)
        check_blocked(resp.status_code, "shopee/listing")
        resp.raise_for_status()
        ids = []
        for m in _ID_DOT_RE.finditer(resp.text):
            pair = (m.group(1), m.group(2))
            if pair not in ids:
                ids.append(pair)
            if len(ids) >= max_items:
                break
        return ids
```

- [ ] **Step 5: 移除 Task 2 的 skip**

编辑 `backend/tests/test_ondemand_registry.py`,删除 `test_get_crawler_returns_platform_class` 上方的 `@pytest.mark.skip(...)` 行。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_shopee.py tests/test_ondemand_registry.py -v`
Expected: PASS(shopee 3 passed;registry 全 passed,原 skip 用例现 passed)

- [ ] **Step 7: 提交**

```bash
git add backend/app/ondemand/shopee.py backend/tests/test_ondemand_shopee.py backend/tests/test_ondemand_registry.py backend/tests/fixtures/ondemand/shopee_pdp.json backend/tests/fixtures/ondemand/shopee_ratings.json
git commit -m "feat(ondemand): add Shopee crawler and unskip registry test"
```

---

## Task 6: 编排 + 入库(runner.fetch)

**Files:**
- Create: `backend/app/ondemand/runner.py`
- Test: `backend/tests/test_ondemand_runner.py`

设计:`fetch(url)` 用 `detect_platform`/`classify_url` 选平台与入口,product 直接 `[parse_item_id]`,listing 调 `enumerate_listing`;逐 ID 调 `fetch_listing`/`fetch_reviews`,聚合进 `OnDemandResult`,最后入库函数 `persist()` 写 Product/Review 表。为可单测,`fetch()` 接受可选 `crawler` 注入(默认走 registry),并用布尔形参 `do_persist` 控制是否入库(避免与模块函数 `persist` 重名)。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_runner.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Product, Review
from app.ondemand.base import BaseOnDemand, OnDemandResult

pytestmark = pytest.mark.unit


class FakeCrawler(BaseOnDemand):
    platform = "fake"
    proxy_tier = "none"

    @staticmethod
    def parse_item_id(url):
        return "IT1"

    @staticmethod
    def parse_listing(data, url):
        return {"sku": "IT1", "title": "Fake Chair", "site": "ondemand_fake",
                "product_url": url, "sale_price": 10.0}

    @staticmethod
    def parse_reviews(data, item_id, url):
        return [{"review_id": "rv1", "platform": "ondemand_fake",
                 "site": "ondemand_fake", "rating": 5, "content": "ok"}]

    def fetch_listing(self, item_id, url, proxy=None):
        return self.parse_listing({}, url)

    def fetch_reviews(self, item_id, url, limit=100, proxy=None):
        return self.parse_reviews({}, item_id, url)

    def enumerate_listing(self, url, max_items=100, proxy=None):
        return ["IT1", "IT2"]


def test_fetch_single_product_collects_listing_and_reviews():
    from app.ondemand.runner import fetch

    res = fetch("https://x/IT1", crawler=FakeCrawler(), kind="product",
                do_persist=False)
    assert isinstance(res, OnDemandResult)
    assert len(res.listings) == 1
    assert res.listings[0]["sku"] == "IT1"
    assert len(res.reviews) == 1


def test_fetch_listing_enumerates_multiple(monkeypatch):
    from app.ondemand.runner import fetch

    # enumerate 返回 2 个 id,但 parse_item_id 固定 IT1 → 两条 listing 同 sku
    res = fetch("https://x/shop", crawler=FakeCrawler(), kind="listing",
                max_items=2, do_persist=False)
    assert len(res.listings) == 2


def test_persist_writes_product_and_review():
    from app.ondemand import runner

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    res = OnDemandResult()
    res.add_listing({"sku": "IT1", "title": "Fake Chair", "site": "ondemand_fake",
                     "product_url": "https://x/IT1", "sale_price": 10.0})
    res.add_reviews([{"review_id": "rv1", "platform": "ondemand_fake",
                      "site": "ondemand_fake", "rating": 5, "content": "ok"}])

    sess = TestSession()
    stats = runner.persist(res, session=sess)
    sess.commit()

    assert sess.query(Product).filter_by(sku="IT1").count() == 1
    assert sess.query(Review).filter_by(review_id="rv1").count() == 1
    assert stats["listings"]["inserted"] == 1
    assert stats["reviews"]["inserted"] == 1
    sess.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ondemand.runner'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/ondemand/runner.py`:

```python
"""按需抓取编排 —— fetch(url) → 抓 listing + 评论 → 入库。

listing 入 Product 表(经 pipeline.upsert_products);评论入 Review 表
(去重逻辑对齐 review_runner._upsert_reviews)。被封时切代理重试。
"""
from __future__ import annotations

from datetime import datetime

from ..antiban import BlockedError
from ..db import session_scope
from ..models import Review
from ..pipeline import clean_text, parse_dt, upsert_products
from ..proxy import get_proxy
from .base import OnDemandResult
from .registry import classify_url, detect_platform, get_crawler

_MAX_RETRY = 3


def fetch(url: str, *, max_items: int = 100, review_limit: int = 100,
          crawler=None, kind: str | None = None,
          do_persist: bool = True) -> OnDemandResult:
    """抓取一条 URL(单品或列表页)的 listing + 评论。

    crawler/kind 仅供测试注入;生产调用只传 url。
    do_persist=False 时只抓不入库(单测用)。
    """
    res = OnDemandResult()
    platform = getattr(crawler, "platform", None) or detect_platform(url)
    if platform is None:
        res.note(f"无法识别平台: {url}")
        return res
    if crawler is None:
        crawler = get_crawler(platform)
    kind = kind or classify_url(url)

    # ---- 收集待抓 itemId ----
    try:
        if kind == "product":
            item_ids = [crawler.parse_item_id(url)]
        else:
            proxy = get_proxy(crawler.proxy_tier)
            item_ids = crawler.enumerate_listing(url, max_items=max_items,
                                                 proxy=proxy)
            if len(item_ids) >= max_items:
                res.note(f"列表枚举达上限 {max_items},可能有截断")
    except Exception as exc:
        res.note(f"解析/枚举失败: {exc}")
        return res

    # ---- 逐 ID 抓 listing + 评论 ----
    for iid in item_ids:
        _fetch_one(crawler, iid, url, review_limit, res)

    if do_persist:
        with session_scope() as s:
            persist(res, session=s)
    return res


def _fetch_one(crawler, iid, url, review_limit, res: OnDemandResult) -> None:
    last_err = None
    for attempt in range(_MAX_RETRY):
        proxy = get_proxy(crawler.proxy_tier)
        try:
            res.add_listing(crawler.fetch_listing(iid, url, proxy=proxy))
            res.add_reviews(crawler.fetch_reviews(iid, url, limit=review_limit,
                                                  proxy=proxy))
            return
        except BlockedError as exc:
            last_err = exc                      # 切代理重试
            continue
        except Exception as exc:
            res.note(f"{iid}: {exc}")           # 失败隔离,不重试
            return
    res.note(f"{iid}: 多次被封放弃({last_err})")


def persist(res: OnDemandResult, *, session) -> dict:
    """listing → Product upsert;评论 → Review upsert。返回统计。"""
    by_site: dict[str, list[dict]] = {}
    for p in res.listings:
        by_site.setdefault(p["site"], []).append(p)
    listing_stats = {"inserted": 0, "updated": 0, "skipped": 0}
    for site, items in by_site.items():
        st = upsert_products(session, site, items)
        for k in listing_stats:
            listing_stats[k] += st.get(k, 0)

    review_stats = _upsert_reviews(res.reviews, session)
    return {"listings": listing_stats, "reviews": review_stats}


def _upsert_reviews(reviews: list[dict], session) -> dict:
    stats = {"inserted": 0, "updated": 0}
    for r in reviews:
        rid, plat = r.get("review_id"), r.get("platform")
        if not rid:
            continue
        row = (session.query(Review)
               .filter(Review.platform == plat, Review.review_id == rid)
               .first())
        payload = dict(
            review_id=rid, platform=plat, site=r.get("site"),
            reviewer_name=clean_text(r.get("reviewer_name")),
            rating=_int(r.get("rating")),
            title=clean_text(r.get("title")),
            content=clean_text(r.get("content")),
            review_date=parse_dt(r.get("review_date")),
            sku=r.get("sku"), product_url=r.get("product_url"),
        )
        if row is None:
            session.add(Review(collected_time=datetime.utcnow(), **payload))
            stats["inserted"] += 1
        else:
            for k, v in payload.items():
                setattr(row, k, v)
            stats["updated"] += 1
    return stats


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_runner.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 在 `__init__.py` 导出 fetch**

编辑 `backend/app/ondemand/__init__.py`,改为:

```python
from .base import OnDemandResult
from .runner import fetch

__all__ = ["OnDemandResult", "fetch"]
```

- [ ] **Step 6: 跑全部 ondemand 测试**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_*.py -v`
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/ondemand/runner.py backend/app/ondemand/__init__.py backend/tests/test_ondemand_runner.py
git commit -m "feat(ondemand): add fetch orchestration and persistence"
```

---

## Task 7: CLI 子命令 `fetch-url`

**Files:**
- Modify: `backend/app/cli.py`(`main` 内新增子命令解析 + 分发)
- Test: `backend/tests/test_ondemand_cli.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_cli.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_cli_fetch_url_invokes_runner(monkeypatch, capsys):
    import app.cli as cli
    from app.ondemand.base import OnDemandResult

    called = {}

    def fake_fetch(url, *, max_items, review_limit, do_persist=True):
        called["url"] = url
        called["max_items"] = max_items
        called["review_limit"] = review_limit
        r = OnDemandResult()
        r.add_listing({"sku": "X", "title": "t", "site": "ondemand_shopee",
                       "product_url": url})
        r.add_reviews([{"review_id": "rv"}])
        r.note("ok")
        return r

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr("app.ondemand.fetch", fake_fetch, raising=False)
    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)

    rc = cli.main(["fetch-url", "--url", "https://shopee.com.my/x-i.1.2",
                   "--max-items", "5", "--review-limit", "30"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called["url"].endswith("i.1.2")
    assert called["max_items"] == 5
    assert called["review_limit"] == 30
    assert "listing" in out.lower() or "1" in out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_cli.py -v`
Expected: FAIL — argparse 报未知命令 `fetch-url`(SystemExit)

- [ ] **Step 3: 写实现**

在 `backend/app/cli.py` 的 `main()` 中,`ps = sub.add_parser("shopping", ...)` 块之后、`args = parser.parse_args(argv)` 之前,新增:

```python
    pf = sub.add_parser("fetch-url", help="按需抓取:指定 URL → listing + VOC")
    pf.add_argument("--url", required=True, help="商品页或店铺/类目页 URL")
    pf.add_argument("--max-items", type=int, default=100,
                    help="列表页枚举商品数上限")
    pf.add_argument("--review-limit", type=int, default=100,
                    help="每商品评论抓取上限")
```

在 `main()` 的命令分发区(其它 `if args.cmd == ...` 之间)新增:

```python
    if args.cmd == "fetch-url":
        from . import ondemand
        res = ondemand.fetch(args.url, max_items=args.max_items,
                             review_limit=args.review_limit)
        print(f"✓ 抓取 {args.url}")
        print(f"    listing {len(res.listings)} / 评论 {len(res.reviews)}")
        for n in res.notes:
            print(f"    {n}")
        return 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_cli.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/cli.py backend/tests/test_ondemand_cli.py
git commit -m "feat(ondemand): add fetch-url CLI command"
```

---

## Task 8: MCP 工具 `fetch_listing_voc`

**Files:**
- Modify: `backend/app/mcp_server.py`(末尾新增工具,仿 `fetch_amazon_reviews`)
- Test: `backend/tests/test_ondemand_mcp.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_mcp.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_fetch_listing_voc_tool_exists_and_shapes_output(monkeypatch):
    import app.mcp_server as mcp_server
    from app.ondemand.base import OnDemandResult

    def fake_fetch(url, *, max_items, review_limit):
        r = OnDemandResult()
        r.add_listing({"sku": "111_222", "title": "Mouse",
                       "site": "ondemand_shopee", "product_url": url,
                       "sale_price": 15.99})
        r.add_reviews([{"review_id": "555", "rating": 5, "content": "ok"}])
        r.note("done")
        return r

    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)

    # 取被 metered_tool 包装前的原函数:用 .__wrapped__ 或直接调注册名
    fn = getattr(mcp_server, "fetch_listing_voc")
    # FastMCP 包装后仍可调用 .fn 或直接调用;此处直接调用底层
    result = mcp_server._call_fetch_listing_voc("https://shopee.com.my/x-i.111.222")
    assert result["listings"][0]["sku"] == "111_222"
    assert result["reviews_count"] == 1
    assert "notes" in result
```

> 说明:FastMCP 的 `mcp.tool` 装饰后直接调用不便,故实现里把核心逻辑放在模块级 `_call_fetch_listing_voc(url, max_items, review_limit)`,MCP 工具仅做一层转调。测试覆盖核心逻辑。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_mcp.py -v`
Expected: FAIL — `AttributeError: module 'app.mcp_server' has no attribute '_call_fetch_listing_voc'`

- [ ] **Step 3: 写实现**

在 `backend/app/mcp_server.py` 末尾(其它 `@metered_tool` 工具之后)新增:

```python
def _call_fetch_listing_voc(url: str, max_items: int = 100,
                            review_limit: int = 100) -> dict:
    """按需抓取核心逻辑(供 MCP 工具与测试共用)。"""
    from . import ondemand

    res = ondemand.fetch(url, max_items=max_items, review_limit=review_limit)
    return {
        "url": url,
        "listings": res.listings,
        "listings_count": len(res.listings),
        "reviews": res.reviews,
        "reviews_count": len(res.reviews),
        "notes": res.notes,
    }


@metered_tool(required_scope="crawler:scrape")
def fetch_listing_voc(url: str, max_items: int = 100,
                      review_limit: int = 100) -> dict:
    """[ADVANCED] 指定 URL 抓取 listing + VOC(评论原文)。

    支持 美客多(MercadoLibre)/ Lazada / 虾皮(Shopee)。
    url 可为单商品页(精抓一条)或店铺/类目/搜索页(枚举批量抓)。
    max_items: 列表页枚举上限;review_limit: 每商品评论上限。
    数据同时落 Product/Review 表,可在控制台看板查看。"""
    return _call_fetch_listing_voc(url, max_items=max_items,
                                   review_limit=review_limit)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_mcp.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/mcp_server.py backend/tests/test_ondemand_mcp.py
git commit -m "feat(ondemand): expose fetch_listing_voc MCP tool"
```

---

## Task 9: Web API 端点 `POST /api/ondemand/fetch`

**Files:**
- Modify: `backend/app/api/routes.py`(在 `router`(需登录)上新增端点)
- Test: `backend/tests/test_ondemand_api.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_api.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_ondemand_fetch_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.routes as routes
    from app.main import app
    from app.ondemand.base import OnDemandResult

    def fake_fetch(url, *, max_items, review_limit):
        r = OnDemandResult()
        r.add_listing({"sku": "X", "title": "t", "site": "ondemand_lazada",
                       "product_url": url, "sale_price": 9.9})
        r.add_reviews([{"review_id": "rv", "rating": 4, "content": "ok"}])
        r.note("done")
        return r

    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)
    # 绕过登录依赖
    app.dependency_overrides[routes.require_user] = lambda: "tester"

    client = TestClient(app)
    resp = client.post("/api/ondemand/fetch",
                       json={"url": "https://www.lazada.com.my/products/x-i1-s2.html"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["listings_count"] == 1
    assert body["reviews_count"] == 1
    assert body["listings"][0]["sku"] == "X"
    app.dependency_overrides.clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_api.py -v`
Expected: FAIL — 404(端点不存在)

- [ ] **Step 3: 写实现**

在 `backend/app/api/routes.py` 中,`@router.get("/products")` 端点附近(同属需登录 `router`)新增:

```python
@router.post("/ondemand/fetch")
def ondemand_fetch(payload: dict, user: str = Depends(require_user)):
    """按需抓取:指定 URL → listing + VOC。

    payload: {"url": "...", "max_items"?: int, "review_limit"?: int}
    """
    from .. import ondemand

    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 必填")
    max_items = int(payload.get("max_items", 100))
    review_limit = int(payload.get("review_limit", 100))
    res = ondemand.fetch(url, max_items=max_items, review_limit=review_limit)
    return {
        "url": url,
        "listings": res.listings,
        "listings_count": len(res.listings),
        "reviews": res.reviews,
        "reviews_count": len(res.reviews),
        "notes": res.notes,
    }
```

> 确认文件顶部已 import `HTTPException`(routes.py 已有,无需新增)。若缺失则在 fastapi import 行补上。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_ondemand_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_ondemand_api.py
git commit -m "feat(ondemand): add POST /api/ondemand/fetch endpoint"
```

---

## Task 10: Web 控制台「指定 URL 抓取」UI

**Files:**
- Modify: `frontend/index.html`(新增一个面板:输入框 + 抓取按钮 + 结果展示)

UI 为前端改动,无单元测试;通过手动验证 + smoke 验收。沿用现有 `api()`/`authH()` fetch 模式(见 `frontend/index.html:631-700`)。

- [ ] **Step 1: 定位插入点**

Run: `cd /Users/wangxiaokang/Documents/github/smart-crawler && grep -nE "influencers/full|infPlatform|红人|influencer" frontend/index.html | head`
找到红人(influencer)面板的 HTML 区块与其 `<script>` 内的调用函数,作为新面板的模板参考(它同样是「输入 → 调 API → 展示结果」的交互)。

- [ ] **Step 2: 加入 HTML 面板**

在红人面板相邻处(同一 tab 容器内)插入(Vue3 模板语法,沿用现有 `v-model`/`@click`/`v-for` 风格;类名复用页面已有样式):

```html
<div class="card" style="margin-top:16px">
  <h3>指定 URL 抓取(美客多 / Lazada / 虾皮)</h3>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <input v-model="odUrl" placeholder="粘贴商品页或店铺/类目页 URL"
           style="flex:1;min-width:320px" />
    <input v-model.number="odMaxItems" type="number" title="列表枚举上限"
           style="width:90px" />
    <button @click="runOndemand" :disabled="odLoading">
      {{ odLoading ? '抓取中…' : '抓取' }}
    </button>
  </div>
  <div v-if="odError" style="color:#c00;margin-top:8px">{{ odError }}</div>
  <div v-if="odResult" style="margin-top:12px">
    <div>listing {{ odResult.listings_count }} / 评论 {{ odResult.reviews_count }}</div>
    <ul><li v-for="n in odResult.notes" :key="n">{{ n }}</li></ul>
    <table v-if="odResult.listings.length" style="width:100%;margin-top:8px">
      <thead><tr><th>SKU</th><th>标题</th><th>售价</th><th>原价</th></tr></thead>
      <tbody>
        <tr v-for="p in odResult.listings" :key="p.sku">
          <td>{{ p.sku }}</td><td>{{ p.title }}</td>
          <td>{{ p.sale_price }}</td><td>{{ p.original_price }}</td>
        </tr>
      </tbody>
    </table>
    <table v-if="odResult.reviews.length" style="width:100%;margin-top:8px">
      <thead><tr><th>评分</th><th>评论</th></tr></thead>
      <tbody>
        <tr v-for="r in odResult.reviews" :key="r.review_id">
          <td>{{ r.rating }}</td><td>{{ r.content }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
```

- [ ] **Step 3: 加入响应式状态**

在 Vue `setup()`(或 data)中,红人相关 `ref` 附近新增:

```javascript
      const odUrl = ref('');
      const odMaxItems = ref(20);
      const odLoading = ref(false);
      const odError = ref('');
      const odResult = ref(null);

      async function runOndemand() {
        odError.value = ''; odResult.value = null; odLoading.value = true;
        try {
          const r = await fetch('/api/ondemand/fetch', {
            method: 'POST',
            headers: { ...authH(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: odUrl.value, max_items: odMaxItems.value }),
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          odResult.value = await r.json();
        } catch (e) {
          odError.value = '抓取失败:' + e.message;
        } finally {
          odLoading.value = false;
        }
      }
```

- [ ] **Step 4: 暴露到模板 return**

在 `setup()` 的 `return { ... }` 中追加:`odUrl, odMaxItems, odLoading, odError, odResult, runOndemand`。

> 确认 `authH` 函数已存在(`frontend/index.html` 已定义,返回带 Bearer 的 headers)。

- [ ] **Step 5: 手动验证(无自动化测试)**

Run(启动服务): `cd /Users/wangxiaokang/Documents/github/smart-crawler && ./run.sh`
打开 `http://localhost:8077`,登录后定位「指定 URL 抓取」面板,粘贴一条美客多商品 URL → 点抓取 → 应显示 listing 行 + 评论行(或 notes 提示)。

Expected: 面板渲染正常,点击后有结果或明确错误提示(不白屏、不报 JS 错)。

- [ ] **Step 6: 提交**

```bash
git add frontend/index.html
git commit -m "feat(ondemand): add specified-URL fetch panel to console"
```

---

## Task 11: 端到端 smoke 测试(真实网络,可跳过)

**Files:**
- Create: `backend/tests/test_ondemand_smoke.py`(标记 `@pytest.mark.smoke`)

smoke 测试命中真实平台,默认不在 CI 跑(`pytest -m "not smoke"`);用于上线前人工验证三平台连通性。三平台各覆盖一条真实单品 URL。

- [ ] **Step 1: 写 smoke 测试**

创建 `backend/tests/test_ondemand_smoke.py`:

```python
"""真实网络 smoke —— 上线前手动跑:pytest -m smoke tests/test_ondemand_smoke.py

需要可用网络(Shopee/Lazada 可能需 RESIDENTIAL_PROXY)。任一平台失败不代表代码错误,
可能是平台风控/接口变更,据 notes 排查。
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.smoke

# 上线时替换为真实有效的单品 URL
_URLS = {
    "mercadolibre": os.environ.get("SMOKE_ML_URL", ""),
    "lazada": os.environ.get("SMOKE_LAZADA_URL", ""),
    "shopee": os.environ.get("SMOKE_SHOPEE_URL", ""),
}


@pytest.mark.parametrize("platform", list(_URLS))
def test_fetch_real_product(platform):
    url = _URLS[platform]
    if not url:
        pytest.skip(f"未设置 SMOKE_{platform.upper()}_URL")
    from app import ondemand

    res = ondemand.fetch(url, max_items=5, review_limit=10, )
    # 至少抓到 listing,或给出明确 notes
    assert res.listings or res.notes, f"{platform}: 无 listing 且无 notes"
    if res.listings:
        p = res.listings[0]
        for k in ("sku", "title", "product_url", "site"):
            assert p.get(k), f"{platform}: listing 缺字段 {k}"
```

> 注:`ondemand.fetch` 此处会触发真实入库(`do_persist=True`)。smoke 跑在开发库即可。

- [ ] **Step 2: 跑非 smoke 全量回归确认不破坏**

Run: `cd backend && ../.venv/bin/python -m pytest -m "not smoke" -q`
Expected: 全 PASS(含既有测试 + 新增 ondemand 单测)

- [ ] **Step 3: (可选,有网络时)跑 smoke**

Run: `cd backend && SMOKE_ML_URL="<真实美客多商品URL>" ../.venv/bin/python -m pytest -m smoke tests/test_ondemand_smoke.py -v`
Expected: 美客多 PASS;Lazada/Shopee 未设 URL 则 SKIP

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_ondemand_smoke.py
git commit -m "test(ondemand): add real-network smoke tests"
```

---

## Task 12: 文档与收尾

**Files:**
- Modify: `README.md`(在「快速开始」CLI 示例区补 `fetch-url` 用法)

- [ ] **Step 1: README 补用法**

在 `README.md` 的 CLI 示例块(`export` 行附近)追加:

```bash
# 按需抓取:指定 URL → listing + 评论(美客多 / Lazada / 虾皮)
../.venv/bin/python -m app.cli fetch-url --url "https://articulo.mercadolibre.com.mx/MLM-123456789-..."
../.venv/bin/python -m app.cli fetch-url --url "https://www.lazada.com.my/shop/xxx/" --max-items 50
```

并在「MCP 接入」工具列表处补一行:`fetch_listing_voc(url)` —— 指定 URL 抓 listing+VOC。

- [ ] **Step 2: 全量回归**

Run: `cd backend && ../.venv/bin/python -m pytest -m "not smoke" -q`
Expected: 全 PASS

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs(ondemand): document fetch-url CLI and fetch_listing_voc MCP tool"
```

---

## 自检对照(spec coverage)

| spec 要求 | 对应任务 |
|-----------|----------|
| 三平台 listing 抓取 | Task 3/4/5(parse_listing + fetch_listing) |
| 三平台 VOC 评论原文抓取 | Task 3/4/5(parse_reviews + fetch_reviews) |
| 单品 URL 精抓 | Task 6(kind=product) |
| 列表页枚举批量抓 | Task 3/4/5 enumerate_listing + Task 6(kind=listing) |
| API 直连优先 | 全部采集器走 curl_cffi JSON;Playwright 兜底未在 MVP 实装(见下「已知边界」) |
| 评论只入原文不做 NLP | Task 6 `_upsert_reviews` 不写 sentiment/nlp 字段 |
| listing→Product / 评论→Review 复用现有表 | Task 6 persist |
| 幂等入库 | Task 6(upsert_products 按 site+sku;评论按 platform+review_id) |
| 风控:代理分层 + 熔断 + 失败隔离 + 截断告知 | Task 6 `_fetch_one`(切代理重试 + notes)/ enumerate 上限 note |
| 默认 max_items=100 / review_limit=100 | Task 6/7 默认值 |
| 代理档:ML=none / Lazada,Shopee=residential | Task 3/4/5 `proxy_tier` |
| CLI 入口 | Task 7 |
| MCP 入口 | Task 8 |
| Web 入口 | Task 9 + Task 10 |
| 验收标准 1-5 | Task 11 smoke(1-2)+ 各单测(3-5) |

## 已知边界(MVP 范围内的诚实说明)

- **Playwright 兜底未实装**:本计划 MVP 全走 curl_cffi 直连。spec 写明「Playwright 仅作兜底」,真到 Shopee/Lazada 直连被风控彻底挡住时,需新增一个 Playwright 抓取分支(可作为后续任务)。Task 6 的切代理重试是第一道兜底;Playwright 是第二道,暂留接口位(`fetch_listing` 可在子类内部判失败后转 Playwright),不在本 MVP 实现。
- **enumerate_listing 首版用页面正则兜底**:三平台店铺/类目页的官方搜索 API(美客多 `/sites/{id}/search`、Lazada 类目 API、Shopee `/shop/search_items`)各有签名/分页细节,首版先用「抓页面 HTML + 正则抠 ID」保证可用,后续可按平台升级为官方 API 分页(更稳更全)。此边界已在各 `enumerate_listing` docstring 标注。
- **Shopee 接口签名风险**:`get_pc`/`get_ratings` 近年可能加 `af-ac-enc-dat` 等签名头,直连失败时落到代理重试;若仍失败,据 `notes` 与 `snapshot` 排查,可能需补签名或转 Playwright。
