# 内存自适应并发闸 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给定时采集 worker 加一道内存安全闸——主机已用内存超阈值时暂停领取新 job(不起新浏览器),内存回落自动恢复,让 `WORKER_THREADS` 可安全调高。

**Architecture:** 新增零依赖模块 `app/memory_gate.py` 读 Linux `/proc/meminfo` 判断内存,提供 `wait_until_ok` 阻塞原语;在 `app/worker.py::run_loop` 的 `claim_job` 之前插入闸。fail-open(读不到内存就放行)、安全优先(超时回循环重判绝不硬领)、默认开启(`MEM_GATE_THRESHOLD=80`)。

**Tech Stack:** Python 3, pytest, 标准库 only(无 psutil)。venv 在 `backend/.venv`,测试用 `.venv/bin/python -m pytest`。

设计来源: `docs/superpowers/specs/2026-06-09-memory-adaptive-concurrency-gate-design.md`

---

## 文件结构

- **Create** `backend/app/memory_gate.py` — 读主机内存 + `wait_until_ok` 阻塞原语。单一职责,零依赖。
- **Create** `backend/tests/test_memory_gate.py` — memory_gate 单元测试。
- **Modify** `backend/app/worker.py` — `run_loop` 接入闸(模块级读 3 个 env + 循环内插一行)。
- **Create** `backend/tests/test_worker_memory_gate.py` — 验证闸 False 时不领 job。
- **Modify** `backend/.env.example` — 注明 3 个新环境变量。

---

## Task 1: memory_gate 读内存(available_percent)

**Files:**
- Create: `backend/app/memory_gate.py`
- Test: `backend/tests/test_memory_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_memory_gate.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_available_percent_parses_meminfo(tmp_path):
    from app import memory_gate

    f = tmp_path / "meminfo"
    f.write_text(
        "MemTotal:       16000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    4000000 kB\n"
        "Buffers:          200000 kB\n"
    )
    # 4000000 / 16000000 = 25%
    assert memory_gate.available_percent(str(f)) == pytest.approx(25.0)


def test_used_percent_is_complement(tmp_path):
    from app import memory_gate

    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\n")
    assert memory_gate.used_percent(str(f)) == pytest.approx(75.0)


def test_available_percent_fail_open_on_missing_file():
    from app import memory_gate

    # 读不到文件 → fail-open 返回 100.0(永不阻塞抓取)
    assert memory_gate.available_percent("/no/such/meminfo") == 100.0


def test_available_percent_fail_open_on_missing_fields(tmp_path):
    from app import memory_gate

    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 16000000 kB\n")   # 缺 MemAvailable
    assert memory_gate.available_percent(str(f)) == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.memory_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/memory_gate.py
"""内存自适应并发闸 —— 读主机内存,提供"等到内存 OK"的阻塞原语。

零依赖(读 Linux /proc/meminfo,不引入 psutil)。fail-open:读不到内存时
返回 100% available,闸永不阻塞抓取。容器未设 per-container 内存限制,
故以主机级 MemAvailable 为信号(OOM 风险是主机级)。

详见 docs/superpowers/specs/2026-06-09-memory-adaptive-concurrency-gate-design.md
"""
from __future__ import annotations

import time


def _read_meminfo(path: str) -> dict[str, int]:
    """解析 /proc/meminfo 为 {key: kB}。读不到 → 空 dict。"""
    out: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val = parts[1].strip().split()
                if val and val[0].isdigit():
                    out[key] = int(val[0])
    except OSError:
        return {}
    return out


def available_percent(meminfo_path: str = "/proc/meminfo") -> float:
    """可用内存百分比 = MemAvailable / MemTotal * 100。
    读不到 / 缺字段 / 非 Linux → 返回 100.0(fail-open,永不阻塞)。"""
    info = _read_meminfo(meminfo_path)
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if not total or avail is None:
        return 100.0
    return avail / total * 100.0


def used_percent(meminfo_path: str = "/proc/meminfo") -> float:
    """已用内存百分比 = 100 - available_percent()。"""
    return 100.0 - available_percent(meminfo_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_gate.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/memory_gate.py backend/tests/test_memory_gate.py
git commit -m "feat(worker): memory_gate reads host /proc/meminfo (fail-open)"
```

---

## Task 2: wait_until_ok 阻塞原语

**Files:**
- Modify: `backend/app/memory_gate.py`
- Test: `backend/tests/test_memory_gate.py`

- [ ] **Step 1: Write the failing test**

追加到 `backend/tests/test_memory_gate.py`:

