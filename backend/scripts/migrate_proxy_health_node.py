"""为 proxy_health 表加 node 列并回填现有行为 'nas'，重建唯一约束。

健康度从「proxy_hash 全局唯一」改为「(proxy_hash, node) 组合唯一」，支持
代理健康度按出口节点隔离（NAS / 各 Mac mini 各自独立健康视角）。

幂等：可重复运行。在生产 PostgreSQL 上执行一次。
用法：cd backend && python scripts/migrate_proxy_health_node.py
"""
from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        # 1. 加列（IF NOT EXISTS：PG 9.6+ 支持）
        conn.execute(text(
            "ALTER TABLE proxy_health ADD COLUMN IF NOT EXISTS node VARCHAR"
        ))
        # 2. 回填历史行
        conn.execute(text(
            "UPDATE proxy_health SET node='nas' WHERE node IS NULL"
        ))
        # 3. 删旧的单列 proxy_hash 唯一约束（生产库可能是 SQLAlchemy/PG 自动命名）
        conn.execute(text("""
            DO $$
            DECLARE
                r record;
            BEGIN
                FOR r IN
                    SELECT c.conname
                    FROM pg_constraint c
                    WHERE c.conrelid = 'proxy_health'::regclass
                      AND c.contype = 'u'
                      AND c.conname <> 'uq_proxy_health_hash_node'
                      AND (
                          SELECT array_agg(a.attname::text ORDER BY u.ord)
                          FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                          JOIN pg_attribute a
                            ON a.attrelid = c.conrelid
                           AND a.attnum = u.attnum
                      ) = ARRAY['proxy_hash']
                LOOP
                    EXECUTE format('ALTER TABLE proxy_health DROP CONSTRAINT %I', r.conname);
                END LOOP;
            END $$;
        """))
        # 4. 建新组合唯一约束（若不存在）
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_proxy_health_hash_node'
                ) THEN
                    ALTER TABLE proxy_health
                        ADD CONSTRAINT uq_proxy_health_hash_node UNIQUE (proxy_hash, node);
                END IF;
            END $$;
        """))
        # 5. node 索引
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_proxy_health_node ON proxy_health (node)"
        ))
    print("proxy_health node 迁移完成")


if __name__ == "__main__":
    main()
