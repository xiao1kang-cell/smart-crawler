# spine 队列计费 + 心跳续约 · 设计文档

> **日期:** 2026-06-12
> **分支(待建):** `feat/spine-queue-billing-heartbeat`
> **前置:** spine 异步抓取队列(已合并 main,`bdd6549`)
> **状态:** 设计已确认,待写实现计划

## 目标

补齐 spine 异步队列的两个已知缺口(backlog #14 / #15):
1. **计费**:worker `execute_job` 当前完全不计费——异步抓取是计费黑洞。补齐:精确到 api_key 记账。
2. **心跳续约**:`reclaim_stale_jobs` 按 `started_at` 时间回收,多副本下长抓(>RUNNING_TIMEOUT)会被误判超时回收→重复抓取。补齐:worker execute 期间心跳续约,reclaim 改判"心跳停超 N 秒"——只回收真崩溃,不误伤活着的长抓。

两个修复合一期(都围绕 SpineJob 加列 + execute 增强)。

## 核心原则

- 计费口径与同步路径 `_meter` 一致:成功按 `resolve` 的 `credits_used`(warehouse 命中=0,live 抓=2)记,失败不记。
- 心跳是唯一能区分"活着的长抓 vs 真崩溃"的方案;heartbeat 停超 timeout 才回收。
- 复用现有设施:`record_usage`(api_key_id 可为 None,容错)、`_migrate()`(自动 ADD COLUMN)、`_api_key_row`(REST)、`get_current_api_key().api_key_id`(MCP)。
- 不碰 SP1 同步入口,不碰电商队列。

## 1. 数据模型:SpineJob 加两列

`backend/app/models.py` 的 `SpineJob` 追加:

```python
api_key_id = Column(Integer, ForeignKey("api_keys.id"), index=True, nullable=True)  # 计费归属,enqueue 持久化
heartbeat_at = Column(DateTime, index=True, nullable=True)  # worker 续约时间戳,reclaim 判据
```

**无需手写 ALTER**:`db.py::_migrate()` 是通用自动迁移——遍历模型列,缺哪列就 `ALTER TABLE ADD COLUMN` 补哪列。给已存在的 spine_jobs 表加这两列由 `_migrate` 自动完成。

## 2. 计费链路

### enqueue 持久化 api_key_id

`spine_queue.enqueue(...)` 加参数 `api_key_id: int | None = None`,写进 SpineJob。

- REST `custom_scrape_async`(v2.py):`key = _api_key_row(db, authorization, x_api_key)`,传 `api_key_id=key.id if key else None`。
- MCP `enqueue_custom_scrape`(mcp_server.py):`ctx = get_current_api_key()`,传 `api_key_id=ctx.api_key_id if ctx else None`。

### execute 成功后记账

`execute_job` 成功分支(resolve 之后、置 success 之前):

```python
from .billing import record_usage
record_usage(api_key_id=job.api_key_id, endpoint="/spine/worker/execute",
             record_count=1, bytes_returned=0, duration_ms=0,
             credits_used=out.get("credits_used", 0),
             workspace_id=job.workspace_id)
```

- 失败分支(`_handle_failure`)不记账。
- `record_usage` 内部容错 `api_key_id=None`(走 workspace 记),没带 key 入队的 job 不会崩。
- 口径:warehouse 命中 `credits_used=0` 也照记一行(记录"发生过一次消费、0 credits");live 抓按实际。与同步 `_meter` 一致。
- endpoint 标 `/spine/worker/execute`,区别于同步 `/api/v2/custom/scrape`,便于区分异步消耗。

注:`resolve` 返回 dict 含 `credits_used`(spine.py:218,warehouse 命中分支 credits_used=0,live 分支默认 2)。execute 现取 `out.get("record_id")`,记账时同一个 `out` 取 `credits_used`。

## 3. 心跳续约

### worker execute 期间续约

`execute_job` 领到 job 后起轻量后台线程,每 `HEARTBEAT_INTERVAL`(默认 30s)更新 `heartbeat_at=now`;execute 结束(成功/失败/异常)停线程。用 `threading.Event` + daemon `Thread`,`finally` 里 `stop.set()` + `join(timeout)` 保证不泄漏。

```python
import threading

def _start_heartbeat(job_id: int, interval: float = 30.0):
    stop = threading.Event()
    def beat():
        while not stop.wait(interval):
            with session_scope() as s:
                j = s.get(SpineJob, job_id)
                if j is not None:
                    j.heartbeat_at = datetime.utcnow()
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return stop, t
```

`execute_job` 结构:
```python
stop, t = _start_heartbeat(job_id, HEARTBEAT_INTERVAL)
try:
    ... resolve + 记账 + 置 success / _handle_failure ...
finally:
    stop.set()
    t.join(timeout=2)
```

### claim_job 设首次心跳

`claim_job` 领取时,在置 running 的同一 UPDATE 里同时设 `heartbeat_at=now`(与 `started_at=now` 一起),避免"刚领到还没第一拍心跳就被判 NULL 回收"。

### reclaim 改判据

`reclaim_stale_jobs(running_timeout_sec=600)`:
- 判据从 `started_at < cutoff` 改为 `heartbeat_at < cutoff`(心跳停超 timeout 才回收)。
- 保留 NULL 防御:`or_(heartbeat_at < cutoff, heartbeat_at.is_(None))`(刚领没心跳的脏行也回收)。
- 默认 timeout 仍 600s(>30s 心跳间隔,容得下漏跳几拍)。

## 4. 测试策略(全程 TDD)

| 层 | 测试 |
|---|---|
| 模型迁移 | spine_jobs 有 api_key_id / heartbeat_at 列;老库副本 `_migrate` 演练零丢失 |
| 计费 | enqueue 带 api_key_id 持久化进 job;execute 成功后 Usage 表多一行(api_key_id 对、credits_used=resolve 值、endpoint=/spine/worker/execute);execute 失败不产生 Usage 行;api_key_id=None 记账不崩 |
| claim 心跳 | claim_job 领取后 heartbeat_at 非 None |
| 心跳续约 | `_start_heartbeat` 起线程后 heartbeat_at 被周期更新(短 interval 验证);stop 后线程退出 |
| reclaim 新判据 | heartbeat_at 老于 cutoff → 回收;heartbeat_at 新鲜 → 不回收;heartbeat_at IS NULL → 回收 |
| 端到端 | enqueue(带 key)→ worker 消费 → 落库 success + Usage 记一行 + 心跳线程正常起停 |

mock 边界:只 mock `app.spine._do_scrape`;心跳用真实线程 + 短 interval(如 0.1s)验证更新。

### 已有测试回归风险(实现时必处理)

`reclaim` 判据从 `started_at` 改 `heartbeat_at` 后,Task 4 已有的两个测试会受影响,必须同步改造:
- `test_reclaim_stale_running_job_to_pending`:原本造 `started_at` 老 → 改成造 `heartbeat_at` 老(claim 现在会设 heartbeat_at=now,所以要手动推老 heartbeat_at)。
- `test_reclaim_leaves_fresh_running_untouched`:claim 后 heartbeat_at=now 新鲜 → 仍不应被回收,语义不变但确认通过。
- `test_reclaim_recovers_running_with_null_started_at`:NULL 防御从 started_at 移到 heartbeat_at → 改成造 `heartbeat_at IS NULL`。
全量回归必须保持 220 passed 不退。

## 范围边界(YAGNI)

**这期做:** SpineJob 加 api_key_id/heartbeat_at;enqueue 链路持久化 api_key_id;execute 成功记账;心跳续约线程;reclaim 改判据;全程 TDD + 端到端。

**这期不做:**
- ❌ 真起多 worker 副本压测(只保证机制正确)
- ❌ per-job 硬超时杀进程(心跳解决误回收;卡死 job 靠心跳停+reclaim 兜)
- ❌ 计费额度/限流(只记账,不拦截超额)
- ❌ 失败"尝试"计费(失败不记)

## 文件清单

| 文件 | 改动 |
|---|---|
| `backend/app/models.py` | SpineJob 加 api_key_id / heartbeat_at | 改 |
| `backend/app/spine_queue.py` | enqueue 加 api_key_id 参数;claim_job 设 heartbeat_at;execute_job 记账 + 心跳起停;reclaim 改判据;`_start_heartbeat` | 改 |
| `backend/app/api/v2.py` | custom_scrape_async 传 api_key_id | 改 |
| `backend/app/mcp_server.py` | enqueue_custom_scrape 传 api_key_id | 改 |
| `backend/tests/test_spine_queue.py` | 计费/心跳/reclaim 新判据测试 | 改 |
| `backend/tests/test_spine_queue_api.py` | 端到端带 key 记账 | 改 |