```python
def test_wait_until_ok_returns_true_when_memory_ok(monkeypatch):
    from app import memory_gate

    # 内存充裕(used 30% < 阈值 80)→ 立即 True,不 sleep
    monkeypatch.setattr(memory_gate, "used_percent", lambda *a, **k: 30.0)
    slept = []
    monkeypatch.setattr(memory_gate.time, "sleep", lambda s: slept.append(s))

    assert memory_gate.wait_until_ok(80.0) is True
    assert slept == []                       # 没等待


def test_wait_until_ok_disabled_threshold_returns_true(monkeypatch):
    from app import memory_gate

    # 阈值 0 / 100 = 关闸 → 立即 True,连内存都不查
    monkeypatch.setattr(memory_gate, "used_percent",
                        lambda *a, **k: 99.0)   # 即便内存爆了
    assert memory_gate.wait_until_ok(0) is True
    assert memory_gate.wait_until_ok(100) is True


def test_wait_until_ok_times_out_when_memory_stays_high(monkeypatch):
    from app import memory_gate

    # used 恒 95% > 阈值 80,等满 max_wait 仍 False
    monkeypatch.setattr(memory_gate, "used_percent", lambda *a, **k: 95.0)
    monkeypatch.setattr(memory_gate.time, "sleep", lambda s: None)  # 加速

    assert memory_gate.wait_until_ok(
        80.0, check_interval=1.0, max_wait=3.0) is False


def test_wait_until_ok_recovers_when_memory_drops(monkeypatch):
    from app import memory_gate

    # 前两次高、第三次回落 → True
    seq = iter([95.0, 95.0, 50.0])
    monkeypatch.setattr(memory_gate, "used_percent",
                        lambda *a, **k: next(seq))
    monkeypatch.setattr(memory_gate.time, "sleep", lambda s: None)

    assert memory_gate.wait_until_ok(
        80.0, check_interval=1.0, max_wait=60.0) is True


def test_wait_until_ok_stops_when_should_continue_false(monkeypatch):
    from app import memory_gate

    # 内存高,但 should_continue 变假 → 提前 False(优雅停机不卡)
    monkeypatch.setattr(memory_gate, "used_percent", lambda *a, **k: 95.0)
    monkeypatch.setattr(memory_gate.time, "sleep", lambda s: None)

    assert memory_gate.wait_until_ok(
        80.0, check_interval=1.0, max_wait=60.0,
        should_continue=lambda: False) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_gate.py -q`
Expected: FAIL — `AttributeError: module 'app.memory_gate' has no attribute 'wait_until_ok'`

- [ ] **Step 3: Write minimal implementation**

追加到 `backend/app/memory_gate.py`:

```python
def wait_until_ok(threshold_pct: float, *,
                  check_interval: float = 2.0,
                  max_wait: float = 300.0,
                  should_continue=None,
                  meminfo_path: str = "/proc/meminfo") -> bool:
    """阻塞直到 used_percent() < threshold_pct。

    返回值:True = 可以继续领 job;False = 本轮别领,回上层循环重判。
    - threshold_pct <= 0 或 >= 100:关闸,立即 True(连内存都不查)。
    - 每 check_interval 秒查一次;累计等待达 max_wait 仍超阈 → False。
    - should_continue() 变假 → 提前 False(worker 停机时不卡)。

    安全优先:超时只返回 False(上层会回循环重判),**绝不**在内存高位放行。
    """
    if threshold_pct <= 0 or threshold_pct >= 100:
        return True
    should_continue = should_continue or (lambda: True)
    waited = 0.0
    while True:
        if used_percent(meminfo_path) < threshold_pct:
            return True
        if not should_continue():
            return False
        if waited >= max_wait:
            return False
        time.sleep(check_interval)
        waited += check_interval
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_gate.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/memory_gate.py backend/tests/test_memory_gate.py
git commit -m "feat(worker): memory_gate.wait_until_ok blocking primitive"
```

---

## Task 3: worker.run_loop 接入内存闸

**Files:**
- Modify: `backend/app/worker.py`(模块级 env + `run_loop` 循环内)
- Test: `backend/tests/test_worker_memory_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_worker_memory_gate.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_run_loop_skips_claim_when_gate_blocks(monkeypatch):
    """内存闸返回 False 时,本轮不调用 claim_job(不领 job、不起浏览器)。"""
    from app import worker

    # 闸:第一次 False(挡住),促使 run_loop 这一轮跳过 claim_job
    monkeypatch.setattr(worker, "MEM_THRESHOLD", 80.0)
    gate_calls = []

    def fake_gate(threshold, **kw):
        gate_calls.append(threshold)
        return False                      # 一直挡

    monkeypatch.setattr(worker.memory_gate, "wait_until_ok", fake_gate)

    claimed = []
    monkeypatch.setattr(worker, "claim_job",
                        lambda wid: claimed.append(wid))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    # should_continue:跑 2 轮后停,避免死循环
    ticks = iter([True, True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert gate_calls                      # 闸被查过
    assert claimed == []                   # 闸挡住 → 从没领 job


def test_run_loop_claims_when_gate_open(monkeypatch):
    """内存闸放行时,正常领 job。"""
    from app import worker

    monkeypatch.setattr(worker, "MEM_THRESHOLD", 80.0)
    monkeypatch.setattr(worker.memory_gate, "wait_until_ok",
                        lambda threshold, **kw: True)

    claimed = []

    def fake_claim(wid):
        claimed.append(wid)
        return None                        # 没有 job,走 sleep continue 分支

    monkeypatch.setattr(worker, "claim_job", fake_claim)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    ticks = iter([True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert claimed                         # 闸放行 → 领了 job
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_worker_memory_gate.py -q`
Expected: FAIL — `AttributeError: module 'app.worker' has no attribute 'memory_gate'`(或 `MEM_THRESHOLD`)

