# SP1 通用数据脊柱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 smart-crawler 加一层通用数据脊柱——任意 URL/任意 schema 抓一次→带 provenance 入库→TTL 内复用——而不碰现有电商能力。

**Architecture:** 3 张新表（raw_snapshots/extracted_records/datasets，走 `_migrate()` 幂等建）+ 新模块 `backend/app/spine.py`（落库 ingest + warehouse-first resolve + 质量门 + canonical 去重）+ 2 个新 MCP 工具和 2 个 v2 端点，全部复用现有 `scrape_url`/`snapshot.py`/`metered_tool`/`_require_scope`。现有 Product 路径、旧工具一行不动。

**Tech Stack:** FastAPI + SQLAlchemy（SQLite 本地 / PG 生产）、FastMCP、curl_cffi、pytest。

**分支:** `feat/generic-data-spine-sp1`（已建，spec 已 commit 在此分支）。

**Spec:** `docs/superpowers/specs/2026-06-11-generic-data-spine-sp1-design.md`

**关键复用点（已核实）:**
- `agent_crawler.py::scrape_url(db, url, *, formats, wait_for_ms, timeout_ms, force_live, mode) -> dict`，返回含 `data`/`scrape_id`/`metadata`(有 `canonical`)/`markdown`/`usage`/`warnings`。`data` 里有 `confidence`。
- `agent_crawler.py::extract_metadata(html, base_url)` 返回 `canonical`；`_shape_to_schema(data, schema)`（831 行）做 schema 投影。
- `snapshot.py::save(site, name, content) -> None`（不返路径，本计划加一个返路径的变体）。
- `mcp_context.py::metered_tool(required_scope, cacheable)` 装饰器；`McpApiKeyContext` 只有 `api_key_id`（**无 workspace_id**，需从 `ApiKey.workspace_id` 查，models.py:354）。`get_current_api_key()` 取当前 ctx。
- `api/v2.py::_require_scope(db, authorization, x_api_key, required)`；`_api_key_row(...)` 拿 ApiKey 行（有 workspace_id）。
- `db.py::_migrate()` 幂等 ADD COLUMN + `create_all`。

---

## 文件结构

| 文件 | 职责 | 新建/改 |
|---|---|---|
| `backend/app/models.py` | RawSnapshot / ExtractedRecord / Dataset 三模型 | 改（追加） |
| `backend/app/snapshot.py` | 加 `save_returning_path()` 返回 .gz 路径 | 改 |
| `backend/app/spine.py` | canonical/质量门/ingest/resolve/dataset 解析 | 新建 |
| `backend/app/api/v2.py` | `/custom/scrape` + `/dataset/query` 两端点 | 改（追加） |
| `backend/app/mcp_server.py` | `crawl_custom_source` + `query_dataset` 两工具 | 改（追加） |
| `backend/app/api/discovery.py` | 补全 `_TOOLS`（修 stale + 加新 2 个） | 改 |
| `backend/tests/test_spine.py` | spine 单测 | 新建 |
| `backend/tests/test_spine_api.py` | MCP/v2 端到端 | 新建 |

---

## Task 1: 三张新表 + 迁移演练

**Files:**
- Modify: `backend/app/models.py`（文件末尾追加 3 个模型）
- Test: `backend/tests/test_spine.py`（新建）

- [ ] **Step 1: 写迁移断言测试**

新建 `backend/tests/test_spine.py`：

```python
"""通用数据脊柱（SP1）测试。"""
from sqlalchemy import inspect

from app.db import engine, init_db


def test_spine_tables_exist():
    init_db()
    insp = inspect(engine)
    for t in ("raw_snapshots", "extracted_records", "datasets"):
        assert insp.has_table(t), f"缺表 {t}"
    cols = {c["name"] for c in insp.get_columns("extracted_records")}
    for c in ("dataset_id", "snapshot_id", "source_url", "canonical_url",
              "entity_type", "data", "record_key", "content_hash",
              "confidence", "extraction_method", "recipe_id",
              "quality_status", "fetched_at", "workspace_id"):
        assert c in cols, f"extracted_records 缺列 {c}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py::test_spine_tables_exist -v`
Expected: FAIL（表不存在）

- [ ] **Step 3: 加 3 个模型**

在 `backend/app/models.py` 末尾追加（确认顶部已 import `JSON, Float, ForeignKey, UniqueConstraint, Text, DateTime, Integer, String`——均已在用）：

