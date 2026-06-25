# 分布式抓取（NAS 调度 + Mac mini 执行）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 NAS 只做任务调度+数据存储，抓取下沉到 Mac mini（第一批 mini1 + mini4），多 mini 共享 NAS 的 PostgreSQL 队列与代理租约，互不撞 IP、健康度按节点隔离，第一阶段仅 `xiaokang` 租户走 mini 验证且不中断其余租户。

**Architecture:** worker 本就是无状态轮询 PG 队列模型（claim_job 乐观锁 → execute_job 写回 PG）。本计划先做 4 处后端代码改动（强制代理租约 D2、健康度按节点隔离 D4、claim_job 租户路由 D3）+ 测试，再做 NAS 侧 PG 暴露与角色配置，最后做 mini 装机/launchd/rsync 部署，按灰度顺序串联。

**Tech Stack:** Python 3.12、SQLAlchemy 2.0、PostgreSQL 16、pytest（SQLite 内存库）、Playwright、launchd、rsync、Tailscale。

参考设计文档：`docs/superpowers/specs/2026-06-23-distributed-mini-workers-design.md`

---

## 第一批 mini 节点

| 节点 | SSH | NODE_ID | worker id 示例 |
|------|-----|---------|----------------|
| mini1 | `solvea@100.75.94.90` | `US-macmini1` | `US-macmini1-1`, `US-macmini1-2` |
| mini4 | `solvea@100.72.33.57` | `US-macmini4` | `US-macmini4-1`, `US-macmini4-2` |

mini4 已于 2026-06-24 探测：`mini4.local` / arm64 / macOS 26.3.1，SSH 免密直连。

## 重要约定（每个执行者必读）

- **测试运行目录**：所有 pytest 命令在 `backend/` 目录下执行（`cd backend`）。`backend/tests/conftest.py` 会自动把 `DATABASE_URL` 指向临时 SQLite，**不会污染生产库**。
- **测试基类**：proxy_health 相关测试用内存 SQLite，模式见 `backend/tests/test_frontier_and_proxy_health.py`（`create_engine("sqlite:///:memory:")` + `Base.metadata.create_all`）。
- **提交粒度**：每个 Task 末尾提交一次。提交信息末尾加：
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```
- **不要在本计划中部署**：Task 1-8 是代码与测试（可安全在主工作区或 worktree 做）。Task 9 起是运维/部署，需在对应主机执行，且严格按灰度顺序。

---

## 文件结构（改动地图）

| 文件 | 责任 | Task |
|------|------|------|
| `backend/app/models.py` | `ProxyHealth` 加 `node` 列、唯一键改 `(proxy_hash, node)` | 1 |
| `backend/app/proxy_health.py` | `record_proxy_result` / `unhealthy_proxy_hashes` 加 `node` 参数 | 2 |
| `backend/app/proxy_pool.py` | `NODE_ID` 常量；`_persistent_unhealthy_hashes()` 透传 node | 3 |
| `backend/app/fetching.py` | D2 强制 lease 默认值；`record_proxy_result` 传 node | 4 |
| `backend/app/proxy_probe.py` | `record_proxy_result` 传 node | 4 |
| `backend/app/runner.py` | `claim_job` 加 workspace allowlist/blocklist + workspace_sites 判定 | 5 |
| `backend/app/worker.py` | 读 `WORKSPACE_ALLOWLIST`/`WORKSPACE_BLOCKLIST` env 传入 claim_job | 6 |
| `backend/tests/test_proxy_health_node.py` | D4 健康隔离单测 | 1-2 |
| `backend/tests/test_claim_job_workspace.py` | 租户路由单测 | 5 |
| `backend/tests/test_proxy_lease_concurrency.py` | 防撞单测 | 7 |
| `backend/scripts/migrate_proxy_health_node.py` | ProxyHealth 数据迁移（回填 node='nas'） | 8 |
| `docker-compose.yml` | PG 暴露 Tailscale；NAS worker 加 BLOCKLIST + NODE_ID | 10 |
| `deploy/io.smartcrawler.worker.plist` | launchd 模板 | 11 |
| `scripts/mini_bootstrap.sh` | mini 装机 | 11 |
| `scripts/deploy_mini.sh` | rsync 部署 | 13 |

---

## Task 1: ProxyHealth 加 node 维度（schema）

**Files:**
- Modify: `backend/app/models.py:509-530`（ProxyHealth 类）
- Test: `backend/tests/test_proxy_health_node.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_proxy_health_node.py`：

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ProxyHealth


pytestmark = pytest.mark.unit


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_same_hash_different_node_coexist(session):
    """同一 proxy_hash 在不同 node 下可各存一行（唯一键是组合键）。"""
    session.add(ProxyHealth(proxy_hash="abc", node="nas", status="down"))
    session.add(ProxyHealth(proxy_hash="abc", node="US-macmini1", status="healthy"))
    session.commit()

    rows = session.query(ProxyHealth).filter(ProxyHealth.proxy_hash == "abc").all()
    assert len(rows) == 2
    by_node = {r.node: r.status for r in rows}
    assert by_node == {"nas": "down", "US-macmini1": "healthy"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py -v`
Expected: FAIL —— `TypeError: 'node' is an invalid keyword argument for ProxyHealth`（node 列尚不存在）。

- [ ] **Step 3: 实现——加 node 列与组合唯一键**

修改 `backend/app/models.py`，把 ProxyHealth 的 `__table_args__` 和列定义改为：

```python
class ProxyHealth(Base):
    """代理健康状态 —— 持久化代理连通性和失败类型。

    健康度是 (proxy_hash, node) 的属性：同一 IP 在不同出口节点可用性不同。
    """

    __tablename__ = "proxy_health"
    __table_args__ = (
        UniqueConstraint("proxy_hash", "node", name="uq_proxy_health_hash_node"),
    )

    id = Column(Integer, primary_key=True)
    proxy_hash = Column(String, index=True)
    node = Column(String, index=True, default="nas")   # 出口节点：nas / US-macmini1 ...
    proxy_redacted = Column(String)
    tier = Column(String, index=True)
    status = Column(String, default="unknown", index=True)  # healthy/degraded/blocked/down
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    consecutive_failures = Column(Integer, default=0)
    last_success_at = Column(DateTime)
    last_failure_at = Column(DateTime)
    last_checked_at = Column(DateTime, index=True)
    last_failure_code = Column(String, index=True)
    last_failure_detail = Column(Text)
    blocked_until = Column(DateTime, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

（仅新增 `node` 列、把 `__table_args__` 的唯一约束从单列 `proxy_hash` 改为组合 `(proxy_hash, node)`，其余列保持不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/models.py backend/tests/test_proxy_health_node.py
git commit -m "feat(proxy-health): ProxyHealth 加 node 维度，唯一键改 (proxy_hash, node)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: record_proxy_result / unhealthy_proxy_hashes 按 node 读写

**Files:**
- Modify: `backend/app/proxy_health.py:36-91`（record_proxy_result）、`:151-168`（unhealthy_proxy_hashes）
- Test: `backend/tests/test_proxy_health_node.py`（追加）

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_proxy_health_node.py` 末尾追加：

