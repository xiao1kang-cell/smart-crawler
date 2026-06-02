"""按 record 计费 · 用量统计 + 账单计算

================================================================================
背景
================================================================================
海尔智家大规模定制业务招标要求"资源池按订单付费"。本模块提供：
    · record_usage(): 记录一次 API 调用的用量（schema 就绪 · 中间件下个迭代加）
    · get_usage_summary(): 按 API key + 时间窗口 + endpoint 聚合 + 账单计算

定价（基础档）：$1.5 / 1k records · 量大可议（>10M records 月 → $0.8 / 1k）

================================================================================
本迭代范围
================================================================================
只有 schema + API 端点 · **不在中间件层做 metering**（避免影响线上稳定性）
中间件 + 自动计量留给下个迭代

================================================================================
使用
================================================================================
    from .billing import record_usage, get_usage_summary
    # 手动埋点（暂未自动）
    record_usage(api_key_id=1, endpoint="/api/export/products",
                 record_count=500, bytes_returned=120_000, duration_ms=820)
    # 查询当前 key 30 天用量
    summary = get_usage_summary(api_key_id=1, days=30)
================================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .agent_runtime import DEFAULT_FREE_CREDITS
from .models import ApiKey, Usage


# 基础档价格：$1.5 / 1k records
PRICE_PER_1K_RECORDS = 1.5

# 量大优惠门槛（>10M records / 月 → $0.8 / 1k）
VOLUME_DISCOUNT_THRESHOLD = 10_000_000
PRICE_PER_1K_RECORDS_BULK = 0.8


def record_usage(api_key_id: int, endpoint: str, record_count: int,
                 bytes_returned: int, duration_ms: int,
                 credits_used: int | None = None) -> None:
    """记录一次调用的用量。

    Args:
        api_key_id: ApiKey.id
        endpoint: 调用的 endpoint 路径（如 "/api/export/products"）
        record_count: 该次调用返回的 records 数
        bytes_returned: 返回字节数
        duration_ms: 调用耗时（毫秒）
        credits_used: 该次调用消耗的 credits；不传时按 record_count 兼容旧调用
    """
    with SessionLocal() as s:
        u = Usage(
            api_key_id=api_key_id,
            endpoint=endpoint,
            record_count=record_count,
            credits_used=record_count if credits_used is None else credits_used,
            bytes_returned=bytes_returned,
            duration_ms=duration_ms,
        )
        s.add(u)
        s.commit()


def _price_for(total_records: int) -> float:
    """按量阶梯定价。"""
    if total_records >= VOLUME_DISCOUNT_THRESHOLD:
        return (total_records / 1000) * PRICE_PER_1K_RECORDS_BULK
    return (total_records / 1000) * PRICE_PER_1K_RECORDS


def get_usage_summary(api_key_id: int, days: int = 30) -> dict:
    """获取某 key 最近 N 天用量 + 账单。

    Args:
        api_key_id: ApiKey.id
        days: 时间窗口（默认 30 天）

    Returns:
        {
            "api_key_id": int,
            "days": int,
            "total_calls": int,
            "total_records": int,
            "total_bytes": int,
            "cost_usd": float,
            "by_endpoint": {endpoint: record_count, ...},
        }
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    with SessionLocal() as s:
        rows = (s.query(Usage)
                  .filter(Usage.api_key_id == api_key_id,
                          Usage.occurred_at >= cutoff)
                  .all())
        key = s.get(ApiKey, api_key_id)
        total_records = sum(r.record_count or 0 for r in rows)
        total_credits = sum(getattr(r, "credits_used", 0) or 0 for r in rows)
        total_calls = len(rows)
        total_bytes = sum(r.bytes_returned or 0 for r in rows)
        record_cost_usd = _price_for(total_records)
        credit_cost_usd = _price_for(total_credits)
        quota = None
        credit_balance = None
        if key is not None:
            quota = (
                key.monthly_credit_quota
                if key.monthly_credit_quota is not None
                else DEFAULT_FREE_CREDITS
            )
            credit_balance = max(0, int(quota) - int(total_credits))

        # 按 endpoint 分组
        by_endpoint: dict[str, int] = {}
        for r in rows:
            ep = r.endpoint or "(unknown)"
            by_endpoint.setdefault(ep, 0)
            by_endpoint[ep] += r.record_count or 0

        return {
            "api_key_id": api_key_id,
            "days": days,
            "total_calls": total_calls,
            "total_records": total_records,
            "total_credits": total_credits,
            "monthly_credit_quota": quota,
            "credit_balance": credit_balance,
            "total_bytes": total_bytes,
            "billing_basis": "credits",
            "cost_usd": round(credit_cost_usd, 2),
            "estimated_cost_usd_by_credits": round(credit_cost_usd, 2),
            "estimated_cost_usd_by_records": round(record_cost_usd, 2),
            "by_endpoint": by_endpoint,
        }
