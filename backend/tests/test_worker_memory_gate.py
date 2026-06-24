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
                        lambda wid, trigger_allowlist=None, **kwargs: claimed.append(wid))
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    # should_continue:跑 2 轮后停,避免死循环
    ticks = iter([True, True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert len(gate_calls) == 2            # 两轮都被闸住
    assert claimed == []                   # 闸挡住 → 从没领 job


def test_run_loop_claims_when_gate_open(monkeypatch):
    """内存闸放行时,正常领 job。"""
    from app import worker

    monkeypatch.setattr(worker, "MEM_THRESHOLD", 80.0)
    monkeypatch.setattr(worker.memory_gate, "wait_until_ok",
                        lambda threshold, **kw: True)

    claimed = []

    def fake_claim(wid, trigger_allowlist=None, **kwargs):
        claimed.append(wid)
        return None                        # 没有 job,走 sleep continue 分支

    monkeypatch.setattr(worker, "claim_job", fake_claim)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    ticks = iter([True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert claimed                         # 闸放行 → 领了 job


def test_run_loop_sleeps_and_warns_when_blocked_by_high_memory(monkeypatch):
    """闸挡住且内存仍高时:记一条 warning + sleep 一拍(防 MEM_MAX_WAIT=0 空转)。"""
    from app import worker

    monkeypatch.setattr(worker, "MEM_THRESHOLD", 80.0)
    monkeypatch.setattr(worker.memory_gate, "wait_until_ok",
                        lambda threshold, **kw: False)   # 一直挡
    # 内存仍高(95% ≥ 阈值 80)→ 应进 log+sleep 分支
    monkeypatch.setattr(worker.memory_gate, "used_percent",
                        lambda *a, **k: 95.0)

    slept = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    warnings = []
    monkeypatch.setattr(worker.logger, "warning",
                        lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(worker, "claim_job",
                        lambda wid, trigger_allowlist=None, **kwargs: pytest.fail("内存高位不该领 job"))

    ticks = iter([True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert slept == [worker.POLL_INTERVAL]   # sleep 了一拍,没空转
    assert warnings                          # 记了 warning,运维可见


def test_run_loop_shutdown_during_block_exits_without_sleep(monkeypatch):
    """闸挡住但内存已回落(=停机路径)时:不 sleep、快速退出。"""
    from app import worker

    monkeypatch.setattr(worker, "MEM_THRESHOLD", 80.0)
    monkeypatch.setattr(worker.memory_gate, "wait_until_ok",
                        lambda threshold, **kw: False)
    # 内存已回落(30% < 阈值)→ False 是停机所致,不该 sleep
    monkeypatch.setattr(worker.memory_gate, "used_percent",
                        lambda *a, **k: 30.0)

    slept = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))

    ticks = iter([True, False])
    worker.run_loop(should_continue=lambda: next(ticks, False))

    assert slept == []                       # 停机路径:不 sleep,快速退出
