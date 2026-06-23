from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_env_int_tuple_parsing():
    from app.worker import _env_int_tuple
    import os
    os.environ["X_TEST_WS"] = "7, 8 ,abc,,9"
    try:
        assert _env_int_tuple("X_TEST_WS") == (7, 8, 9)
    finally:
        del os.environ["X_TEST_WS"]


def test_env_int_tuple_empty_returns_none():
    from app.worker import _env_int_tuple
    assert _env_int_tuple("X_NONEXISTENT_WS_VAR") is None