```python
from app.crawl_diagnostics import FailureInfo, STAGE_FETCH
from app.proxy_health import record_proxy_result, unhealthy_proxy_hashes


def _net_failure():
    return FailureInfo("network_timeout", STAGE_FETCH, "timeout", True, "retry")


def test_record_writes_per_node(session):
    """同一 proxy_url 在两个 node 上各记录独立健康行。"""
    url = "http://user:pass@1.2.3.4:8000"
    # nas 上连续 3 次失败 → down
    for _ in range(3):
        record_proxy_result(session, proxy_url=url, tier="residential",
                            success=False, failure=_net_failure(), node="nas")
    # mini 上成功
    record_proxy_result(session, proxy_url=url, tier="residential",
                        success=True, node="US-macmini1")
    session.commit()

    rows = session.query(ProxyHealth).all()
    assert len(rows) == 2
    by_node = {r.node: r.status for r in rows}
    assert by_node["nas"] == "down"
    assert by_node["US-macmini1"] == "healthy"


def test_unhealthy_is_node_scoped(session):
    """nas 标 down 的 IP，查 mini node 的黑名单不应包含它。"""
    url = "http://user:pass@1.2.3.4:8000"
    for _ in range(3):
        record_proxy_result(session, proxy_url=url, tier="residential",
                            success=False, failure=_net_failure(), node="nas")
    record_proxy_result(session, proxy_url=url, tier="residential",
                        success=True, node="US-macmini1")
    session.commit()

    from app.proxy_health import proxy_hash
    h = proxy_hash(url)
    assert h in unhealthy_proxy_hashes(session, node="nas")
    assert h not in unhealthy_proxy_hashes(session, node="US-macmini1")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py -v`
Expected: FAIL —— `record_proxy_result() got an unexpected keyword argument 'node'`。

- [ ] **Step 3: 实现——record_proxy_result 加 node**

修改 `backend/app/proxy_health.py` 的 `record_proxy_result`，签名加 `node` 参数，查询/创建按 `(proxy_hash, node)`：

```python
def record_proxy_result(
    session: Session,
    *,
    proxy_url: str | None,
    tier: str | None,
    success: bool,
    failure: FailureInfo | None = None,
    cooldown_sec: int = 600,
    node: str = "nas",
) -> ProxyHealth | None:
    if not proxy_url:
        return None
    now = datetime.utcnow()
    h = proxy_hash(proxy_url)
    endpoint_tier = _endpoint_tier(session, h)
    health_tier = endpoint_tier or _normalized_health_tier(tier)
    row = (session.query(ProxyHealth)
           .filter(ProxyHealth.proxy_hash == h, ProxyHealth.node == node)
           .first())
    if row is None:
        row = ProxyHealth(
            proxy_hash=h,
            node=node,
            proxy_redacted=redact_proxy(proxy_url),
            tier=health_tier,
        )
        session.add(row)
    # ↓ 以下逻辑（health_tier 更新、success/failure 分支）保持不变
    if health_tier:
        row.tier = health_tier
    row.proxy_redacted = redact_proxy(proxy_url)
    row.last_checked_at = now
    row.updated_at = now
    if success or not is_proxy_health_failure(failure):
        row.status = "healthy"
        row.success_count = (row.success_count or 0) + 1
        row.consecutive_failures = 0
        row.last_success_at = now
        row.last_failure_code = None
        row.last_failure_detail = None
        row.blocked_until = None
        return row

    row.failure_count = (row.failure_count or 0) + 1
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_failure_at = now
    if failure:
        row.last_failure_code = failure.code
        row.last_failure_detail = failure.detail[:2000] if failure.detail else None
    if failure and failure.code == "proxy_auth_failed":
        row.status = "blocked"
        row.blocked_until = None
    elif row.consecutive_failures >= 3:
        row.status = "down"
        row.blocked_until = now + timedelta(seconds=cooldown_sec)
    else:
        row.status = "degraded"
        row.blocked_until = now + timedelta(seconds=cooldown_sec)
    return row
```

- [ ] **Step 4: 实现——unhealthy_proxy_hashes 加 node 过滤**

修改 `backend/app/proxy_health.py` 的 `unhealthy_proxy_hashes`：

```python
def unhealthy_proxy_hashes(session: Session, node: str | None = None) -> set[str]:
    now = datetime.utcnow()
    query = session.query(ProxyHealth.proxy_hash).filter(
        or_(
            ProxyHealth.status == "blocked",
            ProxyHealth.status == "down",
            and_(
                ProxyHealth.status == "degraded",
                or_(
                    ProxyHealth.blocked_until.is_(None),
                    ProxyHealth.blocked_until > now,
                ),
            ),
        )
    )
    if node is not None:
        query = query.filter(ProxyHealth.node == node)
    rows = query.all()
    return {row[0] for row in rows if row[0]}
```

（`node=None` 时保持旧的"全节点合并"语义，作为向后兼容回退路径。）

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py -v`
Expected: PASS（4 个测试全过）。

- [ ] **Step 6: 回归——确认旧 health 测试不破**

Run: `cd backend && python -m pytest tests/test_frontier_and_proxy_health.py -v`
Expected: PASS。若旧测试调用 `record_proxy_result` 未传 node，会用默认 `"nas"`，行为不变。

- [ ] **Step 7: 提交**

```bash
git add backend/app/proxy_health.py backend/tests/test_proxy_health_node.py
git commit -m "feat(proxy-health): record/unhealthy 按 node 读写，节点间健康隔离

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: NODE_ID 常量 + _persistent_unhealthy_hashes 透传 node

