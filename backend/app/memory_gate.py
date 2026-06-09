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
                # isdigit() 排除负数 —— /proc/meminfo 数值恒 ≥ 0
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
    check_interval = max(check_interval, 0.001)   # 防 0 间隔死循环空转
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
