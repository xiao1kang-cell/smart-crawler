# spine 队列计费 + 心跳续约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 spine 异步队列两个缺口——worker execute 精确到 api_key 记账,worker 心跳续约让 reclaim 只回收真崩溃(不误伤活着的长抓)。

**Architecture:** SpineJob 加 `api_key_id`/`heartbeat_at` 两列(`_migrate` 自动 ADD COLUMN);enqueue 链路(REST/MCP)持久化 api_key_id;execute_job 成功后 `record_usage` 记账 + 期间后台线程心跳续约;reclaim 判据从 `started_at` 改 `heartbeat_at`。不碰 SP1 同步入口与电商队列。

**Tech Stack:** FastAPI + SQLAlchemy、FastMCP、threading、pytest。

**Spec:** `docs/superpowers/specs/2026-06-12-spine-queue-billing-heartbeat-design.md`

**分支(待建):** `feat/spine-queue-billing-heartbeat`

**关键复用点(已核实):**
- `record_usage(api_key_id, endpoint, record_count, bytes_returned, duration_ms, credits_used=None, workspace_id=None)`(billing.py:48);`api_key_id` 可为 None(内部 `s.get(ApiKey, api_key_id) if api_key_id else None`),`workspace_id` 可独立传。`Usage` 模型有 `api_key_id`/`endpoint`/`credits_used`/`record_count`/`workspace_id`(models.py:459+)。
- `spine.resolve(...)` 返回 dict 含 `record_id` 和 `credits_used`(warehouse 命中=0,live=2,spine.py:218)。
- `db.py::_migrate()` 通用自动迁移:遍历模型列,缺列即 `ALTER TABLE ADD COLUMN`。给 SpineJob 加列后自动补到已存在的 spine_jobs 表,**无需手写 ALTER**。
- `claim_job` 现用 `update(SpineJob).where(id=, status=="pending").values(status="running", worker=, started_at=now)`(spine_queue.py)。
- `execute_job` 现在 `with session_scope()` 读 job 字段、try 内 resolve、成功置 success、except 调 `_handle_failure`。
- `reclaim_stale_jobs` 现判 `or_(started_at < cutoff, started_at.is_(None))`。
- REST `custom_scrape_async`:有 `_require_scope`、`_v2_ws_id`;`_api_key_row(db, authorization, x_api_key)` 拿 ApiKey 行(同文件 _meter 在用)。MCP `enqueue_custom_scrape`:有 `_ws_id_from_ctx(db)`;`get_current_api_key().api_key_id` 拿 api_key_id(mcp_context.py:10 `McpApiKeyContext.api_key_id`)。
- 测试 helper `_clear_pending`/`_clear_running`/`_scrape_stub` 已在 test_spine_queue.py。mock 边界:`patch("app.spine._do_scrape", side_effect=stub)`。

---

## 文件结构

| 文件 | 职责 | 改 |
|---|---|---|
| `backend/app/models.py` | SpineJob 加 api_key_id / heartbeat_at | 改 |
| `backend/app/spine_queue.py` | enqueue 加 api_key_id;claim 设 heartbeat;execute 记账+心跳;reclaim 改判据;_start_heartbeat | 改 |
| `backend/app/api/v2.py` | custom_scrape_async 传 api_key_id | 改 |
| `backend/app/mcp_server.py` | enqueue_custom_scrape 传 api_key_id | 改 |
| `backend/tests/test_spine_queue.py` | 列/计费/心跳/reclaim 新判据测试 + 改造 3 个旧 reclaim 测试 | 改 |
| `backend/tests/test_spine_queue_api.py` | 端到端带 key 记账 | 改 |

---

## Task 1: SpineJob 加 api_key_id / heartbeat_at 列

**Files:**
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_spine_queue.py`(追加)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine_queue.py` 末尾:

```python
def test_spine_jobs_has_billing_and_heartbeat_cols():
    from sqlalchemy import inspect
    from app.db import engine
    init_db()
    cols = {c["name"] for c in inspect(engine).get_columns("spine_jobs")}
    assert "api_key_id" in cols, "spine_jobs 缺列 api_key_id"
    assert "heartbeat_at" in cols, "spine_jobs 缺列 heartbeat_at"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py::test_spine_jobs_has_billing_and_heartbeat_cols -v`
Expected: FAIL(列不存在)

