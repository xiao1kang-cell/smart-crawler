from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_cli_fetch_url_invokes_runner(monkeypatch, capsys):
    import app.cli as cli
    from app.ondemand.base import OnDemandResult

    called = {}

    def fake_fetch(url, *, max_items, review_limit, do_persist=True):
        called["url"] = url
        called["max_items"] = max_items
        called["review_limit"] = review_limit
        r = OnDemandResult()
        r.add_listing({"sku": "X", "title": "t", "site": "ondemand_shopee",
                       "product_url": url})
        r.add_reviews([{"review_id": "rv"}])
        r.note("ok")
        return r

    monkeypatch.setattr(cli, "init_db", lambda: None)
    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)

    rc = cli.main(["fetch-url", "--url", "https://shopee.com.my/x-i.1.2",
                   "--max-items", "5", "--review-limit", "30"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called["url"].endswith("i.1.2")
    assert called["max_items"] == 5
    assert called["review_limit"] == 30
    assert "listing" in out.lower() or "1" in out