```python
class RawSnapshot(Base):
    """Raw 层 —— 原始抓取的元数据;正文 gzip 在磁盘(snapshot.py)。"""

    __tablename__ = "raw_snapshots"

    id = Column(Integer, primary_key=True)
    url = Column(Text, index=True)
    canonical_url = Column(Text, index=True)
    content_hash = Column(String, index=True)        # sha256(正文)
    fetched_at = Column(DateTime, index=True, default=datetime.utcnow)
    status_code = Column(Integer)
    etag = Column(String)
    last_modified = Column(String)
    content_type = Column(String)
    body_path = Column(String)                        # data/snapshots/*.gz
    fetch_mode = Column(String)                       # live / advanced
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Dataset(Base):
    """View 层入口 —— 命名数据集。"""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("workspace_id", "slug",
                                       name="uq_dataset_ws_slug"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)
    slug = Column(String, index=True)
    entity_type = Column(String)                      # 默认实体类型
    description = Column(Text)
    source_kind = Column(String)                      # custom_url / ecommerce_template
    freshness_ttl_sec = Column(Integer, default=86400)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExtractedRecord(Base):
    """Normalized 层 —— 任意 schema 的结构化结果 + 完整 provenance。"""

    __tablename__ = "extracted_records"
    __table_args__ = (UniqueConstraint("dataset_id", "record_key",
                                       name="uq_record_dataset_key"),)

    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), index=True)
    snapshot_id = Column(Integer, ForeignKey("raw_snapshots.id"), nullable=True)
    source_url = Column(Text, index=True)
    canonical_url = Column(Text, index=True)
    entity_type = Column(String, index=True)
    data = Column(JSON)
    record_key = Column(String, index=True)
    content_hash = Column(String)                     # sha256(规整 data)
    confidence = Column(Float)
    extraction_method = Column(String)
    recipe_id = Column(Integer, nullable=True)        # SP3 用
    quality_status = Column(String, index=True)       # main / staging / quarantine
    fetched_at = Column(DateTime, index=True)
    extracted_at = Column(DateTime, default=datetime.utcnow)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
```

确认 `models.py` 顶部已 `from datetime import datetime`（是）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py::test_spine_tables_exist -v`
Expected: PASS

- [ ] **Step 5: 迁移演练（真实库副本，幂等 + 零丢失）**

Run:
```bash
cd backend
cp ../data/smart_crawler.db /tmp/spine_rehearsal.db
DATABASE_URL="sqlite:////tmp/spine_rehearsal.db" .venv/bin/python -c "
from app.db import init_db; init_db(); init_db()
import sqlite3; c=sqlite3.connect('/tmp/spine_rehearsal.db')
for t in ('raw_snapshots','extracted_records','datasets'):
    assert c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] == 0
print('3 tables OK, products preserved:', c.execute('SELECT count(*) FROM products').fetchone()[0])
"
rm -f /tmp/spine_rehearsal.db
```
Expected: `3 tables OK, products preserved: <非0>` 无报错。

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/models.py backend/tests/test_spine.py
git commit -m "feat(spine): raw_snapshots/extracted_records/datasets models"
```

---

## Task 2: snapshot 返回路径 + canonical/hash 工具

**Files:**
- Modify: `backend/app/snapshot.py`
- Create: `backend/app/spine.py`（先放纯函数：canonical、hash）
- Test: `backend/tests/test_spine.py`（追加）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine.py`：

```python
from app.spine import canonical_url, content_hash


def test_canonical_strips_tracking_and_normalizes():
    a = canonical_url("https://Shop.Example.com/p/1?utm_source=x&id=5")
    b = canonical_url("https://shop.example.com/p/1/?id=5&fbclid=z")
    assert a == b  # 跟踪参去掉、host 小写、末尾斜杠统一、保留 id
    assert "utm_source" not in a and "fbclid" not in a


def test_canonical_prefers_explicit():
    got = canonical_url("https://x.com/a?utm_source=q",
                        explicit="https://x.com/canonical")
    assert got == "https://x.com/canonical"


