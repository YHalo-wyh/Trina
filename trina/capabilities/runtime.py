"""Capability 到现有 ToolRegistry 执行链的最小适配层。"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from ..tools import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec
from ..tools.external import run_external, run_mcp_stdio
from .catalog import Capability


Publish = Callable[[Capability, ToolResult, dict[str, Any]], None]
LeaseCheck = Callable[[], None]


class CapabilityRuntime:
    """把租约内 Capability 投影为普通工具，不引入第二条执行通道。"""

    def __init__(self, base_registry: ToolRegistry) -> None:
        self.base_registry = base_registry

    def available(self, capability: Capability) -> tuple[bool, str]:
        kind = str(capability.runtime.get("type") or "").lower()
        if kind == "tool":
            tool = self.base_registry.get(str(capability.runtime.get("tool") or capability.tool_name))
            return tool.available() if tool is not None else (False, "对应 ToolRegistry 工具不存在")
        try:
            command = self.command(capability)
        except ValueError as exc:
            return False, str(exc)
        binary = command[0]
        return (True, "") if shutil.which(binary) or Path(binary).is_file() else (False, f"启动程序不存在: {binary}")

    def bind(
        self,
        capabilities: Iterable[Capability],
        *,
        check_lease: LeaseCheck,
        publish: Publish,
    ) -> tuple[ToolRegistry, dict[str, str]]:
        registry = ToolRegistry()
        mapping: dict[str, str] = {}
        for capability in capabilities:
            kind = str(capability.runtime.get("type") or "").lower()
            original = None
            if kind == "tool":
                original = self.base_registry.get(str(capability.runtime.get("tool") or capability.tool_name))
                if original is None:
                    raise ValueError(f"Capability {capability.id} 对应工具不存在")
            wrapped = _CapabilityTool(capability, self, original, check_lease, publish)
            registry.register(wrapped)
            mapping[capability.id] = wrapped.spec.name
        return registry, mapping

    def invoke(self, capability: Capability, ctx: ToolContext, params: dict[str, Any]) -> ToolResult:
        kind = str(capability.runtime.get("type") or "").lower()
        _check_workspace_paths(capability, ctx.workspace, params)
        command = self.command(capability)
        filesystem = str(
            capability.policy.get("filesystem")
            or capability.requires.get("filesystem")
            or "workspace"
        )
        if filesystem not in {"workspace", "manifest"}:
            return ToolResult(
                ok=False,
                summary=f"{capability.id} 的 filesystem 策略不受支持",
                error=f"unsupported filesystem policy: {filesystem}",
            )
        cwd = str(ctx.workspace) if filesystem == "workspace" else str(Path(capability.source).resolve().parent)
        target = str(params.get("target") or params.get("url") or params.get("host") or "")
        level = str(capability.policy.get("action_level") or "detect")
        timeout = float(capability.policy.get("timeout") or ctx.config.run.tool_timeout)
        env = capability.runtime.get("env") if isinstance(capability.runtime.get("env"), dict) else {}

        if kind in {"mcp", "mcp_stdio"}:
            mcp_tool = str(capability.runtime.get("tool") or "").strip()
            if not mcp_tool:
                return ToolResult(ok=False, summary=f"{capability.id} 缺少 MCP tool", error="runtime.tool 不能为空")
            call = run_mcp_stdio(
                command,
                mcp_tool,
                params,
                ctx,
                target=target,
                required_level=level,
                timeout=timeout,
                cwd=cwd,
                protocol_version=str(capability.runtime.get("protocol") or "2025-11-25"),
                env={str(key): str(value) for key, value in env.items()},
            )
            payload = call.result
            structured = payload.get("structuredContent") if isinstance(payload.get("structuredContent"), dict) else {}
            text = _mcp_text(payload)
            summary = str(structured.get("summary") or text or call.error or capability.description)[:500]
            return ToolResult(
                ok=call.ok,
                summary=summary,
                error=call.error,
                data=structured or {"content": payload.get("content") or []},
                raw=text or json.dumps(payload, ensure_ascii=False, default=str),
                command=call.command,
                duration=call.duration,
                facts=dict(structured.get("facts") or {}) if isinstance(structured.get("facts"), dict) else {},
                evidence=list(structured.get("evidence") or []) if isinstance(structured.get("evidence"), list) else [],
                category=str(capability.policy.get("category") or "capability"),
            )

        if kind == "cli":
            argv = [*command, *_render_argv(capability.runtime.get("argv") or (), params)]
            input_data = json.dumps(params, ensure_ascii=False) if capability.runtime.get("input") == "json" else None
            call = run_external(
                argv,
                ctx,
                tool=capability.id,
                target=target,
                required_level=level,
                timeout=timeout,
                input_data=input_data,
                cwd=cwd,
                env=_capability_environment(env),
            )
            payload: dict[str, Any] = {}
            if capability.runtime.get("output") == "json" and call.stdout.strip():
                try:
                    decoded = json.loads(call.stdout)
                    payload = decoded if isinstance(decoded, dict) else {"value": decoded}
                except json.JSONDecodeError:
                    return ToolResult(ok=False, summary=f"{capability.id} 输出不是合法 JSON", error="invalid JSON", raw=call.stdout, command=call.command)
            summary = str(payload.get("summary") or call.output or call.error or capability.description).strip()[:500]
            return ToolResult(
                ok=call.ok,
                summary=summary,
                error=call.error or (call.stderr[:500] if not call.ok else ""),
                data=payload or call.to_dict(),
                raw=call.stdout,
                command=call.command,
                duration=call.duration,
                facts=dict(payload.get("facts") or {}) if isinstance(payload.get("facts"), dict) else {},
                evidence=list(payload.get("evidence") or []) if isinstance(payload.get("evidence"), list) else [],
                category=str(capability.policy.get("category") or "capability"),
            )

        return ToolResult(ok=False, summary=f"不支持的 Capability runtime: {kind}", error=kind)

    @staticmethod
    def command(capability: Capability) -> list[str]:
        value = capability.runtime.get("command")
        if isinstance(value, str):
            command = shlex.split(value, posix=os.name != "nt")
        elif isinstance(value, list):
            command = [str(item) for item in value]
        else:
            command = []
        if not command:
            raise ValueError(f"Capability {capability.id} runtime.command 不能为空")
        if capability.source and not capability.source.startswith("<"):
            base = Path(capability.source).resolve().parent
            for index in range(1, len(command)):
                candidate = Path(command[index])
                if not candidate.is_absolute() and (base / candidate).is_file():
                    command[index] = str((base / candidate).resolve())
        return command


class _CapabilityTool(Tool):
    """租约感知的工具薄壳；执行、超时、状态吸收仍由外层 ToolRegistry 完成。"""

    def __init__(
        self,
        capability: Capability,
        runtime: CapabilityRuntime,
        original: Tool | None,
        check_lease: LeaseCheck,
        publish: Publish,
    ) -> None:
        self.capability = capability
        self.runtime = runtime
        self.original = original
        self.check_lease = check_lease
        self.publish = publish
        self.spec = original.spec if original is not None else _tool_spec(capability)

    def available(self) -> tuple[bool, str]:
        return self.original.available() if self.original is not None else self.runtime.available(self.capability)

    def required_level(self, params: dict[str, Any]) -> str:
        return self.original.required_level(params) if self.original is not None else self.spec.action_level

    def level_hint(self, params: dict[str, Any]) -> str:
        return self.original.level_hint(params) if self.original is not None else ""

    def run(self, ctx: ToolContext, **params: Any) -> ToolResult:
        self.check_lease()
        result = self.original.run(ctx, **params) if self.original is not None else self.runtime.invoke(self.capability, ctx, params)
        self.check_lease()
        try:
            self.publish(self.capability, result, params)
        except Exception as exc:  # noqa: BLE001 - 索引失败不能伪造工具失败
            ctx.audit.append("capability.publish.error", capability=self.capability.id, error=f"{type(exc).__name__}: {exc}"[:300])
        return result


def _tool_spec(capability: Capability) -> ToolSpec:
    interface = dict(capability.interface)
    raw_params = interface.get("params") if isinstance(interface.get("params"), dict) else {}
    required_names: set[str] = set()
    if raw_params.get("type") == "object" and isinstance(raw_params.get("properties"), dict):
        required_names = {str(item) for item in raw_params.get("required") or ()}
        raw_params = raw_params["properties"]
    params: dict[str, ParamSpec] = {}
    for name, raw in raw_params.items():
        raw = dict(raw or {}) if isinstance(raw, dict) else {}
        param_type = str(raw.get("type") or "str")
        param_type = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list", "object": "dict"}.get(param_type, param_type)
        required = bool(raw.get("required", name in required_names))
        params[str(name)] = ParamSpec(
            param_type,
            str(raw.get("description") or ""),
            required=required,
            default=raw.get("default"),
            enum=list(raw.get("enum")) if isinstance(raw.get("enum"), list) else None,
        )
    return ToolSpec(
        name=capability.tool_name,
        summary=capability.description,
        category=str(capability.policy.get("category") or "capability"),
        action_level=str(capability.policy.get("action_level") or "detect"),
        params=params,
        destructive=bool(capability.policy.get("destructive", False)),
        external_bin=(CapabilityRuntime.command(capability)[0] if capability.runtime.get("command") else None),
        produces=[str(item).removeprefix("fact.") for item in capability.provides if str(item).startswith("fact.")],
    )


def _render_argv(template: Iterable[Any], params: dict[str, Any]) -> list[str]:
    values = {key: (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)) for key, value in params.items()}
    rendered: list[str] = []
    for item in template:
        try:
            rendered.append(str(item).format_map(values))
        except KeyError as exc:
            raise ValueError(f"CLI argv 引用了不存在的参数: {exc.args[0]}") from exc
    return rendered


def _check_workspace_paths(capability: Capability, workspace: Path, params: dict[str, Any]) -> None:
    path_params = capability.policy.get("path_params") or ()
    if not isinstance(path_params, (list, tuple)):
        raise ValueError(f"Capability {capability.id} policy.path_params 必须是列表")
    root = workspace.resolve()
    for name in path_params:
        if name not in params:
            continue
        value = Path(str(params[name])).expanduser()
        path = value.resolve() if value.is_absolute() else (root / value).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Capability {capability.id} 参数 {name} 越出 Worker workspace") from exc


def _mcp_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for block in payload.get("content") or ()
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _capability_environment(extra: dict[str, Any]) -> dict[str, str]:
    """外部能力默认不继承宿主凭据，只保留启动进程所需环境。"""
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
        "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result["PYTHONUNBUFFERED"] = "1"
    for key, value in extra.items():
        name, text = str(key), str(value)
        if not name or "\x00" in name or "\x00" in text:
            raise ValueError("Capability 环境变量非法")
        result[name] = text
    return result


__all__ = ["CapabilityRuntime"]
