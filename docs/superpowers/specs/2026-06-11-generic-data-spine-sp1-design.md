# SP1 通用数据脊柱（Generic Data Spine）设计

- 日期：2026-06-11
- 状态：设计已审，待写实现计划
- 范围：把 smart-crawler 从"电商竞品适配器集合"升级为"面向 AI Agent/开发者的通用网页数据采集平台"的**第一个子项目（SP1）**

## Context（为什么做）

smart-crawler 现状底子不错（多租户/API Key/用量计量/反爬/代理/snapshot/scrape_url/MCP 全有），但数据层强耦合电商：所有产出直接进 `Product`/`Review` 专用表，无通用"任意 schema"存储、无完整 provenance、无新鲜度 TTL（6 个月前的数据也当 warehouse 命中返回）、snapshot 写磁盘但与 DB 零关联。

平台 vision 拆成 4 个子项目（SP1 数据脊柱 / SP2 少爬引擎 / SP3 诊断配方 / SP4 强抓质量门），本 spec 只覆盖 **SP1**——其余三个的地基。

**两条铁律**（用户强调）：
1. **不破坏现有**：Aosom/家居电商能力继续稳定，旧 API/MCP/看板/表/采集器一行不动。现有能力沉淀为 `ecommerce_competitor` 行业模板。
2. **减少重复爬取**：爬一次多场景复用，默认 warehouse-first，仓库没有或过期才 live。

**为什么纯增量安全**（探查已确认）：`db.py::_migrate()` 是幂等 `ALTER TABLE ADD COLUMN` + `create_all`，新表/新列自动建、永不删改，SQLite/PG 兼容。新 MCP 工具注册进现有 `mcp_server.py` 不影响旧工具。`scrape_url`（`agent_crawler.py`）已接受任意 URL + `force_live` + 算 confidence，是干净的扩展点。

## 复用的现有件（不重造）

- `agent_crawler.py::scrape_url(db, url, force_live, mode)` —— live/advanced 抓取 + JSON-LD 提取 + confidence(1.0/0.9/0.45/0.25) + scrape_id + extract_metadata（含 `<link rel=canonical>` 解析）。
- `snapshot.py` —— 写磁盘 `data/snapshots/{site}/{date}/{name}.gz`。
- `mcp_context.py::metered_tool` —— scope 校验 + 5min agent cache + 计量 + force_live 旁路。
- `api/v2.py` —— 鉴权/限速/响应封套（success/usage/warnings）。
- `_migrate()` 幂等迁移。

## §1 数据模型（3 张新表，纯增量）

全部走 `_migrate()`。Provenance 字段（source_url/canonical/fetched_at/extraction_method/recipe_id/snapshot_id/confidence/freshness/content_hash）分布在三层对应的三张表。

### `raw_snapshots`（Raw 层：元数据进表，正文留磁盘）
```
id (PK)
url (Text, index)
canonical_url (Text, index)
content_hash (String, index)        # sha256(正文)
fetched_at (DateTime, index)
status_code (Integer)
etag (String)                       # HTTP 头，SP2 增量用
last_modified (String)              # HTTP 头，SP2 增量用
content_type (String)
body_path (String)                  # 指向现有 data/snapshots/*.gz
fetch_mode (String)                 # live / advanced
workspace_id (Integer, ForeignKey workspaces.id, index)
created_at (DateTime, default utcnow)
```
复用 `snapshot.py` 写磁盘，仅补一条 DB 行 + 算 content_hash。

### `extracted_records`（Normalized 层：任意 schema）
```
id (PK)
dataset_id (Integer, ForeignKey datasets.id, index)
snapshot_id (Integer, ForeignKey raw_snapshots.id, nullable)
source_url (Text, index)
canonical_url (Text, index)
entity_type (String, index)         # product/review/article/company/job/creator/generic
data (JSON)                         # 任意 schema 结构化结果
record_key (String, index)          # 去重键（默认 canonical_url）
content_hash (String)               # sha256(规整后的 data)，SP2 跳过未变更
confidence (Float)
extraction_method (String)          # jsonld / heuristic / recipe / schema_projection
recipe_id (Integer, nullable)       # SP3 用，现可空
quality_status (String, index)      # main / staging / quarantine
fetched_at (DateTime, index)
extracted_at (DateTime, default utcnow)
workspace_id (Integer, ForeignKey workspaces.id, index)
```
唯一约束 `UniqueConstraint(dataset_id, record_key)` → 同源 upsert 去重。