**Files:**
- Modify: `backend/app/proxy_pool.py:504-520`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_proxy_health_node.py` 末尾追加：

```python
def test_node_id_env(monkeypatch):
    """NODE_ID 从环境变量读取，默认 nas。"""
    import importlib
    import app.proxy_pool as pp
    monkeypatch.setenv("NODE_ID", "US-macmini1")
    importlib.reload(pp)
    assert pp.NODE_ID == "US-macmini1"
    monkeypatch.delenv("NODE_ID", raising=False)
    importlib.reload(pp)
    assert pp.NODE_ID == "nas"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py::test_node_id_env -v`
Expected: FAIL —— `AttributeError: module 'app.proxy_pool' has no attribute 'NODE_ID'`。

- [ ] **Step 3: 实现**

在 `backend/app/proxy_pool.py` 顶部（`FAIL_THRESHOLD` 附近，约第 47 行后）加：

```python
NODE_ID = os.environ.get("NODE_ID", "nas")
```

修改 `_persistent_unhealthy_hashes`（约第 504 行）透传 NODE_ID：

```python
def _persistent_unhealthy_hashes() -> set[str]:
    try:
        from .db import SessionLocal
        from .proxy_health import unhealthy_proxy_hashes
    except Exception:
        return set()
    db = SessionLocal()
    try:
        return unhealthy_proxy_hashes(db, node=NODE_ID)
    except Exception:
        return set()
    finally:
        db.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_proxy_health_node.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/proxy_pool.py backend/tests/test_proxy_health_node.py
git commit -m "feat(proxy-pool): NODE_ID 常量，unhealthy 查询按本节点过滤

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 抓取/探针写健康时带 node + D2 强制 lease 默认值

**Files:**
- Modify: `backend/app/fetching.py:100`（proxy_lease_ttl_sec 默认值）、`:476-481`（record_proxy_result 调用）
- Modify: `backend/app/proxy_probe.py:149`（record_proxy_result 调用）

- [ ] **Step 1: D2 —— 改 proxy_lease_ttl_sec 默认值为 >0**

设计要求：默认走带并发锁的 lease 路径，而非无锁 `get_proxy`。`fetching.py:100` 当前是 `proxy_lease_ttl_sec: int = 0`。

读 `backend/app/fetching.py` 第 88-120 行确认 `FetchContext` 的字段上下文，然后改默认值为环境变量驱动（默认 300 秒）：

```python
    proxy_lease_ttl_sec: int = field(
        default_factory=lambda: int(os.environ.get("PROXY_LEASE_TTL_SEC", "300"))
    )
```

（注意：`FetchContext` 是 dataclass，可变默认值必须用 `field(default_factory=...)`；确认文件顶部已 `from dataclasses import dataclass, field` 与 `import os`，缺则补 import。设 `PROXY_LEASE_TTL_SEC=0` 可回退旧 get_proxy 路径——回滚开关。）

- [ ] **Step 2: fetching 写健康带 node**

修改 `backend/app/fetching.py` 第 476 行附近的 `record_proxy_result` 调用，传 `node=proxy_pool.NODE_ID`：

```python
        try:
            record_proxy_result(
                db,
                proxy_url=result.proxy,
                tier=fetcher.context.site.proxy_tier,
                success=result.ok or not proxy_failed,
                failure=result.failure,
                node=proxy_pool.NODE_ID,
            )
            db.commit()
```

（文件顶部已有 `from . import proxy_pool`，见 `fetching.py:19`，直接用。）

- [ ] **Step 3: proxy_probe 写健康带 node**

修改 `backend/app/proxy_probe.py` 第 149 行附近的 `record_proxy_result` 调用，传 node。先读 `backend/app/proxy_probe.py:140-160` 确认上下文，然后在调用里加：

```python
        from .proxy_pool import NODE_ID
        record_proxy_result(
            ...,            # 保持现有其它参数
            node=NODE_ID,
        )
```

- [ ] **Step 4: 回归测试**

Run: `cd backend && python -m pytest tests/test_proxy_probe.py tests/test_frontier_and_proxy_health.py tests/test_proxy_config_db.py -v`
Expected: PASS。

- [ ] **Step 5: 全量冒烟（确保 import 无误）**

Run: `cd backend && python -c "import app.fetching, app.proxy_probe, app.proxy_pool; print('imports ok')"`
Expected: `imports ok`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/fetching.py backend/app/proxy_probe.py
git commit -m "feat(fetching): 默认走代理租约(D2)+写健康带node(D4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: claim_job 租户路由（allowlist/blocklist + workspace_sites 判定）

**Files:**
- Modify: `backend/app/runner.py:207-249`（claim_job）
- Test: `backend/tests/test_claim_job_workspace.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_claim_job_workspace.py`：

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
from app.db import Base
from app.models import CrawlJob, Site, WorkspaceSite


pytestmark = pytest.mark.unit


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # claim_job 用 session_scope() → 绑定到测试引擎
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _site(s, name):
    site = Site(site=name, base_url=f"https://{name}.com", platform="generic",
                enabled=True)
    s.add(site)


def test_allowlist_only_claims_matching_workspace(session):
    """mini：WORKSPACE_ALLOWLIST 只领指定租户的 job（按 requested_by_workspace_id）。"""
    from app.runner import claim_job
    _site(session, "siteA")
    _site(session, "siteB")
    session.add(CrawlJob(site="siteA", status="pending", trigger="manual",
                         requested_by_workspace_id=7))
    session.add(CrawlJob(site="siteB", status="pending", trigger="manual",
                         requested_by_workspace_id=99))
    session.commit()

    jid = claim_job("mini-1", workspace_allowlist=(7,))
    job = session.get(CrawlJob, jid)
    assert job.site == "siteA"          # 只领 ws=7 的
    assert claim_job("mini-1", workspace_allowlist=(7,)) is None  # ws=99 不领


def test_blocklist_skips_matching_workspace(session):
    """NAS：WORKSPACE_BLOCKLIST 不领指定租户的 job。"""
    from app.runner import claim_job
    _site(session, "siteA")
    session.add(CrawlJob(site="siteA", status="pending", trigger="manual",
                         requested_by_workspace_id=7))
    session.commit()
    assert claim_job("nas", workspace_blocklist=(7,)) is None


def test_scheduled_null_routed_via_workspace_sites(session):
    """scheduled job 的 requested_by_workspace_id 为 NULL，靠 workspace_sites 映射判定。"""
    from app.runner import claim_job
    _site(session, "siteA")
    session.add(WorkspaceSite(workspace_id=7, site="siteA"))
    session.add(CrawlJob(site="siteA", status="pending", trigger="scheduled",
                         requested_by_workspace_id=None))
    session.commit()

    jid = claim_job("mini-1", workspace_allowlist=(7,))
    assert jid is not None
    assert session.get(CrawlJob, jid).site == "siteA"
