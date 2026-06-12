"""超管后台审计 —— 统一记录写操作。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AdminAuditLog


def record_audit(db: Session, *, actor_user_id: int | None, actor_name: str,
                 action: str, target_type: str, target_id: str | None = None,
                 detail: dict | None = None, ip: str | None = None) -> None:
    """记一条审计。调用方负责 commit(通常与被审计的写操作同事务提交)。"""
    db.add(AdminAuditLog(
        actor_user_id=actor_user_id, actor_name=actor_name, action=action,
        target_type=target_type, target_id=str(target_id) if target_id is not None else None,
        detail=detail or {}, ip=ip))
