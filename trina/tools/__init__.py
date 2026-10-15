"""工具集装配。

每个子模块导出 `TOOLS: list[Tool]`；本包负责把它们装配成一个 `ToolRegistry`。
缺失的子模块会被跳过（便于分阶段开发与按需裁剪能力），但会留下告警。
"""

from __future__ import annotations

import importlib
from typing import Any, Iterable

from .registry import (
    ParamSpec,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    denied_result,
    register,
    registry,
)

#: 工具子模块（顺序即提示词中的分组顺序）
TOOL_MODULES = ("recon", "web", "vuln", "exploit", "creds")

LOAD_ERRORS: dict[str, str] = {}


def build_registry(modules: Iterable[str] | None = None) -> ToolRegistry:
    """装配所有工具。找不到的子模块会被跳过并记入 `LOAD_ERRORS`。"""
    tools = ToolRegistry()
    for name in modules or TOOL_MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name in (f"{__name__}.{name}", name):
                LOAD_ERRORS[name] = "模块不存在"
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - 单个工具模块故障不应瘫痪整个 Agent
            LOAD_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            continue
        loaded = 0
        for tool in getattr(module, "TOOLS", []) or []:
            tools.register(tool)
            loaded += 1
        LOAD_ERRORS.pop(name, None)
        if loaded == 0:
            LOAD_ERRORS[name] = "模块未导出 TOOLS"
    for tool in registry().values():  # 模块级 register() 的工具
        tools.register(tool)
    return tools


def tool_names() -> list[str]:
    return build_registry().names()


__all__ = [
    "ParamSpec",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_registry",
    "denied_result",
    "register",
    "registry",
    "tool_names",
]
