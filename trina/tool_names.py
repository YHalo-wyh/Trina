"""公开工具名与持久化标识的对应关系。"""
from __future__ import annotations


_PUBLIC_NAMES = {"workspace_read": "read", "workspace_write": "write", "terminal_exec": "shell"}
_INTERNAL_NAMES = {public: internal for internal, public in _PUBLIC_NAMES.items()}


def public_tool_name(name: str) -> str:
    return _PUBLIC_NAMES.get(name, name)


def internal_tool_name(name: str) -> str:
    return _INTERNAL_NAMES.get(name, name)
