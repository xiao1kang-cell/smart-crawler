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