- [ ] **Step 3: 加两列**

在 `backend/app/models.py` 的 `SpineJob` 类里,`workspace_id` 那行之后追加(顶部 import 已有 `Column, Integer, DateTime, ForeignKey`):

```python
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), index=True, nullable=True)  # 计费归属,enqueue 持久化
    heartbeat_at = Column(DateTime, index=True, nullable=True)  # worker 续约时间戳,reclaim 判据
```

`_migrate()` 会自动给已存在的 spine_jobs 表补这两列(通用 ADD COLUMN),无需改 db.py。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py::test_spine_jobs_has_billing_and_heartbeat_cols -v`
Expected: PASS

- [ ] **Step 5: 迁移演练(真实库副本,自动补列 + 零丢失)**

Run:
```bash
cd backend
cp ../data/smart_crawler.db /tmp/spinebh_rehearsal.db
DATABASE_URL="sqlite:////tmp/spinebh_rehearsal.db" .venv/bin/python -c "
from app.db import init_db; init_db(); init_db()
import sqlite3; c=sqlite3.connect('/tmp/spinebh_rehearsal.db')
cols={r[1] for r in c.execute('PRAGMA table_info(spine_jobs)')}
assert 'api_key_id' in cols and 'heartbeat_at' in cols, cols
print('spine_jobs cols OK, products preserved:', c.execute('SELECT count(*) FROM products').fetchone()[0])
"
rm -f /tmp/spinebh_rehearsal.db
```
Expected: `spine_jobs cols OK, products preserved: <非0>` 无报错。

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/models.py backend/tests/test_spine_queue.py
git commit -m "feat(spine-queue): SpineJob api_key_id + heartbeat_at columns"
```

---

## Task 2: enqueue 持久化 api_key_id + claim 设首次心跳

**Files:**
- Modify: `backend/app/spine_queue.py`
- Test: `backend/tests/test_spine_queue.py`(追加)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine_queue.py` 末尾:

```python
def test_enqueue_persists_api_key_id():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue
    jid = enqueue(s, "https://x.com/p/bill", "bill-set", api_key_id=42,
                  workspace_id=None)
    s.commit()
    job = s.get(SpineJob, jid)
    assert job.api_key_id == 42
    s.close()


def test_claim_sets_heartbeat():
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job
    jid = enqueue(s, "https://x.com/p/hb", "hb-set", workspace_id=None)
    s.commit(); s.close()
    assert claim_job("w-hb") == jid
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.heartbeat_at is not None  # 领取即设首次心跳
    s2.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k "persists_api_key or claim_sets_heartbeat" -v`
Expected: FAIL(enqueue 无 api_key_id 参数 / heartbeat_at 为 None)

- [ ] **Step 3: 改 enqueue + claim_job**

在 `backend/app/spine_queue.py`:

(a) `enqueue` 加 `api_key_id` 参数并写入。把现有 enqueue 签名和 SpineJob 构造改为:

```python
def enqueue(db: Session, url: str, dataset: str, *,
            entity_type: str = "generic",
            save_policy: str = "promote_if_valid",
            force_live: bool = False, max_retries: int = 3,
            api_key_id: int | None = None,
            workspace_id: int | None = None) -> int:
    """入队一条 spine 抓取任务,返回 job_id。调用方负责 commit。"""
    job = SpineJob(url=url, dataset=dataset, entity_type=entity_type,
                   save_policy=save_policy, force_live=force_live,
                   status="pending", retries=0, max_retries=max_retries,
                   next_attempt_at=datetime.utcnow(), api_key_id=api_key_id,
                   workspace_id=workspace_id, created_at=datetime.utcnow())
    db.add(job)
    db.flush()
    return job.id
```

(b) `claim_job` 的 UPDATE values 里加 `heartbeat_at=now`。把现有 `.values(status="running", worker=worker_id, started_at=now)` 改为:

```python
        res = s.execute(
            update(SpineJob)
            .where(SpineJob.id == job.id, SpineJob.status == "pending")
            .values(status="running", worker=worker_id,
                    started_at=now, heartbeat_at=now))
        return job.id if res.rowcount == 1 else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k "persists_api_key or claim_sets_heartbeat" -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine_queue.py backend/tests/test_spine_queue.py
