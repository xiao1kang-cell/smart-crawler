"""任务表过大时，归档已成功的历史任务行。"""
import os
import re
import time
from typing import Dict, List

from loguru import logger
from sqlalchemy import inspect, text

from app.db import IS_SQLITE, engine
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB

# 需要自动归档的任务表。这里只处理成功任务（status=2），失败任务保留在当前表排查。
ARCHIVE_TABLES: List[str] = [
    "crawl_single_tasks",
    "crawl_asin_detail_tasks",
]

# 默认超过 500 万行才触发检查，可通过环境变量 TASK_TABLE_ARCHIVE_THRESHOLD 调整。
TASK_TABLE_ARCHIVE_THRESHOLD = int(os.getenv("TASK_TABLE_ARCHIVE_THRESHOLD", "5000000"))


def archive_oversized_task_tables(
    threshold: int = TASK_TABLE_ARCHIVE_THRESHOLD,
) -> List[Dict]:
    """检查配置的任务表，超过阈值时归档 status=2 的成功任务。"""
    db = MySQLTaskDB()
    if not getattr(db, "supports_legacy_mysql_tables", True):
        return _archive_postgres_job_tables(threshold)
    archived = []
    try:
        for table in ARCHIVE_TABLES:
            result = _archive_one_if_needed(db, table, threshold)
            if result:
                archived.append(result)
    finally:
        db.close()
    return archived


POSTGRES_ARCHIVE_TABLES: List[str] = [
    "amazon_review_jobs",
    "amazon_listing_jobs",
]


def _archive_postgres_job_tables(threshold: int) -> List[Dict]:
    archived = []
    for table in POSTGRES_ARCHIVE_TABLES:
        result = _archive_postgres_one_if_needed(table, threshold)
        if result:
            archived.append(result)
    return archived