```

> 注意：若 `Site` 模型必填字段与上面不符，先读 `backend/app/models.py` 的 `class Site` 调整 `_site()` 构造参数。`crawl_preflight_issue` 可能因 site 配置不全而 skip job——如测试因 preflight skip 失败，在 `_site` 里补齐 enabled/tracking 等必要字段，或参考既有用 claim_job 的测试 fixture。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_claim_job_workspace.py -v`
Expected: FAIL —— `claim_job() got an unexpected keyword argument 'workspace_allowlist'`。

- [ ] **Step 3: 实现 claim_job 过滤**

读 `backend/app/runner.py:207-249` 确认当前 claim_job 全貌。修改签名与过滤逻辑：

```python
def claim_job(worker_id: str,
              trigger_allowlist: tuple[str, ...] | None = None,
              workspace_allowlist: tuple[int, ...] | None = None,
              workspace_blocklist: tuple[int, ...] | None = None) -> int | None:
    """worker 原子领取最旧的 pending 任务，返回 job_id 或 None。

    workspace_allowlist：仅领这些租户的 job（mini 用）。
    workspace_blocklist：不领这些租户的 job（NAS 用）。
    租户判定：requested_by_workspace_id 命中；为 NULL 时按 workspace_sites 映射
    （job.site 属于该租户的站点）补判。
    """
    from .models import WorkspaceSite
    with session_scope() as s:
        skipped = 0
        while True:
            priority = case(
                (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), 0),
                (CrawlJob.trigger == "tracking_add", 1),
                else_=2,
            )
            query = s.query(CrawlJob).filter(CrawlJob.status == "pending")
            running_alias = CrawlJob.__table__.alias("running_jobs")
            query = query.filter(~exists().where(
                running_alias.c.status == "running"
            ).where(running_alias.c.site == CrawlJob.site))
            if trigger_allowlist:
                query = query.filter(CrawlJob.trigger.in_(trigger_allowlist))

            # 租户路由：构造"job 属于 workspace 集合"的谓词
            def _belongs_to(ws_ids: tuple[int, ...]):
                site_subq = (s.query(WorkspaceSite.site)
                             .filter(WorkspaceSite.workspace_id.in_(ws_ids)))
                return or_(
                    CrawlJob.requested_by_workspace_id.in_(ws_ids),
                    and_(CrawlJob.requested_by_workspace_id.is_(None),
                         CrawlJob.site.in_(site_subq)),
                )

            if workspace_allowlist:
                query = query.filter(_belongs_to(workspace_allowlist))
            if workspace_blocklist:
                query = query.filter(~_belongs_to(workspace_blocklist))

            high_priority_touched_at = case(
                (CrawlJob.trigger.in_(HIGH_PRIORITY_TRIGGERS), CrawlJob.created_at),
                else_=datetime(1970, 1, 1),
            )
            job = query.order_by(priority, high_priority_touched_at.desc(),
                                 CrawlJob.id).first()
            if job is None:
                return None
            site = s.query(Site).filter(Site.site == job.site).first()
            preflight = crawl_preflight_issue(site, trigger=job.trigger, session=s)
            if preflight is not None:
                _skip_job(s, job, preflight)
                s.flush()
                skipped += 1
                if skipped >= 50:
                    return None
                continue
            now = datetime.utcnow()
            res = s.execute(
                update(CrawlJob)
                .where(CrawlJob.id == job.id, CrawlJob.status == "pending")
                .values(status="running", worker=worker_id,
                        started_at=now, heartbeat_at=now))
            return job.id if res.rowcount == 1 else None
```

（确认 `and_` 已从 sqlalchemy import——`runner.py:16` 已有 `from sqlalchemy import and_, case, exists, or_, update`，齐全。）

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_claim_job_workspace.py -v`
Expected: PASS（3 个测试）。如遇 preflight skip 问题，按 Step 1 注释调整 `_site` fixture。

- [ ] **Step 5: 回归**

Run: `cd backend && python -m pytest tests/test_crawler_limit.py tests/test_worker_diagnostics.py -v`
Expected: PASS（claim_job 旧调用未传新参数，默认 None，行为不变）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/runner.py backend/tests/test_claim_job_workspace.py
git commit -m "feat(runner): claim_job 支持租户 allowlist/blocklist 路由(含scheduled映射)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: worker 读 workspace env 传入 claim_job

**Files:**
- Modify: `backend/app/worker.py:48-51`（env 解析）、`:279`（claim_job 调用）

- [ ] **Step 1: 实现 env 解析**

在 `backend/app/worker.py` 的 `TRIGGER_ALLOWLIST` 定义（第 48 行）后加：

```python
def _env_int_tuple(name: str) -> tuple[int, ...] | None:
    raw = os.environ.get(name, "")
    ids = tuple(int(x.strip()) for x in raw.split(",") if x.strip().isdigit())
    return ids or None


WORKSPACE_ALLOWLIST = _env_int_tuple("WORKSPACE_ALLOWLIST")
WORKSPACE_BLOCKLIST = _env_int_tuple("WORKSPACE_BLOCKLIST")
```

- [ ] **Step 2: 传入 claim_job**

修改 `backend/app/worker.py:279`：

```python
            job_id = claim_job(WORKER_ID, TRIGGER_ALLOWLIST,
                               workspace_allowlist=WORKSPACE_ALLOWLIST,
                               workspace_blocklist=WORKSPACE_BLOCKLIST)
```

- [ ] **Step 3: 冒烟测试**

Run: `cd backend && WORKSPACE_ALLOWLIST=7,8 python -c "import app.worker as w; print(w.WORKSPACE_ALLOWLIST)"`
Expected: `(7, 8)`。

Run: `cd backend && python -c "import app.worker as w; print(w.WORKSPACE_ALLOWLIST, w.WORKSPACE_BLOCKLIST)"`
Expected: `None None`。

- [ ] **Step 4: 提交**

```bash
git add backend/app/worker.py
git commit -m "feat(worker): 读 WORKSPACE_ALLOWLIST/BLOCKLIST env 传入 claim_job

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 代理租约并发防撞单测（锁死 D2 行为）

**Files:**
- Test: `backend/tests/test_proxy_lease_concurrency.py`（新建）

- [ ] **Step 1: 写测试**

先读 `backend/app/proxy_pool.py:589-646`（`_try_create_proxy_lease`）与 `ProxyEndpoint`/`ProxyLease` 模型确认建表所需字段，然后新建 `backend/tests/test_proxy_lease_concurrency.py`：

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db_mod
from app.db import Base
from app.models import ProxyEndpoint
from app import proxy_pool


