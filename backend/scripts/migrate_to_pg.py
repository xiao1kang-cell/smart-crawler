"""SQLite → PostgreSQL 数据迁移脚本。

把源 SQLite 库的所有表数据按 models 逐表搬到目标 PostgreSQL：
  - 自动按外键无关、(site,sku) 等唯一约束安全的顺序逐表 copy；
  - 处理 SQLite 把 datetime/date 存成字符串的问题（转回 Python 对象）；
  - 处理 JSON 列在 SQLite 里存成字符串的问题（转回 dict/list）；
  - 保留原自增主键 id，迁移完成后修正 PostgreSQL 的序列（sequence）。

用法（在 backend/ 目录下运行）：
    python scripts/migrate_to_pg.py \
        --source sqlite:////app/data/smart_crawler.db \
        --target postgresql+psycopg://smart_crawler:PASSWORD@postgres:5432/smart_crawler

参数：
    --source   源 SQLite 的 SQLAlchemy DSN（默认读环境变量 SOURCE_DATABASE_URL，
               再退回 sqlite:///<项目>/data/smart_crawler.db）
    --target   目标 PostgreSQL 的 SQLAlchemy DSN（默认读环境变量 DATABASE_URL）
    --batch    每批插入行数（默认 1000）
    --truncate 迁移前先清空目标表（默认关闭，目标表必须为空否则报错）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# 让脚本能 import app.*（scripts/ 的上级即 backend/）
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import Date, DateTime, create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _default_source() -> str:
    env = os.environ.get("SOURCE_DATABASE_URL")
    if env:
        return env
    db_path = BACKEND_DIR.parent / "data" / "smart_crawler.db"
    return f"sqlite:///{db_path}"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    p.add_argument("--source", default=_default_source(),
                   help="源 SQLite DSN")
    p.add_argument("--target", default=os.environ.get("DATABASE_URL", ""),
                   help="目标 PostgreSQL DSN")
    p.add_argument("--batch", type=int, default=1000, help="每批插入行数")
    p.add_argument("--truncate", action="store_true",
                   help="迁移前清空目标表")
    return p.parse_args()


def _coerce_datetime(value):
    """SQLite 把 datetime/date 存成字符串 —— 转回 Python 对象。"""
    if value is None or isinstance(value, (datetime, date)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 兼容 'YYYY-MM-DD HH:MM:SS[.ffffff]' / 'YYYY-MM-DDTHH:MM:SS' / 'YYYY-MM-DD'
        s = s.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        # 实在解析不了就原样交给驱动
        return value
    return value


def _coerce_date(value):
    dt = _coerce_datetime(value)
    if isinstance(dt, datetime):
        return dt.date()
    return dt


def _coerce_json(value):
    """SQLite 的 JSON 列可能存成字符串 —— 转回 dict/list。"""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return value
    return value


def main() -> int:
    args = _parse_args()
    if not args.target:
        print("错误：未提供目标 DSN（--target 或环境变量 DATABASE_URL）",
              file=sys.stderr)
        return 1
    if not args.target.startswith(("postgresql", "postgres")):
        print(f"错误：目标必须是 PostgreSQL，得到 {args.target!r}", file=sys.stderr)
        return 1

    print(f"源 : {args.source}")
    print(f"目标: {args.target}")

    src_engine = create_engine(args.source, future=True)
    dst_engine = create_engine(args.target, future=True)

    # 导入模型并在目标库建表
    from app.db import Base  # noqa: F401
    from app import models  # noqa: F401

    print("→ 在目标 PostgreSQL 建表 …")
    Base.metadata.create_all(dst_engine)

    SrcSession = sessionmaker(bind=src_engine, future=True)
    DstSession = sessionmaker(bind=dst_engine, future=True)

    # 按 metadata 的拓扑顺序（无外键依赖此项目其实无所谓，但稳妥）
    tables = list(Base.metadata.sorted_tables)

    # 识别每张表里需要类型转换的列
    from sqlalchemy import JSON

    total_rows = 0
    with SrcSession() as src, DstSession() as dst:
        for table in tables:
            cols = list(table.columns)
            colnames = [c.name for c in cols]
            dt_cols = {c.name for c in cols if isinstance(c.type, DateTime)}
            date_cols = {c.name for c in cols
                         if isinstance(c.type, Date) and not isinstance(c.type, DateTime)}
            json_cols = {c.name for c in cols if isinstance(c.type, JSON)}

            # 目标表非空检查 / 清空
            existing = dst.execute(
                text(f"SELECT COUNT(*) FROM {table.name}")).scalar() or 0
            if existing:
                if args.truncate:
                    print(f"  {table.name}: 目标已有 {existing} 行 → 清空")
                    dst.execute(text(
                        f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
                else:
                    print(f"错误：目标表 {table.name} 非空（{existing} 行），"
                          f"加 --truncate 才会覆盖", file=sys.stderr)
                    return 1

            src_rows = src.execute(table.select()).mappings().all()
            if not src_rows:
                print(f"  {table.name}: 源为空，跳过")
                continue

            buf: list[dict] = []
            inserted = 0
            for row in src_rows:
                rec = dict(row)
                for k in dt_cols:
                    if k in rec:
                        rec[k] = _coerce_datetime(rec[k])
                for k in date_cols:
                    if k in rec:
                        rec[k] = _coerce_date(rec[k])
                for k in json_cols:
                    if k in rec:
                        rec[k] = _coerce_json(rec[k])
                # 只保留目标表存在的列
                rec = {k: v for k, v in rec.items() if k in colnames}
                buf.append(rec)
                if len(buf) >= args.batch:
                    dst.execute(table.insert(), buf)
                    inserted += len(buf)
                    buf = []
            if buf:
                dst.execute(table.insert(), buf)
                inserted += len(buf)

            print(f"  {table.name}: 迁移 {inserted} 行")
            total_rows += inserted
        dst.commit()

        # 修正 PostgreSQL 自增序列 —— 否则后续 INSERT 主键冲突
        print("→ 修正 PostgreSQL 自增序列 …")
        for table in tables:
            if "id" not in [c.name for c in table.columns]:
                continue
            # setval 到当前 max(id)；表为空时设为 1 且 is_called=false
            dst.execute(text(
                f"SELECT setval("
                f"  pg_get_serial_sequence('{table.name}', 'id'),"
                f"  COALESCE((SELECT MAX(id) FROM {table.name}), 1),"
                f"  (SELECT MAX(id) IS NOT NULL FROM {table.name})"
                f")"
            ))
        dst.commit()

    print(f"✅ 迁移完成，共 {total_rows} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
