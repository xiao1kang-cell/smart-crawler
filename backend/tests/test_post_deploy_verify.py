from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

import pytest


pytestmark = pytest.mark.unit


def _module(monkeypatch, *, skip_api_key: bool):
    monkeypatch.setenv("SKIP_API_KEY_VERIFY", "1" if skip_api_key else "0")
    path = Path(__file__).resolve().parents[1] / "scripts" / "post_deploy_verify.py"
    spec = importlib.util.spec_from_file_location("post_deploy_verify", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_verify_api_key_surface_can_be_skipped_for_aosen_deploy(monkeypatch):
    mod = _module(monkeypatch, skip_api_key=True)

    results = mod.verify_api_key_surface()

    assert len(results) == 1
    assert results[0].ok is True
    assert "skipped" in results[0].detail


def test_verify_api_key_surface_fails_without_key_by_default(monkeypatch):
    monkeypatch.delenv("SMARTCRAWLER_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    mod = _module(monkeypatch, skip_api_key=False)

    results = mod.verify_api_key_surface()

    assert len(results) == 1
    assert results[0].ok is False
    assert "not set" in results[0].detail