pytestmark = pytest.mark.unit


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_max_concurrency_one_blocks_second_lease(session):
    """max_concurrency=1 的 endpoint：第一次 lease 成功，未释放时第二次拿不到。"""
    ep = ProxyEndpoint(
        proxy_hash="h1",
        endpoint_type="residential",
        max_concurrency=1,
        active=True,
    )
    session.add(ep)
    session.commit()

    from app.proxy_pool import _try_create_proxy_lease, ProxyEntry
    cand = [ProxyEntry(url="http://1.2.3.4:8000", tier="residential", id=ep.id)]

    h1 = _try_create_proxy_lease(cand, site="x", job_id=1, worker="w1", ttl_sec=300)
    assert h1 is not None
    h2 = _try_create_proxy_lease(cand, site="x", job_id=2, worker="w2", ttl_sec=300)
    assert h2 is None     # 并发上限=1，第二次被行锁+计数挡住
```

> 注意：`_try_create_proxy_lease` 内部用 `SessionLocal()` 自建 session（见 proxy_pool.py:608），故必须 monkeypatch `db_mod.SessionLocal`。若 `ProxyEndpoint` 必填字段与上面不符，读模型补齐（如 `proxy_hash`、`url` 等）。SQLite 的 `with_for_update()` 是 no-op 但计数逻辑仍生效，足以验证并发上限。

- [ ] **Step 2: 运行**

Run: `cd backend && python -m pytest tests/test_proxy_lease_concurrency.py -v`
Expected: PASS。若因 ProxyEndpoint 字段不全报错，按提示补字段后重跑。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_proxy_lease_concurrency.py
git commit -m "test(proxy): 锁死 max_concurrency 并发租约防撞行为

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: ProxyHealth 数据迁移脚本（回填 node='nas'）

**Files:**
- Create: `backend/scripts/migrate_proxy_health_node.py`

- [ ] **Step 1: 写迁移脚本**

新建 `backend/scripts/migrate_proxy_health_node.py`：

```python
"""为 proxy_health 表加 node 列并回填现有行为 'nas'，重建唯一约束。

幂等：可重复运行。在 NAS PG 上执行一次（阶段1）。
用法：cd backend && python scripts/migrate_proxy_health_node.py
"""
from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        # 1. 加列（IF NOT EXISTS：PG 16 支持）
        conn.execute(text(
            "ALTER TABLE proxy_health ADD COLUMN IF NOT EXISTS node VARCHAR"
        ))
        # 2. 回填历史行
        conn.execute(text(
            "UPDATE proxy_health SET node='nas' WHERE node IS NULL"
        ))
        # 3. 删旧的单列 proxy_hash 唯一约束（生产库可能是 SQLAlchemy/PG 自动命名）
        conn.execute(text("""
            DO $$
            DECLARE
                r record;
            BEGIN
                FOR r IN
                    SELECT c.conname
                    FROM pg_constraint c
                    WHERE c.conrelid = 'proxy_health'::regclass
                      AND c.contype = 'u'
                      AND c.conname <> 'uq_proxy_health_hash_node'
                      AND (
                          SELECT array_agg(a.attname ORDER BY u.ord)
                          FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                          JOIN pg_attribute a
                            ON a.attrelid = c.conrelid
                           AND a.attnum = u.attnum
                      ) = ARRAY['proxy_hash']
                LOOP
                    EXECUTE format('ALTER TABLE proxy_health DROP CONSTRAINT %I', r.conname);
                END LOOP;
            END $$;
        """))
        # 4. 建新组合唯一约束（若不存在）
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_proxy_health_hash_node'
                ) THEN
                    ALTER TABLE proxy_health
                        ADD CONSTRAINT uq_proxy_health_hash_node UNIQUE (proxy_hash, node);
                END IF;
            END $$;
        """))
        # 5. node 索引
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_proxy_health_node ON proxy_health (node)"
        ))
    print("proxy_health node 迁移完成")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 本地语法检查**

Run: `cd backend && python -c "import ast; ast.parse(open('scripts/migrate_proxy_health_node.py').read()); print('syntax ok')"`
Expected: `syntax ok`。

（实际执行在 Task 11 的 NAS 阶段；此处只创建脚本。SQLite 测试库由 `Base.metadata.create_all` 直接建新 schema，无需此迁移。）

- [ ] **Step 3: 全量回归（代码改动收尾）**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿（或与改动前同样的已知 skip/xfail，无新增失败）。

> 若有非本计划相关的预存失败，记录下来但不阻塞——对比改动前 `git stash` 基线确认非本次引入。

- [ ] **Step 4: 提交**

```bash
git add backend/scripts/migrate_proxy_health_node.py
git commit -m "chore(migrate): proxy_health node 列迁移脚本(回填nas+组合唯一键)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## ⚠️ 部署阶段分界线（Task 9 起在真实主机执行，严格按灰度顺序）

以下任务改动生产环境。**每个 Task 完成后人工确认再进入下一个**。回滚方案见设计文档第 9 节。

### NAS 接入方式（关键 —— Task 9-12 的所有 NAS 命令都适用）

NAS（`vocserver`，Tailscale `100.116.163.64`）有两个 SSH 入口，**用途不同**：

- **`ssh root@100.116.163.64`（直连，免密）** → 进的是 **Tailscale sidecar 容器**（只有 `nc`，无 docker/psql）。**仅用于网络连通验证**（如从这里 `nc -z 127.0.0.1 5432`），**不能跑 docker/psql/迁移**。
- **`ssh -J root@100.116.163.64 shilong@127.0.0.1`（跳板，内层 shilong 密码由用户提供）** → 才是能 `sudo docker` / `psql` 的真正部署环境。**本计划所有 `docker exec ...` / `psql ...` / 改 compose / 跑迁移命令都必须经此跳板执行。**

**执行约定**：下文写成 `docker exec smart-crawler-pg psql ...` 的命令，实际执行时包成：
```bash
ssh -tt -J root@100.116.163.64 shilong@127.0.0.1 'sudo docker exec smart-crawler-pg psql ...'
```
（`-tt` 给 sudo 分配 TTY；`shilong` 不在 docker 组，故 `sudo docker`；**别加 `-o BatchMode=yes`**，会禁掉内层密码输入；**别反复无密码重试**，会触发 `Too many authentication failures` 锁连接。scp 用 `scp -O -J root@100.116.163.64 shilong@127.0.0.1:...`。）

或直接调用 `smart-crawler-nas-deploy` skill，它封装了这套跳板 + sudo docker。

> 2026-06-24 实测：`root@100.116.163.64` 可进 Tailscale sidecar；sidecar 内无
> Docker socket、无 `/volume1` 挂载，不能改 compose/PG 文件。当前 key 直连
> `solvea|shilong|root@192.168.1.80` 与从 sidecar 跳
> `root|admin|shilong|solvea|wangxiaokang@172.17.0.1/192.168.1.80` 均被拒。
> 继续 Task 9 Step 1/3/Task 10 需要 NAS host SSH 凭据或可用 DB 管理凭据。
> 用户补充 `solvea@192.168.1.80` 密码为 `solvea` 后，2026-06-24 再测仍被 SSH
> 拒绝；同密码用于 `smart_crawler/sc_worker/postgres/sc` DB 用户也均认证失败。

---

## Task 9: 【NAS·阶段0】PG 暴露到 Tailscale + worker 专用角色

**Files:**
- Modify: `docker-compose.yml`（postgres ports）
- 运维：NAS 上的 pg_hba.conf、PG 角色

**前置**：经跳板 `ssh -J root@100.116.163.64 shilong@127.0.0.1` 登录（见上方"NAS 接入方式"），或用 `smart-crawler-nas-deploy` skill。

- [ ] **Step 1: 建 worker 专用 PG 角色（最小权限）**

在 NAS PG 容器内执行（角色仅 crawl 相关表 DML，非 superuser）：

```sql
CREATE ROLE sc_worker LOGIN PASSWORD '<强密码>';
GRANT CONNECT ON DATABASE smart_crawler TO sc_worker;
GRANT USAGE ON SCHEMA public TO sc_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sc_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sc_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sc_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sc_worker;
```

- [x] **Step 2: postgres 端口对 Tailscale 可达**

优先方案：修改 `docker-compose.yml` 的 postgres 服务，取消注释 ports 并绑 Tailscale IP：

```yaml
  postgres:
    ...
    ports:
      - "100.116.163.64:5432:5432"   # 仅 Tailscale 网段可达，不开公网
```

2026-06-24 先用 Tailscale sidecar 完成等价的 tailnet-only TCP 转发：

```bash
ssh root@100.116.163.64 \
  'tailscale serve --yes --bg --tcp 5432 tcp://172.19.0.2:5432'
```

已从本机、mini1、mini4 验证 `100.116.163.64:5432` TCP connect 成功。`172.19.0.2`
为 smart-crawler PG 容器所在 Docker bridge 目标；转发到该 IP 后，连接已通过
`pg_hba`，当前阻塞点变为 DB 密码认证失败。

- [ ] **Step 3: pg_hba 限网段**

在 NAS PG 的 `pg_hba.conf` 增加（放行整段 Tailscale CGNAT，因 mini1 有两个 100.x 接口）：

```
host    smart_crawler    sc_worker    100.64.0.0/10    scram-sha-256
host    smart_crawler    sc_worker    127.0.0.1/32     scram-sha-256  # 若继续用 tailscale serve TCP 转发
```

重载：`docker exec smart-crawler-pg psql -U smart_crawler -c "SELECT pg_reload_conf();"`

- [ ] **Step 4: 重启 postgres 应用 ports**

```bash
docker compose up -d postgres
```

- [ ] **Step 5: 验证（从 mini1 + mini4 连）**

在 mini1 与 mini4 上执行：

```bash
ssh solvea@100.75.94.90 'nc -z -G 5 100.116.163.64 5432 && echo REACHABLE'
ssh solvea@100.72.33.57 'nc -z -G 5 100.116.163.64 5432 && echo REACHABLE'
```

Expected: `REACHABLE`。

> 此步只加能力、不改抓取行为，NAS worker 照常。零风险。

---

## Task 10: 【NAS·阶段1】部署代码改动 + 跑迁移 + 设 NODE_ID

**前置**：Task 1-8 代码已合入并推送。

- [ ] **Step 1: 部署后端代码到 NAS**

用 `smart-crawler-nas-deploy` skill 推送 backend 代码到 NAS。

- [ ] **Step 2: 跑 proxy_health 迁移**

脚本会自动删除旧的单列 `proxy_hash` 唯一约束（兼容 `uq_proxy_health_hash` 或 PG 自动命名的 `proxy_health_proxy_hash_key`），再创建 `(proxy_hash, node)` 组合唯一约束。迁移前可先查询现有唯一约束做记录：

```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c \
  "SELECT conname FROM pg_constraint WHERE conrelid='proxy_health'::regclass AND contype='u';"
```
在 NAS 容器内：

```bash
docker exec -w /app/backend smart-crawler python scripts/migrate_proxy_health_node.py
```

Expected: `proxy_health node 迁移完成`。

验证：
```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c "\d proxy_health" | grep node
```
Expected: 看到 `node` 列与 `uq_proxy_health_hash_node` 约束。

- [ ] **Step 3: NAS worker 容器设 NODE_ID=nas**

修改 `docker-compose.yml` 的 worker 模板与各 worker 服务 environment，加：

```yaml
      - NODE_ID=nas
```

- [ ] **Step 4: 重启 NAS 服务，观察无回归**

```bash
docker compose up -d
docker compose logs -f --tail=50 worker_1
```

观察：现有抓取正常、proxy_leases 有租约写入、无报错。手动触发一个非 xiaokang 站点抓一次确认 OK。

- [ ] **Step 5: 确认 D2 lease 生效**

```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler \
  -c "SELECT count(*) FROM proxy_leases WHERE created_at > now() - interval '10 min';"
```

Expected: >0（说明抓取走了租约路径，而非旧 get_proxy）。

> 此步 NAS worker 仍跑全部租户（尚未加 BLOCKLIST），保证不中断。

---

## Task 11: 【mini1 + mini4·阶段2】装机 + launchd 模板

**Files:**
- Create: `deploy/io.smartcrawler.worker.plist`
- Create: `scripts/mini_bootstrap.sh`
- Create: `scripts/install_mini_launchd.sh`

- [x] **Step 1: 写 launchd plist 模板**

新建 `deploy/io.smartcrawler.worker.plist`（占位 `__N__` 由部署脚本替换）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.smartcrawler.worker-__N__</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/solvea/smart-crawler/.venv/bin/python</string>
    <string>-m</string><string>app.worker</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/solvea/smart-crawler/backend</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SC_ENV_FILE</key><string>__ENV_FILE__</string>
    <key>DYLD_LIBRARY_PATH</key><string>/opt/homebrew/opt/expat/lib</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/solvea/smart-crawler/logs/worker-__N__.out.log</string>
  <key>StandardErrorPath</key><string>/Users/solvea/smart-crawler/logs/worker-__N__.err.log</string>
</dict>
</plist>
```

> launchd 不读 shell 的 `.env`。两种方案择一：(a) 在 plist 的 `EnvironmentVariables` 里逐条列出所有 env；(b) 在 `app/worker.py` 的 `main()` 开头加载 `SC_ENV_FILE` 指向的文件。**本计划采用 (b)**——见 Step 2。

- [x] **Step 2: worker 支持 SC_ENV_FILE 加载**

**关键时机问题**：`WORKER_ID`/`NODE_ID`（proxy_pool.py）等是**模块级常量，在 import 时即求值**。`NODE_ID` 在 `app.proxy_pool` 里，而 worker.py 顶部 `from .runner import ...` 会**传递性 import** proxy_pool。因此 env 文件必须在**任何 app 模块被 import 之前**加载。

做法：新建一个最早执行的轻量加载器 `backend/app/envfile.py`：

```python
"""从 SC_ENV_FILE 指定文件加载 KEY=VALUE 到 os.environ。

