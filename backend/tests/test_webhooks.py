"""租户 webhook 通知测试。"""
from datetime import datetime

from sqlalchemy import inspect

from app.db import SessionLocal, engine, init_db


def test_webhook_tables_exist():
    init_db()
    insp = inspect(engine)
    assert insp.has_table("webhook_configs"), "缺表 webhook_configs"
    assert insp.has_table("webhook_deliveries"), "缺表 webhook_deliveries"
    cfg_cols = {c["name"] for c in insp.get_columns("webhook_configs")}
    for c in ("id", "workspace_id", "url", "secret", "active",
              "created_at", "updated_at"):
        assert c in cfg_cols, f"webhook_configs 缺列 {c}"
    del_cols = {c["name"] for c in insp.get_columns("webhook_deliveries")}
    for c in ("id", "workspace_id", "config_id", "event_type", "job_kind",
              "job_id", "payload", "status", "retries", "max_retries",
              "next_retry_at", "http_status", "response_snippet",
              "created_at", "finished_at"):
        assert c in del_cols, f"webhook_deliveries 缺列 {c}"
