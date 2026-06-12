# C 档:验收报告 crawler 根因修复 — 设计文档

日期：2026-06-12
分支：`feat/acceptance-c-crawler-rootcause`（待建）
状态：设计已批准，待写实现计划

## 背景

`标杆平台验收报告.xlsx` 的"爬取数据"表 16/34 站偏差 >50%，结构性根因集中在 crawler 采集 + 入库语义层（即历史归档的 "C 档"）。A/B/D 三档已于 2026-06-11 完成，C 档此前因"需 NAS 真机 + 住宅代理验证"被推迟。

本轮经核实，三条根因可在**纯本地**用单元测试 + 端到端测试锁住行为后修复，无需真机重爬、无模型变更、无数据迁移。"反爬环境下的实际采集量"仍需真机验证，不在本轮范围。

## 根因现状（2026-06-12 对源码核实）

1. **`max_products` 被忽略** — 22 个 crawler 在 `__init__` 里硬编码 `self.limit = ... else DEFAULT_LIMIT`，忽略 `sites.yaml` 的 per-site `max_products`。仅 magento / shoper / generic 读 `hints.get("max_products")`。→ 欠采/过采主因之一。

2. **促销永不触发** — `pipeline.py:71-72` 的 `normalize()` 把缺失的 `original_price` 回填为 `sale_price`；而 `runner.py:_detect_promotions` 靠 `Product.original_price > Product.sale_price` 过滤，回填后两者永远相等，促销表恒空。

3. **变体计数（SPU）** — Product 模型已有 `spu` 列（带索引），Shopify `_expand` 每变体行已写 `spu` 并入库。问题只剩 `api/routes.py:625` 出于性能把 `spu_count = sku_count` 兜底，报表把变体当独立 SKU 多算。**数据已具备，仅报表展示偷懒。**

## 设计目标

- per-site `max_products` 在所有 crawler 生效。
- 促销检测能真触发，且不误报（缺原价的商品不算促销）。
- 站点报表同时给出 `sku_count`（变体行数）与 `spu_count`（去重款数）。
- 全程无模型变更、无迁移、无重爬。
- 每条根因有单元测试 + 一次端到端测试锁住行为，全量 pytest 绿。

## 改动面

```
backend/app/
├── crawlers/base.py        ← 新增 _resolve_limit() 统一入口（读 sites.yaml hints）
├── crawlers/*.py (×22)     ← self.limit 改走 _resolve_limit(DEFAULT_LIMIT)
├── pipeline.py:71-72       ← 删除 original_price 回填两行
└── api/routes.py:622-627   ← spu_count 改真算 count(distinct spu) group by site
```

## 详细设计

### 根因 1：max_products 统一入口

在 `BaseCrawler` 增加 helper（读一次 `get_sites()`，缓存到实例）：

```python
def _resolve_limit(self, default: int, explicit: int | None = None) -> int:
    """limit 优先级：显式参数 > sites.yaml max_products > env 默认。"""
    if explicit is not None:
        return explicit
    hints = next((c for c in get_sites() if c["site"] == self.site.site), {})
    return int(hints.get("max_products", default))
```

各 crawler 把：
```python
self.limit = limit if limit is not None else DEFAULT_LIMIT
# 或 self.limit = DEFAULT_LIMIT
```
改为：
```python
self.limit = self._resolve_limit(DEFAULT_LIMIT, limit)   # 有 limit 参数的
self.limit = self._resolve_limit(DEFAULT_LIMIT)          # 无 limit 参数的
```

特殊情形：
- otto/allegro/idealo 同时有 `scan_cap`（SCAN_CAP 常量），本轮**只统一 limit**，scan_cap 保持现状（不在根因范围）。
- vidaxl 用 `STOREFRONT_LIMIT`，按同样模式接 `_resolve_limit(STOREFRONT_LIMIT)`。
- magento/shoper/generic 已读 hints，改为复用 `_resolve_limit` 以统一（行为等价）。

需改 crawler（22 个）：aliexpress, allegro, article, bestbuy, bol, cdiscount, cratebarrel, ebay, etsy, flexispot, homary, houzz, idealo, ikea, otto, overstock, target, vidaxl, vonhaus, walmart, wayfair, westelm。
统一收编：magento, shoper, generic。

### 根因 2：删除促销回填

`pipeline.py` `normalize()` 删除：
```python
if p.get("original_price") is None:
    p["original_price"] = p.get("sale_price")
```

下游 NULL 安全性已核实：
- `_detect_promotions` 用 SQL `original_price > sale_price`（SQL 中 `NULL > x` 为 false，自动排除无原价行），discount 计算有 `if p.original_price` 守卫。
- `export.py` 趋势/价格曲线的 discount 计算已有 `if p.sale_price and p.original_price and p.original_price > 0` 守卫。
- `pipeline._has_changed` 对 original_price 有 `is not None` 守卫。

无需额外改动。

### 根因 3：spu_count 真算

`api/routes.py` 站点报表（`site_overview` 一带）把：
```python
spu_counts = sku_counts
```
改为：
```python
spu_counts = dict(db.query(Product.site, func.count(distinct(Product.spu)))
                    .filter(Product.spu.isnot(None))
                    .group_by(Product.site).all())
```
`spu` 已有索引；仅在该汇总端点算一次。`spu` 为空的行（非 Shopify 多数 crawler 一品一行、未写 spu）不计入 distinct，此时报表 `spu_count` 会小于 `sku_count`——对这些站二者本应接近，差额即真实的"无 spu 行"，可接受；如需可后续让其余 crawler 补 spu（不在本轮）。

> 注：`sku_count` 仍按 `count(Product.id)` 给出变体行数，两个数并列展示，由前端/报表呈现。

## 测试策略

### 根因 1 — `backend/tests/test_crawler_limit.py`（新建）
- monkeypatch `app.crawlers.base.get_sites`（或对应模块的 `get_sites`）返回 `[{"site": "x", "max_products": 5}]`，对 overstock/bol/idealo/cratebarrel 各构造 crawler，断言 `crawler.limit == 5`。
- 优先级：显式 `limit=3` 参数 > hints(5) > env 默认，断言取 3。
- 无 hints 时回落 DEFAULT_LIMIT。

### 根因 2 — `backend/tests/test_pipeline.py`（补用例）
- `test_normalize_keeps_original_none_when_missing`：`normalize({...sale_price:10})` → `original_price is None`。
- `test_normalize_preserves_real_original`：有 original 原样保留。
- 端到端：入库两商品（A: original 20 > sale 10；B: 仅 sale 10），跑 `_detect_promotions`，断言生成 **1** 条 Promotion（A），B 不误报。

### 根因 3 — `backend/tests/test_site_overview_count.py`（新建，或并入 tenancy 测试）
- 造同 site 3 个 SKU、其中 2 个共享同一 `spu`，调 `site_overview`，断言该站 `sku_count == 3` 且 `spu_count == 2`。

### 回归
全量 `pytest`（基线 164–180 绿）确认无回归。

## 非目标（本轮不做）

- NAS 真机重爬 / 住宅代理验证实际采集量。
- 非 Shopify crawler 补写 `spu`（多数本就一品一行）。
- scan_cap 统一、价格 PDP 增强、flexispot 反爬、is_new 写路径——属采集质量，需真机，另案。

## 风险

- 改 22 个 crawler 的 `self.limit` 赋值——机械改动，靠 `_resolve_limit` 单测 + 全量 pytest 兜底，逐个文件确认无遗漏副作用。
- 删回填改变入库语义：理论上历史依赖"original 必非空"的代码会受影响，已逐处核实下游均 NULL 安全。