launchd 不继承 shell env，故 worker 进程靠此加载配置。
必须在任何读取 env 的 app 模块被 import 前调用。
"""
from __future__ import annotations

import os


def load_env_file() -> None:
    path = os.environ.get("SC_ENV_FILE")
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
```

然后把 `backend/app/worker.py` 的**最顶部**（在 `from __future__` 之后、其它 `from .xxx` import 之前）改为：

```python
from __future__ import annotations

import os as _os  # noqa: E402  (env 加载需先于 app 模块 import)
if _os.environ.get("SC_ENV_FILE"):
    from .envfile import load_env_file
    load_env_file()

import logging
import signal
import socket
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import or_

from .analytics import recompute
# ...（其余 import 保持原样）
```

> `setdefault`：真实环境已设的变量优先于文件。`app.envfile` 只依赖标准库、不 import 任何 app 模块，故可安全最早执行。

测试：
```bash
cd backend && printf 'WORKER_ID=test-x\nNODE_ID=mini-test\n' > /tmp/sc.env
SC_ENV_FILE=/tmp/sc.env python -c "import app.worker as w; import app.proxy_pool as p; print(w.WORKER_ID, p.NODE_ID)"
```
Expected: `test-x mini-test`（证明 env 在 proxy_pool 的 NODE_ID 求值前已加载）。

提交：
```bash
git add backend/app/worker.py backend/app/envfile.py deploy/io.smartcrawler.worker.plist
git commit -m "feat(worker): SC_ENV_FILE 最早加载(launchd用)+launchd plist模板

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [x] **Step 3: 写装机脚本**

新建 `scripts/mini_bootstrap.sh`：

```bash
#!/usr/bin/env bash
# mini 一次性装机。在 mini 上执行：bash mini_bootstrap.sh
set -euo pipefail

USER_HOME="/Users/solvea"
APP="$USER_HOME/smart-crawler"

# 1. Python 3.12（mini 默认 python3 是 3.14，C 扩展轮子不兼容）
brew install python@3.12

# 2. 目录
mkdir -p "$APP/backend" "$APP/logs"

# 3. venv（代码由 deploy_mini.sh 先 rsync 推来）
/opt/homebrew/bin/python3.12 -m venv "$APP/.venv"
source "$APP/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$APP/backend/requirements.txt"

# 4. Playwright 浏览器（用 macOS 真 Chrome 通道；Chromium 兜底）
playwright install chromium

echo "bootstrap complete: $APP"
```

- [x] **Step 4: 执行装机（mini1 + mini4）**

先 rsync 代码（见 Task 13 的脚本，或手动 rsync 一次 backend/），再：

```bash
ssh solvea@100.75.94.90 'bash ~/smart-crawler/scripts/mini_bootstrap.sh'
ssh solvea@100.72.33.57 'bash ~/smart-crawler/scripts/mini_bootstrap.sh'
```

Expected: 结尾打印 `bootstrap complete`。2026-06-24 已在 mini1 + mini4 完成；mini1 的 Homebrew Python 3.12 `pyexpat` 需通过 Homebrew `expat` + `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib` 规避，已写入脚本与 plist。

- [x] **Step 5: 安装 mini env 草稿 + launchd plist（不启动）**

在 mini1 和 mini4 上分别创建 `/Users/solvea/.smart-crawler-1.env`、`/Users/solvea/.smart-crawler-2.env`（权限 600），并生成 `~/Library/LaunchAgents/io.smartcrawler.worker-1.plist`、`worker-2.plist`。此阶段**只安装不 load**，避免 NAS PG 未开放前 worker 反复失败或误用本地 SQLite。

```bash
ssh solvea@100.75.94.90 'NODE_ID=US-macmini1 WORKER_PREFIX=US-macmini1 bash ~/smart-crawler/scripts/install_mini_launchd.sh'
ssh solvea@100.72.33.57 'NODE_ID=US-macmini4 WORKER_PREFIX=US-macmini4 bash ~/smart-crawler/scripts/install_mini_launchd.sh'
```

2026-06-24 已验证：两台各 2 个 env + plist 均存在，权限 `600`，`SC_ENV_FILE` 分别指向 `~/.smart-crawler-1.env` / `~/.smart-crawler-2.env`，launchd 状态为 `not-loaded`。

- [ ] **Step 6: NAS 就绪后填真实 env 并启动 4 个 launchd worker**

先查 `xiaokang` workspace_id（在 NAS 上）并创建 `sc_worker` DB 密码后，把每台两个 env 文件补齐：

```bash
# 在 ~/.smart-crawler-1.env 和 ~/.smart-crawler-2.env 中取消注释/填入：
DATABASE_URL=postgresql+psycopg://sc_worker:<密码>@100.116.163.64:5432/smart_crawler
WORKSPACE_ALLOWLIST=<X>
ANTHROPIC_API_KEY=<key>
```

然后启动：

```bash
ssh solvea@100.75.94.90 'NODE_ID=US-macmini1 WORKER_PREFIX=US-macmini1 bash ~/smart-crawler/scripts/install_mini_launchd.sh --load'
ssh solvea@100.72.33.57 'NODE_ID=US-macmini4 WORKER_PREFIX=US-macmini4 bash ~/smart-crawler/scripts/install_mini_launchd.sh --load'
```

- [ ] **Step 7: 验证 mini1/mini4 领到 xiaokang 的 job**

先在 NAS 加 BLOCKLIST 让 xiaokang 只走 mini（编辑 docker-compose.yml worker 服务加 `WORKSPACE_BLOCKLIST=<X>`，`docker compose up -d`）。

手动触发 xiaokang 某站点抓取，然后查：

```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c \
  "SELECT id, site, worker, status FROM crawl_jobs
   WHERE requested_by_workspace_id=<X> ORDER BY id DESC LIMIT 5;"
```

Expected: `worker` 列为 `US-macmini1-1` 或 `US-macmini4-1`，status 推进到 running/success。

验证健康隔离：
```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c \
  "SELECT node, count(*) FROM proxy_health GROUP BY node;"
```
Expected: 出现 `US-macmini1` 与 `US-macmini4` 行（与 `nas` 行并存、独立）。

- [ ] **Step 8: 验证数据回流 NAS**

在 NAS 前端/报表确认 xiaokang 的抓取数据已入库（products_count 增长）。

---

## Task 12: 【mini1 + mini4·阶段3】放开 scheduled + 每台起第 2 个 worker + 24h 观察

- [ ] **Step 1: mini env 放开 trigger（接 scheduled）**

mini 的 env 当前未设 `TRIGGER_ALLOWLIST`（worker.py 默认 None=全部 trigger），故已接 scheduled。确认 xiaokang 站点的定时任务能被 mini 领取：

```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c \
  "SELECT worker, trigger, count(*) FROM crawl_jobs
   WHERE requested_by_workspace_id=<X> OR site IN
     (SELECT site FROM workspace_sites WHERE workspace_id=<X>)
   GROUP BY worker, trigger;"
```
Expected: scheduled 行的 worker 是 `US-macmini1-*` 或 `US-macmini4-*`。

- [x] **Step 2: 每台准备第 2 个 worker**

已采用每个 worker 独立 env 文件：mini1 为 `US-macmini1-1`、`US-macmini1-2`，mini4 为 `US-macmini4-1`、`US-macmini4-2`；同机共享同一个 `NODE_ID`，但 `WORKER_ID` 唯一。实际 `bootstrap` 在 Task 11 Step 6 的 NAS 就绪后统一执行。

- [ ] **Step 2.5: 验证两 worker 共用一个出口节点视角**

```bash
docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler -c \
  "SELECT DISTINCT worker FROM crawl_jobs WHERE requested_by_workspace_id=<X>;"
```
Expected: 出现 `US-macmini1-1`、`US-macmini1-2`、`US-macmini4-1`、`US-macmini4-2`；proxy_health 按机器只有 `US-macmini1` 与 `US-macmini4` 两个 node 行（同机两 worker 共享健康视角）。

- [ ] **Step 3: 24h 观察检查表**

逐项确认：
- 数据完整性：xiaokang 站点 products_count 与历史持平或更好。
- 429 率：`SELECT worker, failure_code, count(*) FROM crawl_jobs WHERE worker LIKE 'US-macmini%' AND finished_at > now()-interval '24h' GROUP BY worker, failure_code;` —— 无异常飙升。
- 租约不超并发：`SELECT endpoint_id, count(*) FROM proxy_leases WHERE released_at IS NULL AND expires_at > now() GROUP BY endpoint_id;` —— 每行 ≤ 对应 endpoint 的 max_concurrency。
- 健康隔离生效：对比 `nas`、`US-macmini1`、`US-macmini4` 三个 node 下同一 IP 的 status 可不同。

- [ ] **Step 4: 记录观察结论**

把 24h 观察结果记入运维日志，决定是否进入阶段4。

---

## Task 13: rsync 部署脚本 + 【阶段4】扩大范围（验证通过后）

**Files:**
- Create: `scripts/deploy_mini.sh`

- [ ] **Step 1: 写 rsync 部署脚本**

新建 `scripts/deploy_mini.sh`：

```bash
#!/usr/bin/env bash
# 推送 backend 代码到 mini 并重启 worker。在本机/NAS 执行。
set -euo pipefail

MINIS=("solvea@100.75.94.90" "solvea@100.72.33.57")     # mini1 + mini4

for MINI in "${MINIS[@]}"; do
  echo "==> 部署到 $MINI"
  rsync -az --delete \
    --exclude='.venv' --exclude='data' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='*.env' --exclude='proxies*.txt' \
    --exclude='logs' \
    backend/ "$MINI":~/smart-crawler/backend/
  rsync -az scripts/ "$MINI":~/smart-crawler/scripts/
  rsync -az deploy/ "$MINI":~/smart-crawler/deploy/

  ssh "$MINI" 'cd ~/smart-crawler && source .venv/bin/activate && \
    pip install -q -r backend/requirements.txt && \
    for n in 1 2; do \
      launchctl kickstart -k gui/$(id -u)/io.smartcrawler.worker-$n 2>/dev/null || true; \
    done'
  echo "==> $MINI 完成"
done
```

- [ ] **Step 2: 语法检查 + 试运行**

```bash
bash -n scripts/deploy_mini.sh && echo "syntax ok"
bash scripts/deploy_mini.sh    # 实际推送一次，确认 worker 重启无误
```

Expected: `syntax ok`，且 mini worker 重启后正常领 job。

- [ ] **Step 3: 提交脚本**

```bash
git add scripts/deploy_mini.sh
git commit -m "chore(deploy): mini rsync 部署脚本(多机循环+排除密钥/代理文件)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: 【阶段4】扩大范围（人工决策点）**

验证通过后，二选一：
- **扩租户**：mini env 的 `WORKSPACE_ALLOWLIST` 追加更多 workspace_id，NAS 对应加 BLOCKLIST。
- **全量**：去掉 mini 的 `WORKSPACE_ALLOWLIST`（领全部）、去掉 NAS 的 `WORKSPACE_BLOCKLIST`，再逐步把 NAS worker 的 `WORKER_THREADS` 调 0 / 停 worker 容器（保留一键重启兜底）。

每一步后用 Task 12 Step 3 的检查表确认。**动 NAS worker 是最后动作，可随时回退**（恢复 worker 容器 + 去掉 BLOCKLIST）。

---

## 收尾：完成后的验证

- [ ] 运行 `superpowers:verification-before-completion`：跑全量测试、确认部署阶段每步的 Expected 都已实测达成。
- [ ] 更新记忆：把"分布式 mini 抓取已上线（xiaokang 验证）"记入项目记忆，关联 [[nas-deploy-via-tailscale]] [[429-rootcause-and-ratelimit-fix]]。
- [ ] 若需要，用 `superpowers:finishing-a-development-branch` 决定合并/PR。
