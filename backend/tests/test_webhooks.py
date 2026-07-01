"""租户 webhook 通知测试。"""
import json
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


def test_build_payload_for_triggered_and_completed():
    from app.webhooks import build_payload

    triggered = build_payload(
        delivery_id=123,
        workspace_id=42,
        event_type="job.triggered",
        job_kind="crawl",
        job_id=987,
        status="pending",
        created_at=datetime(2026, 6, 12, 8, 29, 40),
        result={"site": "costway_de", "trigger": "manual"},
    )
    assert triggered["event"] == "job.triggered"
    assert triggered["webhook_id"] == "whd_123"
    assert triggered["workspace_id"] == 42
    assert triggered["job"]["kind"] == "crawl"
    assert triggered["job"]["result"]["site"] == "costway_de"

    completed = build_payload(
        delivery_id=124,
        workspace_id=42,
        event_type="job.completed",
        job_kind="crawl",
        job_id=987,
        status="success",
        created_at=datetime(2026, 6, 12, 8, 29, 40),
        finished_at=datetime(2026, 6, 12, 8, 31, 0),
        result={"products": 10},
    )
    assert completed["event"] == "job.completed"
    assert completed["timestamp"].startswith("2026-06-12T08:31:00")
    assert completed["job"]["finished_at"].startswith("2026-06-12T08:31:00")


def test_sign_payload_is_stable():
    from app.webhooks import sign_payload

    a = sign_payload({"b": 2, "a": 1}, "secret")
    b = sign_payload({"a": 1, "b": 2}, "secret")
    assert a == b
    assert a.startswith("sha256=")


def test_enqueue_delivery_and_dispatch(monkeypatch):
    from app.models import WebhookConfig, WebhookDelivery, Workspace
    from app.webhooks import SIGNATURE_HEADER, dispatch_pending, enqueue_delivery

    init_db()
    calls = []

    class Resp:
        status_code = 204
        text = ""

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers,
                      "timeout": timeout})
        return Resp()

    monkeypatch.setattr("app.webhooks.requests.post", fake_post)

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == "internal").first()
        db.query(WebhookDelivery).delete()
        db.query(WebhookConfig).delete()
        db.add(WebhookConfig(workspace_id=ws.id, url="https://example.com/hook",
                             secret="secret", active=True))
        db.commit()

        assert enqueue_delivery(
            db,
            workspace_id=ws.id,
            event_type="job.triggered",
            job_kind="crawl",
            job_id=1,
            status="pending",
            result={"site": "x"},
        ) == 1
        db.commit()
        row = db.query(WebhookDelivery).one()
        assert row.payload["event"] == "job.triggered"

        assert dispatch_pending(db) == 1
        db.commit()
        assert calls and calls[0]["url"] == "https://example.com/hook"
        assert calls[0]["headers"][SIGNATURE_HEADER].startswith("sha256=")
        assert db.get(WebhookDelivery, row.id).status == "success"
    finally:
        db.close()


def test_dingtalk_robot_dispatch_uses_text_message(monkeypatch):
    from app.models import WebhookConfig, WebhookDelivery, Workspace
    from app.webhooks import dispatch_pending, enqueue_delivery

    init_db()
    calls = []

    class Resp:
        status_code = 200
        text = '{"errcode":0,"errmsg":"ok"}'

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json.loads(data.decode("utf-8"))})
        return Resp()

    monkeypatch.setattr("app.webhooks.requests.post", fake_post)

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == "internal").first()
        db.query(WebhookDelivery).delete()
        db.query(WebhookConfig).delete()
        db.add(WebhookConfig(
            workspace_id=ws.id,
            url="https://oapi.dingtalk.com/robot/send?access_token=x",
            secret="secret",
            active=True,
        ))
        db.commit()

        assert enqueue_delivery(
            db,
            workspace_id=ws.id,
            event_type="job.completed",
            job_kind="crawl",
            job_id=10,
            status="success",
            result={"site": "costway_de", "products": 12},
        ) == 1
        db.commit()

        assert dispatch_pending(db) == 1
        assert calls
        assert calls[0]["body"]["msgtype"] == "text"
        assert "SmartCrawler" in calls[0]["body"]["text"]["content"]
        assert "costway_de" in calls[0]["body"]["text"]["content"]
        assert db.query(WebhookDelivery).one().status == "success"
    finally:
        db.close()


def test_webhook_settings_api_roundtrip():
    from fastapi.testclient import TestClient
    from app.auth import make_token
    from app.main import app

    init_db()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {make_token('admin')}"}

    save = client.put(
        "/api/settings/webhook",
        headers=headers,
        json={"url": "https://example.com/hook", "active": True},
    )
    assert save.status_code == 200, save.text
    assert save.json()["configured"] is True
    assert save.json()["url"] == "https://example.com/hook"

    got = client.get("/api/settings/webhook", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["configured"] is True

    deleted = client.delete("/api/settings/webhook", headers=headers)
    assert deleted.status_code == 200, deleted.text