- [ ] **Step 3: Write minimal implementation**

3a. 在 `backend/app/worker.py` 顶部 import 区(现有 `from .runner import claim_job, execute_job` 附近)加:

```python
from . import memory_gate
```

3b. 在模块级配置区(现有 `POLL_INTERVAL` / `JOB_TIMEOUT` 定义之后,约 30 行处)加:

```python
# 内存自适应并发闸 —— 主机已用内存超阈值则暂停领新 job。设 0/100 关闸。
MEM_THRESHOLD = float(os.environ.get("MEM_GATE_THRESHOLD", "80"))
MEM_CHECK_INTERVAL = float(os.environ.get("MEM_GATE_CHECK_INTERVAL", "2"))
MEM_MAX_WAIT = float(os.environ.get("MEM_GATE_MAX_WAIT", "300"))
```

3c. 在 `run_loop` 的 `while should_continue():`(worker.py:71)之后、`try: job_id = claim_job(...)` 之前插入闸:

```python
    while should_continue():
        # 内存安全闸:已用内存超阈值则暂停领新 job(不起新浏览器),
        # 内存回落自动恢复。超时回循环重判,绝不在内存高位硬领。
        if not memory_gate.wait_until_ok(
                MEM_THRESHOLD, check_interval=MEM_CHECK_INTERVAL,
                max_wait=MEM_MAX_WAIT, should_continue=should_continue):
            continue
        try:
            job_id = claim_job(WORKER_ID)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_worker_memory_gate.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run full worker + memory_gate suite (no regression)**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_gate.py tests/test_worker_memory_gate.py -q`
Expected: PASS (11 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/worker.py backend/tests/test_worker_memory_gate.py
git commit -m "feat(worker): gate claim_job on host memory threshold"
```

---

## Task 4: 文档化环境变量

**Files:**
- Modify: `backend/.env.example`(若不存在则用根 `.env.example`,与 spec 一致)

- [ ] **Step 1: 确认 .env.example 位置**

Run: `ls backend/.env.example .env.example 2>/dev/null`
用存在的那个(项目根 `.env.example` 已在 git 跟踪)。

- [ ] **Step 2: 追加内存闸说明**

在 `.env.example` 末尾(或 worker 配置段)追加:

```bash
# 内存自适应并发闸 —— 调高 WORKER_THREADS 时防止多个浏览器把主机内存吃爆。
# 主机已用内存 ≥ MEM_GATE_THRESHOLD% 时,worker 暂停领取新 job(不起新浏览器),
# 内存回落自动恢复。读 /proc/meminfo,读不到则放行(fail-open)。
# 设 0 或 100 关闸。
MEM_GATE_THRESHOLD=80
# MEM_GATE_CHECK_INTERVAL=2     # 闸内查内存间隔(秒)
# MEM_GATE_MAX_WAIT=300         # 单轮最多等待(秒),超时回循环重判
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(worker): document MEM_GATE_* env vars"
```

---

## Self-Review(已执行)

**Spec 覆盖:**
- `available_percent` / `used_percent` + fail-open → Task 1 ✓
- `wait_until_ok`(关闸短路 / 超时 False / 回落 True / should_continue 提前退出)→ Task 2 ✓
- worker 接入(闸 False 不领 job、True 正常领)→ Task 3 ✓
- 3 个 env 变量(默认 80/2/300)→ Task 3(读取)+ Task 4(文档)✓
- 安全优先(超时回循环绝不硬领)→ Task 2 实现 + 测试 `test_wait_until_ok_times_out...` ✓
- on-demand 路径不动 → 计划未触碰 `ondemand/` ✓

**占位符扫描:** 无 TBD/TODO,每个代码步骤含完整代码。✓

**类型/签名一致性:** `wait_until_ok(threshold_pct, *, check_interval, max_wait, should_continue, meminfo_path)` 在 Task 2 定义、Task 3 调用一致;`used_percent` / `available_percent` 签名(可选 `meminfo_path`)在 Task 1 定义、Task 2 monkeypatch 一致;worker 模块级名 `MEM_THRESHOLD` / `MEM_CHECK_INTERVAL` / `MEM_MAX_WAIT` 在 Task 3 定义与测试引用一致。✓