git commit -m "feat(spine-queue): enqueue persists api_key_id + claim sets heartbeat"
```

---

## Task 3: execute_job 成功记账

**Files:**
- Modify: `backend/app/spine_queue.py`
- Test: `backend/tests/test_spine_queue.py`(追加)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine_queue.py` 末尾:

```python
def test_execute_success_records_usage():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    from app.models import Usage
    jid = enqueue(s, "https://x.com/p/billok", "billok-set", entity_type="product",
                  save_policy="main", api_key_id=7, workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    before = SessionLocal()
    n_before = before.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    before.close()
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        execute_job(jid)
    after = SessionLocal()
    rows = (after.query(Usage)
            .filter(Usage.endpoint == "/spine/worker/execute", Usage.api_key_id == 7)
            .all())
    after_count = after.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    after.close()
    assert after_count == n_before + 1  # 成功记一行
    assert any(r.api_key_id == 7 for r in rows)


def test_execute_failure_records_no_usage():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    from app.models import Usage
    jid = enqueue(s, "https://x.com/p/billfail", "billfail-set", api_key_id=8,
                  workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    before = SessionLocal()
    n_before = before.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    before.close()
    def boom(db, url, **kw):
        raise RuntimeError("fail no bill")
    with patch("app.spine._do_scrape", side_effect=boom):
        execute_job(jid)
    after = SessionLocal()
    n_after = after.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    after.close()
    assert n_after == n_before  # 失败不记账


def test_execute_records_usage_with_null_api_key():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/nullkey", "nullkey-set", save_policy="main",
                  api_key_id=None, workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    # api_key_id=None 记账不崩
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        out = execute_job(jid)
    assert out["status"] == "success"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k "records_usage or records_no_usage or null_api_key" -v`
Expected: FAIL(test_execute_success_records_usage:无 Usage 行;另两个可能因无记账逻辑而行为不符)

- [ ] **Step 3: execute_job 成功分支加记账**

在 `backend/app/spine_queue.py` 的 `execute_job` 里,先在函数内读出 `api_key_id`(在读 `workspace_id = job.workspace_id` 那行之后加一行),再在成功分支 resolve 之后、`job.status = "success"` 之前插入记账。改成:

```python
        force_live = bool(job.force_live)
        workspace_id = job.workspace_id
        api_key_id = job.api_key_id
        try:
            ds = spine.get_or_create_dataset(
                s, dataset_name, workspace_id=workspace_id,
                entity_type=entity_type)
            out = spine.resolve(s, url, ds, workspace_id=workspace_id,
                                force_live=force_live, save_policy=save_policy)
            _record_execute_usage(api_key_id, workspace_id, out)
            job.status = "success"
            job.result_record_id = out.get("record_id")
            job.finished_at = datetime.utcnow()
            job.error = None
            return {"job_id": job_id, "status": "success",
                    "record_id": out.get("record_id")}
        except Exception as exc:
            return _handle_failure(s, job, exc)
```

并在 `execute_job` 之后(或 `_handle_failure` 附近)加 helper:

```python
def _record_execute_usage(api_key_id, workspace_id, out) -> None:
    """成功落库后按 resolve 的 credits_used 记账(精确到 key)。失败路径不调本函数。"""
    from .billing import record_usage
    try:
        record_usage(api_key_id=api_key_id, endpoint="/spine/worker/execute",
                     record_count=1, bytes_returned=0, duration_ms=0,
                     credits_used=int(out.get("credits_used") or 0),
                     workspace_id=workspace_id)
    except Exception:
        # 计费绝不阻断 worker 落库(与同步 _meter 容错一致)
        pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k "records_usage or records_no_usage or null_api_key" -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine_queue.py backend/tests/test_spine_queue.py
git commit -m "feat(spine-queue): bill execute success via record_usage (per api_key)"
```

---

## Task 4: 心跳续约 _start_heartbeat + execute 起停