### `datasets`（View 层入口：命名数据集）
```
id (PK)
name (String, index)
slug (String, unique, index)
entity_type (String)                # 默认实体类型
description (Text)
source_kind (String)                # custom_url / ecommerce_template / ...
freshness_ttl_sec (Integer, default 86400)   # 数据集默认新鲜度窗口
workspace_id (Integer, ForeignKey workspaces.id, index)
created_by (String)
created_at (DateTime, default utcnow)
```
唯一约束 `UniqueConstraint(workspace_id, slug)`。

## §2 落库流程（save_policy + 质量门）

新建 `backend/app/spine.py`（不改 scrape_url 抓取核心）。核心函数 `ingest_extraction(db, scrape_result, dataset, save_policy, workspace_id) -> dict`：

1. **写 raw_snapshots**：正文已在磁盘（snapshot.py），补 DB 行 + content_hash(正文) + 抓到的 etag/last_modified（从 scrape 响应头，需 scrape_url 透传，见下）。
2. **组 extracted_records 行**：`data`=scrape_result 的提取结果，`snapshot_id`、`source_url`、`canonical_url`、`confidence`（现有计算值落库）、`extraction_method`、`record_key`=canonical、`content_hash`=sha256(规整 data)。
3. **质量门 `_quality_check(record, save_policy)`** 决定 `quality_status`：

| save_policy | 行为 |
|---|---|
| `promote_if_valid`（默认） | confidence ≥ 0.6 且实体必填字段齐 → `main`；否则 → `staging`（记 `missing_fields`） |
| `staging` | 一律 `staging` |
| `main` | 强制 `main`（信任源，跳质量门） |
| `quarantine` | 一律 `quarantine`（疑似被反爬污染） |

附加规则：scrape_result 带 block/challenge 警告 → 强制 `quarantine`（覆盖 policy）。

4. **upsert by `(dataset_id, record_key)`**：同 URL 重抓更新同一行不新增。**content_hash 未变 → 只刷新 fetched_at，不重写 data**（SP2 少爬钩子）。quality_status 可 main↔staging 流转，不直接丢旧值。

**scrape_url 的小改动**（向后兼容）：现有返回里补 `response_headers`（etag/last_modified/content_type）+ 把 `snapshot.py` 写盘返回的 `body_path` 透出。默认行为不变，旧调用方忽略新字段即可。

**实体必填字段表**（`_REQUIRED_FIELDS`）：`product→{title}`、`review→{content}`、`article→{title}`、`generic→{}`（无强制）。可扩展。

返回结构：
```json
{
  "scrape_id", "snapshot_id", "dataset_id", "record_id",
  "confidence", "quality_status", "fetch_mode",
  "missing_fields": [], "warnings": [],
  "save_policy": "promote_if_valid",
  "provenance": {"source_url","canonical_url","fetched_at","extraction_method","content_hash"}
}
```
**默认绝不直接覆盖主库**：低置信进 staging/quarantine。

## §3 warehouse-first 带 TTL

`spine.py::resolve(db, url, dataset, workspace_id, force_live=False, max_age_sec=None, save_policy="promote_if_valid") -> dict`：

```
1. force_live → 跳仓库，直接 live 抓 + ingest（默认 save_policy=staging 强抓不污染主库）
2. 查 extracted_records (dataset_id, record_key=canonical(url), quality_status='main')
3. 命中：
     age = now - fetched_at
     ttl = max_age_sec(请求级) or dataset.freshness_ttl_sec or 86400(全局兜底)
     age <= ttl → 仓库命中, credits=0, source="warehouse", 返回 data+age+fetched_at
     age >  ttl → 过期 → live 重抓+ingest（content_hash 没变则只刷 fetched_at）
4. 未命中 → live 抓 + ingest
```

