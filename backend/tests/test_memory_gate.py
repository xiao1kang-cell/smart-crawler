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


def test_available_percent_fail_open_on_zero_total(tmp_path):
    from app import memory_gate

    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 0 kB\nMemAvailable: 0 kB\n")
    assert memory_gate.available_percent(str(f)) == 100.0


def test_used_percent_fail_open_on_missing_file():
    from app import memory_gate

    # 读不到文件 → used 视为 0%(fail-open,永不阻塞)
    assert memory_gate.used_percent("/no/such/meminfo") == 0.0


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
    slept = []
    monkeypatch.setattr(memory_gate.time, "sleep", lambda s: slept.append(s))

    assert memory_gate.wait_until_ok(
        80.0, check_interval=1.0, max_wait=60.0,
        should_continue=lambda: False) is False
    assert slept == []                       # 优雅停机:不该睡
