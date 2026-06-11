"""SP1 MCP/v2 端到端(mock scrape,不联网)。"""
from unittest.mock import patch

from app.db import SessionLocal, init_db


def _scrape_stub(db, url, **kw):
    return {"scrape_id": "scr_x", "url": url,
            "data": {"title": "MockItem", "confidence": 0.95},
            "metadata": {"canonical": None}, "html": "<html>m</html>",
            "warnings": [], "usage": {"source": "live", "credits_used": 2}}


def test_crawl_custom_source_tool():
    init_db()
    from app import mcp_server
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        out = mcp_server.crawl_custom_source(
            url="https://x.com/p/1", dataset="mcp-set", save_policy="main")
    assert out["record_id"] and out["quality_status"] == "main"
    assert out["provenance"]["source_url"] == "https://x.com/p/1"


def test_query_dataset_tool():
    init_db()
    from app import mcp_server
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        mcp_server.crawl_custom_source(url="https://x.com/p/2",
                                       dataset="mcp-q", save_policy="main")
    out = mcp_server.query_dataset(dataset="mcp-q", query="MockItem")
    assert out["total"] >= 1


def test_v2_custom_scrape_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    init_db()
    client = TestClient(app)
    r = client.post("/api/v2/custom/scrape", json={"url": "https://x.com", "dataset": "d"})
    assert r.status_code in (401, 403)  # 缺鉴权被挡
    r2 = client.post("/api/v2/dataset/query", json={"dataset": "d"})
    assert r2.status_code in (401, 403)


def test_v2_custom_scrape_and_query_end_to_end():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.apikey import generate, hash_key, short
    from app.db import SessionLocal
    from app.models import ApiKey
    init_db()
    raw = generate()
    s = SessionLocal()
    try:
        s.add(ApiKey(name="spine-v2", key_prefix=short(raw), key_hash=hash_key(raw),
                     scopes=["crawler:scrape", "crawler:read"], active=True))
        s.commit()
    finally:
        s.close()
    headers = {"X-API-Key": raw}
    client = TestClient(app)
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        r = client.post("/api/v2/custom/scrape", headers=headers,
                        json={"url": "https://x.com/p/9", "dataset": "v2-set",
                              "entity_type": "product", "save_policy": "main"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record_id"] and body["quality_status"] == "main"
    q = client.post("/api/v2/dataset/query", headers=headers,
                    json={"dataset": "v2-set", "query": "MockItem"})
    assert q.status_code == 200, q.text
    assert q.json()["total"] >= 1


def test_discovery_lists_new_tools():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    body = client.get("/.well-known/mcp.json").json()
    names = {t.get("name") for t in body.get("tools", [])}
    assert "crawl_custom_source" in names
    assert "query_dataset" in names
    assert "scrape_url" in names  # 之前漏掉的 agent-first 工具