**canonicalization `_canonical(url, html_canonical=None)`**：优先用页面 `<link rel=canonical>`（现有 extract_metadata 已解析）；否则规整——去 utm_*/fbclid/gclid 等跟踪参、统一末尾斜杠、小写 host。两 URL canonical 相同 → 同一 record_key。

**TTL 分层**：请求级 `max_age_sec` > 数据集 `freshness_ttl_sec` > 全局 86400。字段级 TTL 留 SP2，SP1 做到记录级 + content_hash 钩子。

## §4 MCP / API 工具

### 2 个新 MCP 工具（注册进 `mcp_server.py`，用 `metered_tool` 计量，不动旧工具）

**`crawl_custom_source(url, dataset, schema?, entity_type?, force_live?, save_policy?, max_age_sec?)`**
- scope `crawler:scrape`。任意 URL → `resolve` → `ingest` → 返回 record + provenance + quality。
- dataset 用 slug 指定，不存在则按 workspace 自动建（entity_type 默认 generic）。
- schema 给定 → 用现有 `extract_structured_data` 的投影把 data 收敛到该 schema（LLM 增强留 SP3）。

**`query_dataset(dataset, query?, entity_type?, include_staging?, limit?)`**
- scope `crawler:read`。查 extracted_records，默认只返 `quality_status='main'`，`include_staging=true` 才带 staging。
- query 做文本匹配：对 `source_url`/`canonical_url` ILIKE，外加把 `data` JSON 转文本搜索——用 `sqlalchemy.cast(ExtractedRecord.data, String)` + `.ilike(f"%{query}%")`（SQLite/PG 都支持 cast-to-text，避免方言分歧；不追求字段级精确查询，SP1 够用）。

### 2 个 v2 REST 端点（复用 v2 鉴权/计量/封套）
- `POST /api/v2/custom/scrape` → crawl_custom_source
- `POST /api/v2/dataset/query` → query_dataset

### 顺手修 discovery（已知 stale bug）
`discovery.py::_TOOLS` 只列了 9 个旧工具，漏了 14 个 agent-first 工具（scrape_url/query_warehouse/crawl_site/extract_structured_data 等）。SP1 把新 2 个 + 漏的补全，让 `llms.txt` / `.well-known/mcp.json` / `agents.json` 准确。

## 验证

- **后端**：`pytest` 绿 + 新增 spine 测试：
  - 迁移演练（真实库副本跑 `init_db()` 两次幂等、3 表建出、现有数据零丢失）。
  - `ingest_extraction` 各 save_policy 落对 quality_status；低置信进 staging；block 警告进 quarantine。
  - `resolve` warehouse-first：首抓 live、TTL 内第二次 credits=0 命中、超 TTL 重抓、content_hash 未变只刷 fetched_at。
  - canonical 去重：带 utm 的 URL 与干净 URL 命中同一 record。
  - upsert：同 URL 重抓更新同一行不新增。
  - MCP `crawl_custom_source`/`query_dataset` 端到端（mock scrape_url，不真实联网）。
- **不回归**：现有 Product 路径、旧 MCP 工具、v2 旧端点全绿（跑全量 pytest）。
- **前端/线上**：本轮不涉及 UI；不部署。

## 不在 SP1 范围（后续子项目）

- **SP2**：字段级 TTL、ETag/Last-Modified 条件 GET 真正发起、热冷数据差异化刷新频率（SP1 只把 etag/last_modified/content_hash 字段存下作钩子）。
- **SP3**：diagnose_site、generate_crawl_recipe、test_crawl_recipe、recipe-driven 通用采集 runtime（registry 加 `platform=="recipe"` 接缝）、统一 11 处 JSON-LD 解析、LLM schema 投影增强。
- **SP4**：把现有 Product 抓取也接质量门、staging→main 人工/自动晋升流、看板可视化 quarantine。
- 现有 ecommerce 表迁进 extracted_records（并存策略下不做，老路保留）。