**Files:**
- Modify: `backend/app/spine_queue.py`
- Test: `backend/tests/test_spine_queue.py`(追加)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine_queue.py` 末尾:

```python
def test_start_heartbeat_updates_and_stops():
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, _start_heartbeat
    jid = enqueue(s, "https://x.com/p/beat", "beat-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w-beat")  # heartbeat_at = now
    s2 = SessionLocal(); first = s2.get(SpineJob, jid).heartbeat_at; s2.close()
    import time
    stop, t = _start_heartbeat(jid, interval=0.1)
    time.sleep(0.35)  # 至少续约 2~3 次
    stop.set(); t.join(timeout=2)
    s3 = SessionLocal(); later = s3.get(SpineJob, jid).heartbeat_at; s3.close()
    assert later > first  # 心跳确实更新了 heartbeat_at
    assert not t.is_alive()  # 线程已退出
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k start_heartbeat -v`
Expected: FAIL(_start_heartbeat 不存在,ImportError)

- [ ] **Step 3: 加 _start_heartbeat + execute 起停**

在 `backend/app/spine_queue.py` 顶部 import 区加 `import threading`(确认没重复)。加 helper:

```python
HEARTBEAT_INTERVAL = 30.0


def _start_heartbeat(job_id: int, interval: float = HEARTBEAT_INTERVAL):
    """起一个后台线程,每 interval 秒把 job.heartbeat_at 续约为 now。

    返回 (stop_event, thread)。execute 结束时 stop.set() + join 停掉。
    让 reclaim 能区分"活着的长抓(心跳在续)"和"真崩溃(心跳停)"。
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            try:
                with session_scope() as s:
                    j = s.get(SpineJob, job_id)
                    if j is not None:
                        j.heartbeat_at = datetime.utcnow()
            except Exception:
                pass  # 续约失败不影响主执行

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return stop, t
```

把 `execute_job` 用心跳包起来。把 `with session_scope() as s:` 整段包进心跳起停。改成:

```python
def execute_job(job_id: int) -> dict:
    """执行一条已领取(running)的任务:spine.resolve 落库 → 成功/重试/失败。

    注意:spine.resolve 内部自行提交落库(dataset/snapshot/record);job 状态
    另由本函数的 session_scope 提交,二者非原子。极窄崩溃窗口下可能留下卡在
    running 的悬挂 job —— 由 spine_worker 的 running 超时回收兜底,期间靠心跳
    续约区分活着的长抓 vs 真崩溃。
    """
    from . import spine
    stop, t = _start_heartbeat(job_id)
    try:
        with session_scope() as s:
            job = s.get(SpineJob, job_id)
            if job is None:
                raise ValueError(f"任务不存在: {job_id}")
            url = job.url
            dataset_name = job.dataset
            entity_type = job.entity_type or "generic"
            save_policy = job.save_policy or "promote_if_valid"
            force_live = bool(job.force_live)
            workspace_id = job.workspace_id
            api_key_id = job.api_key_id
            try:
                ds = spine.get_or_create_dataset(
                    s, dataset_name, workspace_id=workspace_id,
                    entity_type=entity_type)
                out = spine.resolve(s, url, ds, workspace_id=workspace_id,
                                    force_live=force_live, save_policy=save_policy)
                _record_execute_usage(api_key_id, workspace_id, out)
                job.status = "success"
                job.result_record_id = out.get("record_id")
                job.finished_at = datetime.utcnow()
                job.error = None
                return {"job_id": job_id, "status": "success",
                        "record_id": out.get("record_id")}
            except Exception as exc:
                return _handle_failure(s, job, exc)
    finally:
        stop.set()
        t.join(timeout=2)
```

注:`_start_heartbeat`/`HEARTBEAT_INTERVAL`/`_record_execute_usage` 须定义在 `execute_job` 之前或同模块可见处。把 `_start_heartbeat` 和 `HEARTBEAT_INTERVAL` 放在 `execute_job` 之前。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k "start_heartbeat or execute or records_usage or null_api_key" -v`
Expected: 全 passed(心跳新测试 + Task3 记账测试 + 原 execute 测试都不退)

- [ ] **Step 5: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine_queue.py backend/tests/test_spine_queue.py
git commit -m "feat(spine-queue): heartbeat renewal thread around execute_job"
```

---

## Task 5: reclaim 改判据(heartbeat_at)+ 改造旧测试

**Files:**
- Modify: `backend/app/spine_queue.py`
- Test: `backend/tests/test_spine_queue.py`(改 3 个旧测试 + 加新测试)

- [ ] **Step 1: 改判据实现**

在 `backend/app/spine_queue.py` 把 `reclaim_stale_jobs` 的 filter 从 `started_at` 改为 `heartbeat_at`:

```python
def reclaim_stale_jobs(running_timeout_sec: int = 600) -> int:
    """把心跳停超 running_timeout_sec 的 running job 重置为 pending,返回回收条数。

    判据用 heartbeat_at(worker execute 期间每 HEARTBEAT_INTERVAL 续约):
    只有真崩溃/卡死(心跳停了)才被回收;活着的长抓持续续约,不会被误回收。
    heartbeat_at IS NULL(刚领还没续约的脏行)一并回收。worker loop 每轮先调。
    """
    cutoff = datetime.utcnow() - timedelta(seconds=running_timeout_sec)
    with session_scope() as s:
        stale = (s.query(SpineJob)
                 .filter(SpineJob.status == "running",
                         or_(SpineJob.heartbeat_at < cutoff,
                             SpineJob.heartbeat_at.is_(None)))
                 .all())
        for job in stale:
            job.status = "pending"
            job.worker = None
            job.next_attempt_at = datetime.utcnow()
        return len(stale)
```

- [ ] **Step 2: 改造 3 个旧 reclaim 测试(判据从 started_at 移到 heartbeat_at)**

在 `backend/tests/test_spine_queue.py`:

(a) `test_reclaim_stale_running_job_to_pending`:把"人为把 started_at 推老"那段改成推老 heartbeat_at。把:
```python
    job.started_at = datetime.utcnow() - timedelta(seconds=99999)
```
改为:
```python
    job.heartbeat_at = datetime.utcnow() - timedelta(seconds=99999)
```

(b) `test_reclaim_recovers_running_with_null_started_at`:改名+改判据为 NULL heartbeat。把整个函数体里:
```python
    job.status = "running"; job.started_at = None; job.worker = "ghost"
```
改为:
```python
    job.status = "running"; job.heartbeat_at = None; job.worker = "ghost"
```
(函数名保持不变,避免牵动别处;只改造的是判据字段。)

(c) `test_reclaim_leaves_fresh_running_untouched`:claim 后 heartbeat_at=now 新鲜,语义不变,无需改代码(确认仍 PASS)。

- [ ] **Step 3: 加新测试(心跳新鲜不回收 / 心跳老回收)**

追加到 `backend/tests/test_spine_queue.py` 末尾:

```python
def test_reclaim_uses_heartbeat_not_started_at():
    init_db()
    _clear_running()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, reclaim_stale_jobs
    jid = enqueue(s, "https://x.com/p/hbreclaim", "hbr-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")  # started_at=now, heartbeat_at=now
    # 关键:started_at 推得很老,但 heartbeat_at 保持新鲜 → 不应回收(活着的长抓)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    job.started_at = datetime.utcnow() - timedelta(seconds=99999)
    job.heartbeat_at = datetime.utcnow()  # 心跳新鲜
    s2.commit(); s2.close()
    n = reclaim_stale_jobs(running_timeout_sec=600)
    s3 = SessionLocal()
    job = s3.get(SpineJob, jid)
    assert job.status == "running"  # 心跳新鲜,长抓不被误回收
    s3.close()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -k reclaim -v`
Expected: 全 passed(改造后的 3 个旧 reclaim + 新增 1 个 heartbeat 判据测试)

- [ ] **Step 5: 全量回归(spine 队列单测)**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue.py -q`
Expected: 全 passed,无回归。

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/spine_queue.py backend/tests/test_spine_queue.py
git commit -m "feat(spine-queue): reclaim uses heartbeat_at; long jobs survive"
```

---

## Task 6: REST + MCP enqueue 传 api_key_id

**Files:**
- Modify: `backend/app/api/v2.py`、`backend/app/mcp_server.py`
- Test: `backend/tests/test_spine_queue_api.py`(追加)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_spine_queue_api.py` 末尾:

```python
def test_v2_async_persists_api_key_id_and_bills():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.apikey import generate, hash_key, short
    from app.models import ApiKey, SpineJob, Usage
    init_db()
    raw = generate()
    s = SessionLocal()
    try:
        k = ApiKey(name="bill-key", key_prefix=short(raw), key_hash=hash_key(raw),
                   scopes=["crawler:scrape", "crawler:read"], active=True)
        s.add(k); s.commit(); kid = k.id
    finally:
        s.close()
    headers = {"X-API-Key": raw}
    client = TestClient(app)
    # 清场
    cs = SessionLocal()
    cs.query(SpineJob).filter(SpineJob.status == "pending").delete(); cs.commit(); cs.close()
    r = client.post("/api/v2/custom/scrape/async", headers=headers,
                    json={"url": "https://x.com/p/billed", "dataset": "billed-set",
                          "entity_type": "product", "save_policy": "main"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    # job 持久化了 api_key_id
    chk = SessionLocal(); job = chk.get(SpineJob, jid)
    assert job.api_key_id == kid; chk.close()
    # 消费 → 记账到该 key
    from app.spine_queue import claim_job, execute_job
    assert claim_job("test-worker") == jid
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        execute_job(jid)
    bill = SessionLocal()
    rows = bill.query(Usage).filter(Usage.endpoint == "/spine/worker/execute",
                                    Usage.api_key_id == kid).count()
    bill.close()
    assert rows >= 1


def test_mcp_enqueue_persists_api_key_id_none_ok():
    init_db()
    from app import mcp_server
    from app.models import SpineJob
    # 无 ctx → api_key_id None,不崩
    out = mcp_server.enqueue_custom_scrape(url="https://x.com/p/mcpnull",
                                           dataset="mcpnull-set", save_policy="main")
    jid = out["job_id"]
    chk = SessionLocal(); job = chk.get(SpineJob, jid)
    assert job.api_key_id is None; chk.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue_api.py -k "persists_api_key or api_key_id_none" -v`
Expected: FAIL(REST/MCP 未传 api_key_id,job.api_key_id 为 None 而非 kid)

- [ ] **Step 3: REST 传 api_key_id**

在 `backend/app/api/v2.py` 的 `custom_scrape_async` 里,拿 key 并传 api_key_id。把现有函数体改为:

```python
    _require_scope(db, authorization, x_api_key, "crawler:scrape")
    ws = _v2_ws_id(db, authorization, x_api_key)
    key = _api_key_row(db, authorization, x_api_key)
    job_id = spine_queue.enqueue(db, req.url, req.dataset,
                                 entity_type=req.entity_type,
                                 save_policy=req.save_policy,
                                 force_live=req.force_live,
                                 max_retries=req.max_retries,
                                 api_key_id=key.id if key else None,
                                 workspace_id=ws)
    db.commit()
    return {"job_id": job_id, "status": "pending"}
```

确认 `_api_key_row` 是 v2.py 里的函数(_meter 在用,是)。

- [ ] **Step 4: MCP 传 api_key_id**

在 `backend/app/mcp_server.py` 的 `enqueue_custom_scrape` 里,从 ctx 拿 api_key_id。把 try 块改为:

```python
    from . import spine_queue
    from .mcp_context import get_current_api_key
    s = SessionLocal()
    try:
        ws = _ws_id_from_ctx(s)
        ctx = get_current_api_key()
        aki = ctx.api_key_id if ctx else None
        job_id = spine_queue.enqueue(s, url, dataset, entity_type=entity_type,
                                     save_policy=save_policy, force_live=force_live,
                                     max_retries=max_retries, api_key_id=aki,
                                     workspace_id=ws)
        s.commit()
        return {"job_id": job_id, "status": "pending"}
    finally:
        s.close()
```

确认 `get_current_api_key` 在 mcp_server.py 顶部已 import(现有工具在用,是;若 `_ws_id_from_ctx` 已 import 它就直接用)。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_spine_queue_api.py -k "persists_api_key or api_key_id_none" -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler
git add backend/app/api/v2.py backend/app/mcp_server.py backend/tests/test_spine_queue_api.py
git commit -m "feat(spine-queue): REST/MCP enqueue persists api_key_id for billing"
```

---

## Task 7: 端到端验证 + memory

**Files:** 无(验证)

- [ ] **Step 1: 后端全量回归**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全 passed(原 220 + 本期新增),无回归。

- [ ] **Step 2: 迁移演练复核(真实库副本)**

Run:
```bash
cd backend
cp ../data/smart_crawler.db /tmp/spinebh_e2e.db
DATABASE_URL="sqlite:////tmp/spinebh_e2e.db" .venv/bin/python -c "
from app.db import init_db; init_db(); init_db()
import sqlite3; c=sqlite3.connect('/tmp/spinebh_e2e.db')
cols={r[1] for r in c.execute('PRAGMA table_info(spine_jobs)')}
assert 'api_key_id' in cols and 'heartbeat_at' in cols
print('products preserved:', c.execute('SELECT count(*) FROM products').fetchone()[0])
"
rm -f /tmp/spinebh_e2e.db
```
Expected: products 非 0,无报错。

- [ ] **Step 3: 端到端脚本(mock 抓取,验记账 + 心跳闭环)**

Run:
```bash
cd backend && .venv/bin/python -c "
from unittest.mock import patch
from app.db import init_db, SessionLocal
from app import spine_queue
import app.spine_worker as sw
from app.models import SpineJob, Usage
init_db()
cs=SessionLocal(); cs.query(SpineJob).filter(SpineJob.status=='pending').delete(); cs.commit(); cs.close()
s=SessionLocal()
jid=spine_queue.enqueue(s,'https://x.com/p/e2ebill','e2ebill-q',entity_type='product',save_policy='main',api_key_id=99,workspace_id=None)
s.commit(); s.close()
def stub(db,url,**kw): return {'scrape_id':'x','url':url,'data':{'title':'E2E','confidence':0.95},'metadata':{'canonical':None},'html':'<html>x</html>','warnings':[],'usage':{'source':'live','credits_used':2}}
calls={'n':0}
def once(): calls['n']+=1; return calls['n']<=1
with patch('app.spine._do_scrape', side_effect=stub):
    sw.run_loop(poll_interval=0, should_continue=once)
s2=SessionLocal(); job=s2.get(SpineJob,jid)
bills=s2.query(Usage).filter(Usage.endpoint=='/spine/worker/execute', Usage.api_key_id==99).count()
print('job status:', job.status, '| heartbeat_at set:', job.heartbeat_at is not None, '| usage rows:', bills)
assert job.status=='success' and bills>=1
s2.close()
print('BILLING + HEARTBEAT E2E OK')
" 2>&1 | grep -vE 'utcnow|wrap_callable|Deprecat'
```
Expected: `job status: success | heartbeat_at set: True | usage rows: 1` + `BILLING + HEARTBEAT E2E OK`。

- [ ] **Step 4: 更新 memory**

更新 `spine-async-queue.md`:把"已知缺口:worker execute 不计费"和"多副本长抓回收"两条标为**已解决**(计费精确到 api_key、心跳续约 reclaim 改判据),并简述实现。MEMORY.md 索引行同步。

- [ ] **Step 5: 不自动部署**。汇报完成,等用户决定。

---

## Self-Review(写计划者已核对)

- **Spec 覆盖**:§1 加两列→Task1;§2 enqueue 持久化 api_key_id→Task2(claim 心跳一并)+ Task6(REST/MCP 传入),execute 记账→Task3;§3 心跳续约线程→Task4,claim 设首次心跳→Task2,reclaim 改判据→Task5;§4 测试贯穿;回归风险(3 个旧 reclaim 测试改造)→Task5 Step2;端到端→Task7。全覆盖。
- **类型/签名一致**:`enqueue(..., api_key_id=None, workspace_id=None)`、`_start_heartbeat(job_id, interval=HEARTBEAT_INTERVAL)->(stop,t)`、`_record_execute_usage(api_key_id, workspace_id, out)`、`reclaim_stale_jobs(running_timeout_sec=600)` 跨 Task 一致;execute_job 读 `job.api_key_id` 在 Task3 引入、Task4 心跳包裹时保留。
- **复用点已核实**:`record_usage` 签名 + api_key_id 可 None;`Usage` 有 endpoint/api_key_id;`resolve` 返回含 credits_used;`_migrate` 自动 ADD COLUMN;`_api_key_row`(v2)/`get_current_api_key().api_key_id`(mcp)。
- **无占位符**:每步含完整代码与命令。
- **已知风险(实现时核实)**:
  1. 心跳线程用独立 `session_scope`,与 execute 主 `session_scope` 是不同 session——两者都写 SpineJob 同一行不同字段(心跳写 heartbeat_at,主写 status)。SQLite WAL + busy_timeout 串行化写,极短交错下心跳的 commit 可能与主 commit 交错,但字段不冲突(心跳只动 heartbeat_at),最坏是 heartbeat_at 比 status 晚一拍,无害。
  2. Task5 改判据后,若某测试遗留 running 且 heartbeat_at 老,会被全局 reclaim 计入——测试已用 `_clear_running()` 清场。
  3. `_record_execute_usage` 用 try/except 容错,api_key_id=None 时 record_usage 内部走 workspace 记,不崩。
