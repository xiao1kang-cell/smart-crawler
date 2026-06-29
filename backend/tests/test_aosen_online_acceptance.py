from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest


pytestmark = pytest.mark.unit


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "aosen_online_acceptance.py"
    spec = importlib.util.spec_from_file_location("aosen_online_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_gate_blocks_focus_site_with_zero_promotions():
    mod = _module()
    gate = mod.acceptance_gate(
        {
            "status": "ready",
            "summary": {"fail": 0, "needs_refresh": 0, "needs_business_data": 0},
            "templates": {
                "product_field_fixes": {},
                "sku_targets": {},
                "promotion_signals": {},
                "sales_signals": {},
                "review_history": {},
            },
        },
        {
            "items": [{
                "site": "homary_us",
                "sku_count": 10,
                "promotion_count": 0,
                "issues": [],
            }],
        },
    )

    assert gate["ready"] is False
    assert gate["focus_promotion_missing"] == ["homary_us"]
    assert "focus_promotions_missing=homary_us" in gate["blockers"]


def test_acceptance_gate_passes_ready_focus_site_with_promotions_and_templates():
    mod = _module()
    gate = mod.acceptance_gate(
        {
            "status": "ready",
            "summary": {"fail": 0, "needs_refresh": 0, "needs_business_data": 0},
            "templates": {
                "product_field_fixes": {},
                "sku_targets": {},
                "promotion_signals": {},
                "sales_signals": {},
                "review_history": {},
            },
        },
        {
            "items": [{
                "site": "vonhaus_uk",
                "sku_count": 10,
                "promotion_count": 3,
                "issues": [],
            }],
        },
    )

    assert gate["ready"] is True
    assert gate["blockers"] == []


def test_main_scopes_action_plan_and_field_quality_to_tenant(monkeypatch, capsys):
    mod = _module()
    paths = []

    def fake_request(method, path, **kwargs):
        paths.append(path)
        if path.startswith("/api/admin/spine/acceptance/aosen/action-plan?"):
            return 200, {
                "status": "ready",
                "summary": {"fail": 0, "needs_refresh": 0, "needs_business_data": 0},
                "templates": {
                    "product_field_fixes": {},
                    "sku_targets": {},
                    "promotion_signals": {},
                    "sales_signals": {},
                    "review_history": {},
                },
            }
        if path.startswith("/api/admin/spine/acceptance/aosen/field-quality?"):
            return 200, {
                "items": [{
                    "site": "homary_us",
                    "sku_count": 10,
                    "promotion_count": 2,
                    "issues": [],
                }],
            }
        return 404, {}

    monkeypatch.setattr(mod, "load_env", lambda: None)
    monkeypatch.setattr(mod, "login", lambda: "tok")
    monkeypatch.setattr(mod, "request", fake_request)

    assert mod.main(["--tenant", "1", "--template-limit", "20"]) == 0

    captured = capsys.readouterr()
    assert '"tenant": "1"' in captured.out
    assert any("tenant=1" in path and "template_limit=20" in path for path in paths)
    assert any(path.endswith("field-quality?tenant=1") for path in paths)
