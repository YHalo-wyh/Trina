"""ToolSpec → JSON Schema：把注册表里的工具声明翻译成模型原生 Tool Calling 用的 schema。

设计约束
--------
* **唯一数据源**：schema 只从 ``ToolSpec`` 推导，不维护第二份工具清单。
  改了工具声明，原生协议自动跟着变，不存在"schema 与实际执行不一致"。
* **只描述不执行**：本模块不调用任何工具；执行仍然只有
  ``ToolRegistry.execute()`` 一条路径（见 :mod:`trina.tools.registry`）。
* **finish 是控制面**：原生协议没有 ``final``，用一个 ``finish`` 工具表达"我要收尾"。
  它由 Agent 拦截，**永不进入注册表**，因此不构成第二条执行路径。

OpenAI / DeepSeek 兼容格式::

    {"type": "function",
     "function": {"name": "...", "description": "...",
                  "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
"""

from __future__ import annotations

import json
from typing import Any

from .registry import ParamSpec, ToolSpec

#: ParamSpec.type → JSON Schema type
TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}

#: 原生协议里代表"收尾"的控制面工具名（不是真工具）
FINISH_TOOL_NAME = "finish"

FINISH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINISH_TOOL_NAME,
        "description": (
            "结束本次任务并给出结论。只有在目标要求全部满足、或确认无法继续时才调用。"
            "调用后不会再执行任何工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "partial", "failed"],
                    "description": "任务结论；success 需要已有工具证据支撑",
                },
                "summary": {"type": "string", "description": "结论摘要，包含已验证的事实"},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
    },
}


def param_schema(spec: ParamSpec) -> dict[str, Any]:
    """单个参数 → JSON Schema 片段。

    ``enum``/``default``/``example`` 都保留，让模型看到可选值而不是靠猜；
    ``bool`` 的 default 用 ``false`` 也要写出来（否则模型倾向于省略）。
    """
    node: dict[str, Any] = {"type": TYPE_MAP.get(spec.type, "string")}
    bits = [spec.description] if spec.description else []
    if spec.enum:
        node["enum"] = list(spec.enum)
        bits.append("取值: " + " | ".join(str(item) for item in spec.enum))
    if spec.default is not None:
        node["default"] = spec.default
        bits.append(f"默认 {spec.default!r}")
    if spec.example is not None:
        node["examples"] = [spec.example]
    if bits:
        node["description"] = "；".join(bits)
    return node


def function_schema(spec: ToolSpec) -> dict[str, Any]:
    """一个 ``ToolSpec`` → OpenAI function 定义。"""
    properties = {name: param_schema(param) for name, param in spec.params.items()}
    required = [name for name, param in spec.params.items() if param.required]
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    description = spec.summary
    if spec.notes:
        description = f"{description}（{spec.notes}）"
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": description,
            "parameters": parameters,
        },
    }


def tool_schemas(
    registry: Any,
    *,
    include: Any = None,
    exclude: Any = (),
    include_finish: bool = True,
) -> list[dict[str, Any]]:
    """注册表 → 原生工具 schema 列表。

    ``include`` 给出白名单时只导出这些工具（阶段裁剪用），``exclude`` 优先。
    """
    allowed = set(include) if include else None
    blocked = set(exclude)
    rows: list[dict[str, Any]] = []
    for name in registry.names():
        if allowed is not None and name not in allowed:
            continue
        if name in blocked:
            continue
        tool = registry.get(name)
        if tool is None:
            continue
        rows.append(function_schema(tool.spec))
    if include_finish:
        rows.append(json.loads(json.dumps(FINISH_SCHEMA)))
    return rows


def parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """解析工具调用参数。

    原生协议给的是 JSON 字符串，但不同供应商会给出 ``{}``、空串、甚至已经是 dict。
    返回 ``(params, error)``；``error`` 非空时调用方应按协议错误处理，不要执行。
    """
    if isinstance(raw, dict):
        return dict(raw), ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, f"参数不是合法 JSON：{exc}"
    if not isinstance(value, dict):
        return {}, f"参数必须是 JSON 对象，得到 {type(value).__name__}"
    return value, ""


def schema_signature(schemas: list[dict[str, Any]]) -> str:
    """schema 内容指纹（顺序无关），用于审计"这一步用了哪版工具定义"。"""
    import hashlib

    canonical = json.dumps(schemas, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "FINISH_SCHEMA",
    "FINISH_TOOL_NAME",
    "TYPE_MAP",
    "function_schema",
    "param_schema",
    "parse_arguments",
    "schema_signature",
    "tool_schemas",
]