def test_content_hash_stable_and_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k "canonical or content_hash" -v`
Expected: FAIL（No module named 'app.spine'）

- [ ] **Step 3: 建 spine.py 纯函数**

新建 `backend/app/spine.py`：

```python
"""通用数据脊柱（SP1）—— 落库 + warehouse-first + 质量门。

复用 agent_crawler.scrape_url 的抓取与提取,只在其后接落库/读路径。
不改任何现有电商表/采集器。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_ga", "ref", "ref_src",
}


def canonical_url(url: str, explicit: str | None = None) -> str:
    """规整 URL 作去重键。explicit(页面 <link rel=canonical>) 优先。"""
    target = explicit or url
    p = urlparse(target if "://" in target else f"https://{target}")
    host = (p.netloc or "").lower()
    path = p.path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                       if k.lower() not in _TRACKING_PARAMS])
    return urlunparse((p.scheme or "https", host, path, "", query, ""))


def content_hash(value) -> str:
    """对 dict/str 算稳定 sha256(dict 按 key 排序,顺序无关)。"""
    if isinstance(value, (bytes, str)):
        raw = value.encode("utf-8") if isinstance(value, str) else value
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

- [ ] **Step 4: 给 snapshot.py 加返路径变体**

在 `backend/app/snapshot.py` 的 `save()` 之后追加：

```python
def save_returning_path(site: str, name: str, content) -> str | None:
    """同 save(),但返回写入的 .gz 路径(失败返 None)。spine 用于记录 body_path。"""
    if not ENABLED or content is None:
        return None
    try:
        day = date.today().isoformat()
        folder = SNAPSHOT_DIR / site / day
        folder.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        path = folder / f"{_safe(name)}.gz"
        with gzip.open(path, "wb") as f:
            f.write(data)
        return str(path)
    except Exception:
        return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k "canonical or content_hash" -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine.py backend/app/snapshot.py backend/tests/test_spine.py
git commit -m "feat(spine): canonical_url + content_hash + snapshot path"
```

---

## Task 3: dataset 解析 + 质量门

**Files:**
- Modify: `backend/app/spine.py`
- Test: `backend/tests/test_spine.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
from app.db import SessionLocal
from app.spine import get_or_create_dataset, quality_check


def test_get_or_create_dataset_idempotent():
    init_db(); s = SessionLocal()
    d1 = get_or_create_dataset(s, "My Set", workspace_id=None, entity_type="product")
    d2 = get_or_create_dataset(s, "My Set", workspace_id=None, entity_type="product")
    assert d1.id == d2.id and d1.slug == "my-set"
    s.close()


def test_quality_check_promote_if_valid():
    # 高置信 + 必填齐 → main
    st, missing = quality_check({"title": "x"}, "product", 0.9, [], "promote_if_valid")
    assert st == "main" and missing == []
    # 低置信 → staging
    st, _ = quality_check({"title": "x"}, "product", 0.3, [], "promote_if_valid")
    assert st == "staging"
    # 缺必填 → staging + missing
    st, missing = quality_check({}, "product", 0.9, [], "promote_if_valid")
    assert st == "staging" and "title" in missing
    # 显式 main 跳质量门
    st, _ = quality_check({}, "product", 0.1, [], "main")
    assert st == "main"
    # block 警告 → quarantine(覆盖 policy)
    st, _ = quality_check({"title": "x"}, "product", 0.9, ["blocked"], "main")
    assert st == "quarantine"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k "dataset or quality" -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `spine.py` 追加（顶部补 import）：

```python
import re

from sqlalchemy.orm import Session

from .models import Dataset, ExtractedRecord, RawSnapshot

_CONFIDENCE_MIN = 0.6
_REQUIRED_FIELDS = {
    "product": {"title"},
    "review": {"content"},
    "article": {"title"},
    "generic": set(),
}
_BLOCK_MARKERS = ("blocked", "challenge", "captcha", "403", "429")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "dataset"


def get_or_create_dataset(db: Session, name: str, *, workspace_id: int | None,
                          entity_type: str = "generic",
                          source_kind: str = "custom_url") -> Dataset:
    slug = _slugify(name)
    row = (db.query(Dataset)
           .filter(Dataset.workspace_id == workspace_id, Dataset.slug == slug)
           .first())
    if row:
        return row
    row = Dataset(name=name, slug=slug, entity_type=entity_type,
                  source_kind=source_kind, workspace_id=workspace_id)
    db.add(row); db.commit(); db.refresh(row)
    return row


def quality_check(data: dict, entity_type: str, confidence: float,
                  warnings: list, save_policy: str) -> tuple[str, list[str]]:
    """返回 (quality_status, missing_fields)。"""
    required = _REQUIRED_FIELDS.get(entity_type, set())
    missing = [f for f in required if not (data or {}).get(f)]
    # 被反爬污染 → quarantine,优先级最高
    wtext = " ".join(str(w) for w in (warnings or [])).lower()
    if any(m in wtext for m in _BLOCK_MARKERS):
        return "quarantine", missing
    if save_policy == "quarantine":
        return "quarantine", missing
    if save_policy == "main":
        return "main", missing
    if save_policy == "staging":
        return "staging", missing
    # promote_if_valid(默认)
    if confidence >= _CONFIDENCE_MIN and not missing:
        return "main", missing
    return "staging", missing
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k "dataset or quality" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine.py backend/tests/test_spine.py
git commit -m "feat(spine): get_or_create_dataset + quality_check gate"
```

---

## Task 4: ingest_extraction 落库

**Files:**
- Modify: `backend/app/spine.py`
- Test: `backend/tests/test_spine.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def _fake_scrape(data, *, confidence=0.9, warnings=None, canonical=None,
                 html="<html>x</html>"):
    return {
        "scrape_id": "scr_test", "url": "https://x.com/p/1",
        "data": {**data, "confidence": confidence},
        "metadata": {"canonical": canonical}, "html": html,
        "warnings": warnings or [],
        "usage": {"source": "live"},
    }


def test_ingest_creates_snapshot_and_record():
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "ingest-set", workspace_id=None, entity_type="product")
    from app.spine import ingest_extraction
    out = ingest_extraction(s, _fake_scrape({"title": "Widget"}), ds,
                            save_policy="promote_if_valid", workspace_id=None)
    assert out["quality_status"] == "main"
    assert out["record_id"] and out["snapshot_id"]
    assert out["provenance"]["content_hash"]
    # 库里确有 1 条 main 记录
    rec = s.query(ExtractedRecord).filter_by(dataset_id=ds.id).one()
    assert rec.data["title"] == "Widget" and rec.confidence == 0.9
    s.close()


def test_ingest_low_confidence_goes_staging():
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "stg-set", workspace_id=None, entity_type="product")
    from app.spine import ingest_extraction
    out = ingest_extraction(s, _fake_scrape({"title": "X"}, confidence=0.2), ds,
                            save_policy="promote_if_valid", workspace_id=None)
    assert out["quality_status"] == "staging"
    s.close()


def test_ingest_upsert_same_url_no_dup_and_hash_skip():
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "up-set", workspace_id=None, entity_type="product")
    from app.spine import ingest_extraction
    a = ingest_extraction(s, _fake_scrape({"title": "A"}), ds, save_policy="main", workspace_id=None)
    b = ingest_extraction(s, _fake_scrape({"title": "A"}), ds, save_policy="main", workspace_id=None)
    assert a["record_id"] == b["record_id"]                 # 同 URL 同一行
    assert s.query(ExtractedRecord).filter_by(dataset_id=ds.id).count() == 1
    s.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k ingest -v`
Expected: FAIL

- [ ] **Step 3: 实现 ingest_extraction**

在 `spine.py` 追加（顶部补 `from . import snapshot`）：

```python
def ingest_extraction(db: Session, scrape_result: dict, dataset: Dataset, *,
                      save_policy: str = "promote_if_valid",
                      workspace_id: int | None = None) -> dict:
    """把一次 scrape_url 结果落库:raw_snapshot + extracted_record + 质量门。"""
    data = dict(scrape_result.get("data") or {})
    confidence = float(data.pop("confidence", 0.0) or 0.0)
    meta = scrape_result.get("metadata") or {}
    url = scrape_result.get("url") or ""
    canon = canonical_url(url, explicit=meta.get("canonical"))
    warnings = scrape_result.get("warnings") or []
    entity_type = dataset.entity_type or "generic"
    now = datetime.utcnow()

    # 1) raw_snapshot(正文写盘 + 元数据入表)
    html = scrape_result.get("html") or ""
    body_path = snapshot.save_returning_path(
        dataset.slug, canon.rsplit("/", 1)[-1] or "page", html)
    snap = RawSnapshot(
        url=url, canonical_url=canon, content_hash=content_hash(html),
        fetched_at=now, status_code=(meta.get("status") or 200),
        etag=meta.get("etag"), last_modified=meta.get("last_modified"),
        content_type=meta.get("content_type"), body_path=body_path,
        fetch_mode=(scrape_result.get("usage") or {}).get("source") or "live",
        workspace_id=workspace_id)
    db.add(snap); db.flush()

    # 2) 质量门
    method = "jsonld" if confidence >= 0.9 else "heuristic"
    status, missing = quality_check(data, entity_type, confidence, warnings, save_policy)
    chash = content_hash(data)

    # 3) upsert by (dataset_id, record_key)
    rec = (db.query(ExtractedRecord)
           .filter_by(dataset_id=dataset.id, record_key=canon).first())
    if rec is None:
        rec = ExtractedRecord(dataset_id=dataset.id, record_key=canon,
                              source_url=url, canonical_url=canon,
                              entity_type=entity_type, workspace_id=workspace_id)
        db.add(rec)
    elif rec.content_hash == chash:
        # 内容没变 → 只刷新 fetched_at,不重写 data(SP2 少爬钩子)
        rec.fetched_at = now; rec.snapshot_id = snap.id
        db.commit()
        return _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                                save_policy, canon, url, unchanged=True)
    rec.data = data; rec.content_hash = chash; rec.confidence = confidence
    rec.extraction_method = method; rec.quality_status = status
    rec.snapshot_id = snap.id; rec.fetched_at = now; rec.extracted_at = now
    db.commit(); db.refresh(rec)
    return _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                            save_policy, canon, url, unchanged=False)


def _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                     save_policy, canon, url, *, unchanged) -> dict:
    return {
        "scrape_id": scrape_result.get("scrape_id"),
        "snapshot_id": snap.id, "dataset_id": dataset.id, "record_id": rec.id,
        "confidence": rec.confidence, "quality_status": status,
        "fetch_mode": snap.fetch_mode, "missing_fields": missing,
        "warnings": scrape_result.get("warnings") or [],
        "save_policy": save_policy, "unchanged": unchanged,
        "provenance": {
            "source_url": url, "canonical_url": canon,
            "fetched_at": rec.fetched_at.isoformat() if rec.fetched_at else None,
            "extraction_method": rec.extraction_method,
            "content_hash": rec.content_hash,
        },
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k ingest -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine.py backend/tests/test_spine.py
git commit -m "feat(spine): ingest_extraction (snapshot+record+upsert+gate)"
```

---

## Task 5: resolve（warehouse-first 带 TTL）

**Files:**
- Modify: `backend/app/spine.py`
- Test: `backend/tests/test_spine.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_resolve_warehouse_hit_within_ttl(monkeypatch):
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "res-set", workspace_id=None, entity_type="product")
    import app.spine as spine
    calls = {"n": 0}
    def fake_scrape(db, url, **kw):
        calls["n"] += 1
        return _fake_scrape({"title": "R"}, canonical=None) | {"url": url}
    monkeypatch.setattr(spine, "_do_scrape", fake_scrape)
    # 首次 → live 抓 1 次
    r1 = spine.resolve(s, "https://x.com/p/9", ds, workspace_id=None)
    assert r1["source"] in ("live", "warehouse") and calls["n"] == 1
    # 第二次(TTL 内) → 命中,不再抓
    r2 = spine.resolve(s, "https://x.com/p/9", ds, workspace_id=None, max_age_sec=3600)
    assert r2["source"] == "warehouse" and r2["credits_used"] == 0 and calls["n"] == 1
    s.close()


def test_resolve_force_live_bypasses_warehouse(monkeypatch):
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "fl-set", workspace_id=None, entity_type="product")
    import app.spine as spine
    calls = {"n": 0}
    def fake_scrape(db, url, **kw):
        calls["n"] += 1
        return _fake_scrape({"title": "R"}) | {"url": url}
    monkeypatch.setattr(spine, "_do_scrape", fake_scrape)
    spine.resolve(s, "https://x.com/p/8", ds, workspace_id=None)
    spine.resolve(s, "https://x.com/p/8", ds, workspace_id=None, force_live=True)
    assert calls["n"] == 2  # force_live 不命中仓库
    s.close()


def test_resolve_stale_refetches(monkeypatch):
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "stale-set", workspace_id=None, entity_type="product")
    import app.spine as spine
    from datetime import timedelta
    calls = {"n": 0}
    def fake_scrape(db, url, **kw):
        calls["n"] += 1
        return _fake_scrape({"title": "R"}) | {"url": url}
    monkeypatch.setattr(spine, "_do_scrape", fake_scrape)
    spine.resolve(s, "https://x.com/p/7", ds, workspace_id=None)
    # 人为把 fetched_at 推老
    rec = s.query(ExtractedRecord).filter_by(dataset_id=ds.id).first()
    rec.fetched_at = datetime.utcnow() - timedelta(seconds=99999); s.commit()
    spine.resolve(s, "https://x.com/p/7", ds, workspace_id=None, max_age_sec=10)
    assert calls["n"] == 2  # 过期重抓
    s.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k resolve -v`
Expected: FAIL

- [ ] **Step 3: 实现 resolve + _do_scrape 包装**

在 `spine.py` 追加（顶部补 `from datetime import datetime, timedelta`）：

```python
_GLOBAL_TTL = 86400


def _do_scrape(db: Session, url: str, *, force_live: bool, mode: str) -> dict:
    """包一层 agent_crawler.scrape_url,便于测试 monkeypatch。"""
    from .agent_crawler import scrape_url as _scrape
    out = _scrape(db, url, force_live=force_live, mode=mode)
    out.setdefault("url", url)
    return out


def resolve(db: Session, url: str, dataset: Dataset, *, workspace_id: int | None,
            force_live: bool = False, max_age_sec: int | None = None,
            save_policy: str = "promote_if_valid", mode: str = "standard") -> dict:
    """warehouse-first 带 TTL。返回 data + source + credits + provenance。"""
    canon = canonical_url(url)
    if not force_live:
        rec = (db.query(ExtractedRecord)
               .filter_by(dataset_id=dataset.id, record_key=canon,
                          quality_status="main").first())
        if rec and rec.fetched_at:
            ttl = max_age_sec or dataset.freshness_ttl_sec or _GLOBAL_TTL
            age = (datetime.utcnow() - rec.fetched_at).total_seconds()
            if age <= ttl:
                return {
                    "source": "warehouse", "credits_used": 0,
                    "dataset_id": dataset.id, "record_id": rec.id,
                    "data": rec.data, "confidence": rec.confidence,
                    "quality_status": rec.quality_status,
                    "age_sec": int(age),
                    "provenance": {
                        "source_url": rec.source_url,
                        "canonical_url": rec.canonical_url,
                        "fetched_at": rec.fetched_at.isoformat(),
                        "extraction_method": rec.extraction_method,
                        "content_hash": rec.content_hash,
                    },
                }
    # 未命中/过期/force_live → live 抓 + 落库
    scrape_result = _do_scrape(db, url, force_live=True, mode=mode)
    # force_live 默认强抓不污染主库 → staging
    policy = save_policy if not force_live else "staging"
    out = ingest_extraction(db, scrape_result, dataset,
                            save_policy=policy, workspace_id=workspace_id)
    out["source"] = "live"
    out["credits_used"] = (scrape_result.get("usage") or {}).get("credits_used", 2)
    out["data"] = {k: v for k, v in (scrape_result.get("data") or {}).items()
                   if k != "confidence"}
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k resolve -v`
Expected: 3 passed

- [ ] **Step 5: 全量回归**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全 passed（原有 + spine 新增）

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine.py backend/tests/test_spine.py
git commit -m "feat(spine): resolve warehouse-first with TTL"
```

---

## Task 6: query_dataset 查询

**Files:**
- Modify: `backend/app/spine.py`
- Test: `backend/tests/test_spine.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_query_dataset_main_only_by_default():
    init_db(); s = SessionLocal()
    ds = get_or_create_dataset(s, "q-set", workspace_id=None, entity_type="product")
    from app.spine import ingest_extraction, query_dataset
    ingest_extraction(s, _fake_scrape({"title": "Alpha"}), ds, save_policy="main", workspace_id=None)
    ingest_extraction(s, _fake_scrape({"title": "Beta"}) | {"url": "https://x.com/p/2"},
                      ds, save_policy="staging", workspace_id=None)
    main = query_dataset(s, ds, query=None, include_staging=False, limit=10)
    assert main["total"] == 1 and main["items"][0]["data"]["title"] == "Alpha"
    allrec = query_dataset(s, ds, query=None, include_staging=True, limit=10)
    assert allrec["total"] == 2
    # query 文本命中 data
    hit = query_dataset(s, ds, query="Alpha", include_staging=True, limit=10)
    assert hit["total"] == 1
    s.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k query_dataset -v`
Expected: FAIL

- [ ] **Step 3: 实现 query_dataset**

在 `spine.py` 追加（顶部补 `from sqlalchemy import String, cast, or_`）：

```python
def query_dataset(db: Session, dataset: Dataset, *, query: str | None = None,
                  entity_type: str | None = None, include_staging: bool = False,
                  limit: int = 20) -> dict:
    """查通用数据集。默认只返 main;include_staging 才带 staging。"""
    q = db.query(ExtractedRecord).filter(ExtractedRecord.dataset_id == dataset.id)
    statuses = ["main"] + (["staging"] if include_staging else [])
    q = q.filter(ExtractedRecord.quality_status.in_(statuses))
    if entity_type:
        q = q.filter(ExtractedRecord.entity_type == entity_type)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(ExtractedRecord.source_url.ilike(like),
                         ExtractedRecord.canonical_url.ilike(like),
                         cast(ExtractedRecord.data, String).ilike(like)))
    total = q.count()
    rows = q.order_by(ExtractedRecord.fetched_at.desc().nullslast(),
                      ExtractedRecord.id.desc()).limit(limit).all()
    return {"total": total, "dataset": dataset.slug, "items": [
        {"record_id": r.id, "entity_type": r.entity_type, "data": r.data,
         "confidence": r.confidence, "quality_status": r.quality_status,
         "source_url": r.source_url,
         "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None}
        for r in rows]}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine.py -k query_dataset -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine.py backend/tests/test_spine.py
git commit -m "feat(spine): query_dataset (main-only default + staging opt-in)"
```

---

## Task 7: MCP 工具 crawl_custom_source + query_dataset

**Files:**
- Modify: `backend/app/mcp_server.py`
- Test: `backend/tests/test_spine_api.py`（新建）

参考现有 `scrape_url` MCP 工具（mcp_server.py:519）：用 `@metered_tool(required_scope=..., cacheable=...)`，内部 `SessionLocal()` 开 session，从 `get_current_api_key()` 拿 ctx，再用 `ApiKey.workspace_id` 解析 workspace。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_spine_api.py`：

```python
"""SP1 MCP/v2 端到端(mock scrape,不联网)。"""
from unittest.mock import patch

from app.db import SessionLocal, init_db


def _scrape_stub(db, url, **kw):
    return {"scrape_id": "scr_x", "url": url,
            "data": {"title": "MockItem", "confidence": 0.95},
            "metadata": {"canonical": None}, "html": "<html>m</html>",
            "warnings": [], "usage": {"source": "live", "credits_used": 2}}


def test_crawl_custom_source_tool():
    init_db()
    from app import mcp_server
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        out = mcp_server.crawl_custom_source(
            url="https://x.com/p/1", dataset="mcp-set", save_policy="main")
    assert out["record_id"] and out["quality_status"] == "main"
    assert out["provenance"]["source_url"] == "https://x.com/p/1"


def test_query_dataset_tool():
    init_db()
    from app import mcp_server
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        mcp_server.crawl_custom_source(url="https://x.com/p/2",
                                       dataset="mcp-q", save_policy="main")
    out = mcp_server.query_dataset(dataset="mcp-q", query="MockItem")
    assert out["total"] >= 1
```

注：已核实——本项目 MCP 工具被 `@metered_tool` 装饰后**仍是普通可调用函数**，现有测试直接 `mcp_server.crawl_site(...)` / `mcp_server.scrape_url(...)` 调用（见 tests/test_access_and_metering.py:94,253），**不用 `.fn`**。所以上面直接调 `mcp_server.crawl_custom_source(...)`。无 api key ctx 时 `_ws_id_from_ctx` 返 None（workspace_id=None），测试可跑。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -v`
Expected: FAIL

- [ ] **Step 3: 实现两个工具**

先看 mcp_server.py 顶部如何拿 db / workspace（搜 `SessionLocal`、`get_current_api_key`、`ApiKey`）。在 mcp_server.py 末尾（其他 `@metered_tool` 之后）追加：

```python
def _ws_id_from_ctx(db) -> int | None:
    from .mcp_context import get_current_api_key
    from .models import ApiKey
    ctx = get_current_api_key()
    if not ctx:
        return None
    row = db.get(ApiKey, ctx.api_key_id)
    return row.workspace_id if row else None


@metered_tool(required_scope="crawler:scrape", cacheable=False)
def crawl_custom_source(url: str, dataset: str, schema: dict | None = None,
                        entity_type: str = "generic", force_live: bool = False,
                        save_policy: str = "promote_if_valid",
                        max_age_sec: int | None = None) -> dict:
    """通用数据采集:任意 URL → 探测/抓取 → 带 provenance 入指定 dataset。

    warehouse-first:dataset 内同 URL 在 TTL(max_age_sec 或 dataset 默认)内命中则
    credits_used=0 直接返回。force_live=true 强制实时抓(默认进 staging 不污染主库)。
    save_policy: promote_if_valid(默认)/staging/main/quarantine。
    返回 record_id/quality_status/confidence/provenance/warnings。
    """
    from . import spine
    from .models import ApiKey
    s = SessionLocal()
    try:
        ws = _ws_id_from_ctx(s)
        ds = spine.get_or_create_dataset(s, dataset, workspace_id=ws,
                                         entity_type=entity_type)
        out = spine.resolve(s, url, ds, workspace_id=ws, force_live=force_live,
                            max_age_sec=max_age_sec, save_policy=save_policy)
        # schema 投影(复用现有 _shape_to_schema)
        if schema and out.get("data"):
            from .agent_crawler import _shape_to_schema
            out["data"] = _shape_to_schema(out["data"], schema)
        return out
    finally:
        s.close()


@metered_tool(required_scope="crawler:read", cacheable=True)
def query_dataset(dataset: str, query: str | None = None,
                  entity_type: str | None = None, include_staging: bool = False,
                  limit: int = 20) -> dict:
    """查通用数据集(extracted_records)。默认只返 main;include_staging=true 带 staging。"""
    from . import spine
    s = SessionLocal()
    try:
        ws = _ws_id_from_ctx(s)
        ds = spine.get_or_create_dataset(s, dataset, workspace_id=ws)
        return spine.query_dataset(s, ds, query=query, entity_type=entity_type,
                                   include_staging=include_staging, limit=limit)
    finally:
        s.close()
```

确认 mcp_server.py 顶部已 import `SessionLocal`（现有工具在用,是）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -v`
Expected: PASS（MCP 工具直接调用，无 `.fn`，已核实）

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/mcp_server.py backend/tests/test_spine_api.py
git commit -m "feat(spine): crawl_custom_source + query_dataset MCP tools"
```

---

## Task 8: v2 REST 端点

**Files:**
- Modify: `backend/app/api/v2.py`
- Test: `backend/tests/test_spine_api.py`（追加）

参考现有 `POST /api/v2/scrape`（v2.py:182）：`_require_scope(...)` + 调 agent_crawler + `_meter(...)`。

- [ ] **Step 1: 写失败测试**

```python
from fastapi.testclient import TestClient
from app.main import app
from app.auth import make_token  # 若 v2 用 api key 而非 token,改用现有 v2 测试的鉴权方式


def test_v2_custom_scrape_and_query(monkeypatch):
    init_db()
    # 复用现有 v2 测试的 api key 构造方式;若仓库已有 conftest fixture 提供 key,用之。
    # 这里只验证路由存在 + 鉴权门(未带 key → 401)
    client = TestClient(app)
    r = client.post("/api/v2/custom/scrape", json={"url": "https://x.com", "dataset": "d"})
    assert r.status_code in (401, 403)  # 缺鉴权被挡
    r2 = client.post("/api/v2/dataset/query", json={"dataset": "d"})
    assert r2.status_code in (401, 403)
```

（注：完整鉴权端到端依赖现有 v2 测试的 api key fixture。先确认 `backend/tests/` 里 v2 怎么测——`grep -rn "api/v2\|x-api-key\|X-API-Key" backend/tests/`——复用同款 key 构造补一条 200 路径测试。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -k v2_custom -v`
Expected: FAIL（404,路由未加）

- [ ] **Step 3: 加两个端点**

在 `backend/app/api/v2.py` 末尾追加（参考现有 ScrapeRequest/scrape 写法；顶部 import 区补 `from .. import spine`、`from ..db import SessionLocal`、`from ..models import ApiKey`）：

```python
class CustomScrapeRequest(BaseModel):
    url: str
    dataset: str
    entity_type: str = "generic"
    schema: dict | None = None
    force_live: bool = False
    save_policy: str = "promote_if_valid"
    max_age_sec: int | None = None


class DatasetQueryRequest(BaseModel):
    dataset: str
    query: str | None = None
    entity_type: str | None = None
    include_staging: bool = False
    limit: int = 20


def _v2_ws_id(db, authorization, x_api_key) -> int | None:
    row = _api_key_row(db, authorization, x_api_key)
    return row.workspace_id if row else None


@router.post("/custom/scrape")
def custom_scrape(req: CustomScrapeRequest,
                  authorization: str = Header(default=""),
                  x_api_key: str = Header(default="", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    ws = _v2_ws_id(db, authorization, x_api_key)
    ds = spine.get_or_create_dataset(db, req.dataset, workspace_id=ws,
                                     entity_type=req.entity_type)
    out = spine.resolve(db, req.url, ds, workspace_id=ws, force_live=req.force_live,
                        max_age_sec=req.max_age_sec, save_policy=req.save_policy)
    if req.schema and out.get("data"):
        from ..agent_crawler import _shape_to_schema
        out["data"] = _shape_to_schema(out["data"], req.schema)
    _meter(db, authorization, x_api_key, "custom/scrape", out)
    return out


@router.post("/dataset/query")
def dataset_query(req: DatasetQueryRequest,
                  authorization: str = Header(default=""),
                  x_api_key: str = Header(default="", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    _require_scope(db, authorization, x_api_key, "crawler:read")
    ws = _v2_ws_id(db, authorization, x_api_key)
    ds = spine.get_or_create_dataset(db, req.dataset, workspace_id=ws)
    out = spine.query_dataset(db, ds, query=req.query, entity_type=req.entity_type,
                              include_staging=req.include_staging, limit=req.limit)
    _meter(db, authorization, x_api_key, "dataset/query", out)
    return out
```

确认 v2.py 顶部已有 `Header`, `Depends`, `get_db`, `BaseModel`, `_require_scope`, `_meter`, `_api_key_row`（现有端点在用）。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -v && .venv/bin/python -m pytest -q`
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/api/v2.py backend/tests/test_spine_api.py
git commit -m "feat(spine): v2 /custom/scrape + /dataset/query endpoints"
```

---

## Task 9: 修 discovery 的 stale _TOOLS

**Files:**
- Modify: `backend/app/api/discovery.py`
- Test: `backend/tests/test_spine_api.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_discovery_lists_new_tools():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    body = client.get("/.well-known/mcp.json").json()
    names = {t.get("name") for t in body.get("tools", [])}
    assert "crawl_custom_source" in names
    assert "query_dataset" in names
    assert "scrape_url" in names  # 之前漏掉的 agent-first 工具
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -k discovery -v`
Expected: FAIL

- [ ] **Step 3: 补全 _TOOLS**

读 `backend/app/api/discovery.py` 的 `_TOOLS` 列表（约 32 行）。把缺的 agent-first 工具补进去：`scrape_url`、`map_site`、`crawl_site`、`extract_structured_data`、`query_warehouse`、`fetch_listing_voc`，以及本次新增的 `crawl_custom_source`、`query_dataset`。每条按现有 _TOOLS 条目的结构（name + description）追加。保持现有 9 条不删。

（具体每条的 description 跟随现有条目风格一行简述；name 必须与 mcp_server.py 里 @metered_tool 装饰的函数名完全一致。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_api.py -k discovery -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/api/discovery.py backend/tests/test_spine_api.py
git commit -m "fix(discovery): list agent-first + spine tools in manifests"
```

---

## Task 10: 端到端验证 + memory

**Files:** 无（验证）

- [ ] **Step 1: 后端全量回归**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全 passed（原有 185 + spine 新增），无回归。

- [ ] **Step 2: 迁移演练复核（真实库副本）**

Run:
```bash
cd backend
cp ../data/smart_crawler.db /tmp/spine_e2e.db
DATABASE_URL="sqlite:////tmp/spine_e2e.db" .venv/bin/python -c "
from app.db import init_db; init_db(); init_db()
import sqlite3; c=sqlite3.connect('/tmp/spine_e2e.db')
print('products preserved:', c.execute('SELECT count(*) FROM products').fetchone()[0])
print('spine tables:', [t for t in ('raw_snapshots','extracted_records','datasets')])
"
rm -f /tmp/spine_e2e.db
```
Expected: products 非 0，无报错。

- [ ] **Step 3: 端到端脚本（mock 抓取，验 warehouse-first 闭环）**

Run:
```bash
cd backend && .venv/bin/python -c "
from unittest.mock import patch
from app.db import init_db, SessionLocal
import app.spine as spine
init_db(); s = SessionLocal()
ds = spine.get_or_create_dataset(s, 'e2e', workspace_id=None, entity_type='product')
def stub(db,url,**kw): return {'scrape_id':'x','url':url,'data':{'title':'E2E','confidence':0.95},'metadata':{'canonical':None},'html':'<html>x</html>','warnings':[],'usage':{'source':'live','credits_used':2}}
with patch('app.spine._do_scrape', side_effect=stub):
    a = spine.resolve(s,'https://x.com/p/1?utm_source=z',ds,workspace_id=None)
    b = spine.resolve(s,'https://x.com/p/1',ds,workspace_id=None,max_age_sec=3600)
print('1st:', a['source'], '2nd:', b['source'], 'credits:', b['credits_used'])
assert b['source']=='warehouse' and b['credits_used']==0  # canonical 去重命中
print('query:', spine.query_dataset(s, ds, query='E2E')['total'])
s.close()
"
```
Expected: `1st: live 2nd: warehouse credits: 0` + `query: 1`（带 utm 的 URL 与干净 URL canonical 相同 → 命中）。

- [ ] **Step 4: 更新 memory**

把 SP1 完成情况追加进 `acceptance-report-rootcause.md` 或新建一条平台化 memory（SP1 已建脊柱、4 子项目拆法、SP2/3/4 待做、未部署）。

- [ ] **Step 5: 不自动部署**。汇报完成,等用户决定 commit/部署。

---

## Self-Review（写计划者已核对）

- **Spec 覆盖**：§1 三表→Task1；canonical/hash→Task2；dataset+质量门→Task3；ingest+save_policy+upsert+content_hash 跳过→Task4；warehouse-first TTL+force_live→Task5；query_dataset→Task6；MCP 工具→Task7；v2 端点→Task8；discovery 修复→Task9；端到端+迁移演练→Task10。全覆盖。
- **类型/签名一致**：`canonical_url(url, explicit=)`、`content_hash(value)`、`get_or_create_dataset(db, name, *, workspace_id, entity_type, source_kind)`、`quality_check(data, entity_type, confidence, warnings, save_policy)->(status, missing)`、`ingest_extraction(db, scrape_result, dataset, *, save_policy, workspace_id)`、`resolve(db, url, dataset, *, workspace_id, force_live, max_age_sec, save_policy, mode)`、`query_dataset(db, dataset, *, query, entity_type, include_staging, limit)` 跨 Task 一致；MCP/v2 都调同一组 spine 函数。
- **已知风险（实现时核实）**：
  1. ~~Task7 FastMCP 工具测试调用方式~~——已核实：`@metered_tool` 后仍是普通函数，直接 `mcp_server.crawl_custom_source(...)` 调，不用 `.fn`（tests/test_access_and_metering.py:94,253 先例）。
  2. Task8 v2 鉴权 fixture——复用现有 v2 测试的 api key 构造（tests/test_agent_crawler.py:310 用 `ApiKey(id=..., scopes=...)` 直接建行）；`_api_key_row`/`_meter`/`_require_scope` 签名以 v2.py 实际为准。
  3. `scrape_url` 返回的 `metadata` 是否含 `status`/`etag`/`last_modified`/`content_type`——当前 extract_metadata 不返这些，ingest 里用 `.get(...)` 容错（缺则 None），不阻断；真正抓 HTTP 头留 SP2。
  4. `_shape_to_schema` 入参形状以 agent_crawler.py:831 实际为准。