def _archive_postgres_one_if_needed(table: str, threshold: int) -> Dict:
    if not _valid_table_name(table):
        raise ValueError(f"invalid table name: {table}")
    insp = inspect(engine)
    if not insp.has_table(table):
        return {}
    with engine.begin() as conn:
        total_rows = int(conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
        if total_rows < threshold:
            return {}
        active_rows = int(conn.execute(
            text(f'SELECT COUNT(*) FROM "{table}" WHERE status IN (:queued, :running)'),
            {"queued": "queued", "running": "running"},
        ).scalar() or 0)
        if active_rows > 0:
            logger.warning(
                f"[TaskTableArchive] 跳过 {table}: total={total_rows}, active={active_rows}; "
                "存在 queued/running 任务，等待任务执行完再归档"
            )
            return {}
        cutoff_id = int(conn.execute(text(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')).scalar() or 0)
        if cutoff_id <= 0:
            return {}
        archive_rows = int(conn.execute(
            text(f'SELECT COUNT(*) FROM "{table}" WHERE id <= :cutoff_id AND status = :status'),
            {"cutoff_id": cutoff_id, "status": "completed"},
        ).scalar() or 0)
        if archive_rows <= 0:
            return {}
        backup_table = _next_postgres_backup_name(table)
        logger.warning(
            f"[TaskTableArchive] 开始归档 {table}: rows={total_rows}, completed_rows={archive_rows}, "
            f"cutoff_id={cutoff_id}, backup={backup_table}"
        )
        if IS_SQLITE:
            conn.execute(text(f'CREATE TABLE "{backup_table}" AS SELECT * FROM "{table}" WHERE 0'))
        else:
            conn.execute(text(f'CREATE TABLE "{backup_table}" (LIKE "{table}" INCLUDING ALL)'))
        inserted = conn.execute(
            text(f'INSERT INTO "{backup_table}" SELECT * FROM "{table}" WHERE id <= :cutoff_id AND status = :status'),
            {"cutoff_id": cutoff_id, "status": "completed"},
        )
        deleted = conn.execute(
            text(
                f'DELETE FROM "{table}" WHERE status = :status '
                f'AND id IN (SELECT id FROM "{backup_table}")'
            ),
            {"status": "completed"},
        )
    inserted_rows = int(inserted.rowcount or archive_rows or 0)
    deleted_rows = int(deleted.rowcount or 0)
    logger.warning(
        f"[TaskTableArchive] 已归档 {table} -> {backup_table}, "
        f"inserted={inserted_rows}, deleted={deleted_rows}"
    )
    return {
        "table": table,
        "backup_table": backup_table,
        "rows": total_rows,
        "active_rows": active_rows,
        "archived_rows": inserted_rows,
        "deleted_rows": deleted_rows,
        "cutoff_id": cutoff_id,
    }


def _next_postgres_backup_name(table: str) -> str:
    insp = inspect(engine)
    base = f"{table}_{time.strftime('%Y%m%d_%H%M%S')}"
    backup = base
    suffix = 1
    while insp.has_table(backup):
        backup = f"{base}_{suffix}"
        suffix += 1
    return backup


def _archive_one_if_needed(db: MySQLTaskDB, table: str, threshold: int) -> Dict:
    """单表归档入口：满足阈值且没有待执行/执行中任务时才归档。"""
    if not _valid_table_name(table):
        raise ValueError(f"invalid table name: {table}")
    db._check_connection()
    if not db._table_exists(table):
        return {}

    approx_rows = _approx_rows(db, table)
    # InnoDB 的 information_schema.table_rows 是估算值。
    # 只有明显低于阈值时才直接跳过，接近阈值时再 COUNT(*) 精确确认。
    if approx_rows is not None and approx_rows < int(threshold * 0.8):
        return {}

    total_rows = _count_rows(db, table)
    if total_rows < threshold:
        return {}

    active_rows = _count_active_rows(db, table)
    if active_rows > 0:
        logger.warning(
            f"[TaskTableArchive] 跳过 {table}: total={total_rows}, active={active_rows}; "
            "存在 status=0/1 任务，等待任务执行完再归档"
        )
        return {}

    # 记录本轮归档开始时的最大 id。
    # 清理期间如果 API 又插入新任务，新任务 id 会更大，不会被本轮搬到备份表。
    cutoff_id = _max_id(db, table)
    if cutoff_id <= 0:
        return {}

    # 只归档成功任务。status=3 的失败任务保留在当前表，方便后续排查。
    archive_rows = _count_archivable_rows(db, table, cutoff_id)
    if archive_rows <= 0:
        return {}

    backup_table = _next_backup_name(db, table)
    logger.warning(
        f"[TaskTableArchive] 开始归档 {table}: rows={total_rows}, status2_rows={archive_rows}, "
        f"cutoff_id={cutoff_id}, backup={backup_table}"
    )

    # 备份表结构完全复用原表，表名格式如 crawl_single_tasks_20260525_153000。
    db.cursor.execute(f"CREATE TABLE `{backup_table}` LIKE `{table}`")
    db.cursor.execute(
        f"INSERT INTO `{backup_table}` SELECT * FROM `{table}` "
        "WHERE id <= %s AND status = 2",
        (cutoff_id,),
    )
    inserted_rows = int(db.cursor.rowcount or 0)

    # 只删除已经成功插入备份表的 id，避免并发状态变化导致误删未备份数据。
    db.cursor.execute(
        f"DELETE current_table FROM `{table}` AS current_table "
        f"INNER JOIN `{backup_table}` AS backup_table ON current_table.id = backup_table.id "
        "WHERE current_table.status = 2"
    )
    deleted_rows = int(db.cursor.rowcount or 0)
    db.conn.commit()

    logger.warning(
        f"[TaskTableArchive] 已归档 {table} -> {backup_table}, "
        f"inserted={inserted_rows}, deleted={deleted_rows}"
    )
    return {
        "table": table,
        "backup_table": backup_table,
        "rows": total_rows,
        "active_rows": active_rows,
        "archived_rows": inserted_rows,
        "deleted_rows": deleted_rows,
        "cutoff_id": cutoff_id,
    }


def _approx_rows(db: MySQLTaskDB, table: str):
    """读取 MySQL 维护的估算行数，用于减少大表 COUNT(*) 次数。"""
    db.cursor.execute(
        """
        SELECT table_rows
        FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = %s
        """,
        (table,),
    )
    row = db.cursor.fetchone() or {}
    value = row.get("table_rows")
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _count_rows(db: MySQLTaskDB, table: str) -> int:
    """精确统计表总行数。"""
    db.cursor.execute(f"SELECT COUNT(*) AS cnt FROM `{table}`")
    return int((db.cursor.fetchone() or {}).get("cnt") or 0)


def _count_active_rows(db: MySQLTaskDB, table: str) -> int:
    """统计还不能归档的任务：status=0 待执行，status=1 执行中。"""
    db.cursor.execute(f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE status IN (0, 1)")
    return int((db.cursor.fetchone() or {}).get("cnt") or 0)


def _count_archivable_rows(db: MySQLTaskDB, table: str, cutoff_id: int) -> int:
    """统计本轮可以归档的成功任务数量。"""
    db.cursor.execute(
        f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE id <= %s AND status = 2",
        (cutoff_id,),
    )
    return int((db.cursor.fetchone() or {}).get("cnt") or 0)


def _max_id(db: MySQLTaskDB, table: str) -> int:
    """取当前表最大 id，作为本轮归档边界。"""
    db.cursor.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM `{table}`")
    return int((db.cursor.fetchone() or {}).get("max_id") or 0)


def _next_backup_name(db: MySQLTaskDB, table: str) -> str:
    """生成不冲突的备份表名。"""
    base = f"{table}_{time.strftime('%Y%m%d_%H%M%S')}"
    backup = base
    suffix = 1
    while db._table_exists(backup):
        backup = f"{base}_{suffix}"
        suffix += 1
    return backup


def _valid_table_name(table: str) -> bool:
    """限制表名字符，避免动态 SQL 拼接风险。"""
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", table or ""))
