"""Request-scoped MCP auth context."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class McpApiKeyContext:
    api_key_id: int
    name: str
    scopes: list[str]


_current_api_key: ContextVar[McpApiKeyContext | None] = ContextVar(
    "smartcrawler_mcp_api_key", default=None)


def get_current_api_key() -> McpApiKeyContext | None:
    return _current_api_key.get()


def set_current_api_key(ctx: McpApiKeyContext):
    return _current_api_key.set(ctx)


def reset_current_api_key(token) -> None:
    _current_api_key.reset(token)
