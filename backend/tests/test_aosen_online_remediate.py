from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest


pytestmark = pytest.mark.unit


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "aosen_online_remediate.py"
    spec = importlib.util.spec_from_file_location("aosen_online_remediate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_csv_files_validates_without_import_when_not_apply(tmp_path, monkeypatch):
    mod = _module()
    csv_path = tmp_path / "promotion.csv"
    csv_path.write_text("site,sku,promotion_name\nhomary_us,H-1,Deal\n", encoding="utf-8")
    calls = []

    def fake_post_json(token, path, payload):
        calls.append((token, path, payload))
        assert payload["csv"].startswith("site,sku")
        return {"valid": True, "valid_rows": 1}

    monkeypatch.setattr(mod, "post_json", fake_post_json)

    out = mod.import_csv_files(
        "tok",
        {"promotion_signals": ("/validate", "/import", csv_path)},
        apply=False,
    )

    assert out["promotion_signals"]["applied"] is False
    assert out["promotion_signals"]["validation"]["valid"] is True
    assert [call[1] for call in calls] == ["/validate"]


def test_import_csv_files_apply_stops_when_validation_fails(tmp_path, monkeypatch):
    mod = _module()
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("site,sku,date,thirty_day_sales\nhomary_us,H-1,2026-06-28,\n", encoding="utf-8")
    calls = []

    def fake_post_json(token, path, payload):
        calls.append(path)
        return {"valid": False, "errors": [{"row": 1, "errors": ["sales required"]}]}

    monkeypatch.setattr(mod, "post_json", fake_post_json)

    with pytest.raises(RuntimeError, match="sales_signals validation failed"):
        mod.import_csv_files(
            "tok",
            {"sales_signals": ("/validate", "/import", csv_path)},
            apply=True,
        )

    assert calls == ["/validate"]


def test_import_csv_files_apply_imports_after_valid_csv(tmp_path, monkeypatch):
    mod = _module()
    csv_path = tmp_path / "review_history.csv"
    csv_path.write_text("site,sku,date,review_count\nhomary_us,H-1,2026-06-01,10\n", encoding="utf-8")
    calls = []

    def fake_post_json(token, path, payload):
        calls.append(path)
        if path == "/validate":
            return {"valid": True, "valid_rows": 1}
        return {"status": "imported", "rows": 1}

    monkeypatch.setattr(mod, "post_json", fake_post_json)

    out = mod.import_csv_files(
        "tok",
        {"review_history": ("/validate", "/import", csv_path)},
        apply=True,
    )

    assert calls == ["/validate", "/import"]
    assert out["review_history"]["applied"] is True
    assert out["review_history"]["import"]["rows"] == 1


def test_action_plan_scopes_request_to_tenant(monkeypatch):
    mod = _module()
    paths = []

    def fake_request(method, path, **kwargs):
        paths.append(path)
        return 200, {"status": "ready"}

    monkeypatch.setattr(mod, "request", fake_request)

    assert mod.action_plan("tok", 80, tenant="1") == {"status": "ready"}

    assert paths == [
        "/api/admin/spine/acceptance/aosen/action-plan?template_limit=80&include_deferred=1&tenant=1"
    ]


def test_filter_plan_scope_keeps_only_matching_sites():
    mod = _module()
    plan = {
        "summary": {"sites": 3},
        "groups": {
            "promotion_refresh": {
                "items": [
                    {"site": "homary_us", "issues": ["promotions_missing"]},
                    {"site": "amazon_us_beauty", "issues": ["sales_missing"]},
                    {"site": "vidaxl_us", "issues": ["promotions_missing"]},
                ],
            },
        },
        "templates": {
            "promotion_signals": {
                "csv": (
                    "site,sku,promotion_name\n"
                    "homary_us,H-1,Deal\n"
                    "amazon_us_beauty,A-1,Deal\n"
                    "vidaxl_us,V-1,Deal\n"
                ),
            },
        },
    }

    scoped = mod.filter_plan_scope(
        plan,
        prefixes=("homary", "vidaxl"),
        exact_sites=set(),
    )

    group = scoped["groups"]["promotion_refresh"]
    assert group["sites"] == ["homary_us"]
    assert group["count"] == 1
    csv_text = scoped["templates"]["promotion_signals"]["csv"]
    assert "homary_us" in csv_text
    assert "amazon_us_beauty" not in csv_text
    assert "vidaxl_us" not in csv_text


def test_product_field_issues_flags_missing_review_count():
    mod = _module()

    issues = mod.product_field_issues({
        "site": "homary_us",
        "sku": "H-1",
        "title": "Good Product",
        "category_path": "Outdoor",
        "image_urls": ["https://example.com/p.jpg"],
        "sale_price": "12.99",
        "review_count": "",
    })

    assert issues == ["review_count_missing"]


def test_product_field_issues_accepts_zero_review_count_as_present():
    mod = _module()

    issues = mod.product_field_issues({
        "site": "homary_us",
        "sku": "H-1",
        "title": "Good Product",
        "category_path": "Outdoor",
        "image_urls": ["https://example.com/p.jpg"],
        "sale_price": "12.99",
        "review_count": "0",
    })

    assert issues == []


def test_field_rerun_sites_selects_required_field_gaps():
    mod = _module()

    sites = mod.field_rerun_sites({
        "groups": {
            "field_fixes": {
                "items": [
                    {"site": "homary_us", "issues": ["review_count_missing"]},
                    {"site": "costway_es", "issues": ["price_missing"]},
                    {"site": "currency_us", "issues": ["currency_missing"]},
                    {"site": "image_us", "issues": ["image_missing"]},
                ],
            },
        },
    })

    assert sites == ["homary_us", "costway_es"]


def test_data_quality_fallback_can_skip_slow_product_samples(monkeypatch):
    mod = _module()

    def fake_fetch_json(token, path, *, timeout=120):
        assert path == "/api/data-quality?tenant=1"
        return {
            "summary": {},
            "items": [{
                "site": "homary_us",
                "issues": ["promotions_missing", "sales_missing", "revenue_missing"],
                "sku_count": 10,
                "spu_count": 10,
                "promotion_count": 0,
            }],
        }

    def fail_fetch_products(*args, **kwargs):
        raise AssertionError("/api/products should not be called")

    monkeypatch.setattr(mod, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(mod, "fetch_site_products", fail_fetch_products)

    plan = mod.data_quality_fallback_plan(
        "tok",
        template_limit=10,
        products_per_site=3,
        product_pages_per_site=1,
        tenant="1",
        skip_product_samples=True,
    )

    assert "homary_us,," in plan["templates"]["promotion_signals"]["csv"]
    assert "homary_us,," in plan["templates"]["sales_signals"]["csv"]
