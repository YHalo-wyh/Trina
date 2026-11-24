"""工具层核心契约：ToolSpec / ToolResult / ToolContext / ToolRegistry。

**单一收口**：所有工具调用都必须经过 `ToolRegistry.execute()`，它负责
参数校验 → 动作等级与范围护栏 → 审计留痕 → 超时与异常兜底 → 结果归一。

新增工具的写法（放在 `trina/tools/` 下并在 `trina/tools/__init__.py` 导入）::

    from .registry import ParamSpec, Tool, ToolResult, ToolSpec

    class MyTool(Tool):
        spec = ToolSpec(
            name="my_tool",
            summary="一句话说明，会直接进大模型的工具清单",
            category="web",
            action_level="detect",
            params={"url": ParamSpec("str", "目标 URL", required=True)},
        )

        def run(self, ctx, url):
            result = ctx.http.get(url)
            return ToolResult(ok=result.ok, summary=f"GET {url} -> {result.status}",
                              data=result.to_dict(), evidence=[result.as_evidence()])

    TOOLS = [MyTool()]
"""

from __future__ import annotations

import inspect
import copy
import time
import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..audit import AuditLog, sha256_text
from ..config import ACTION_LEVELS, TrinaConfig
from ..scope import Decision, ScopeGuard, ScopeViolation
from ..tool_names import internal_tool_name, public_tool_name
from ..state import RunState

# ---------------------------------------------------------------------------
# 规格
# ---------------------------------------------------------------------------

PARAM_TYPES = ("str", "int", "float", "bool", "list", "dict")


@dataclass
class ParamSpec:
    type: str = "str"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: list[Any] | None = None
    example: Any = None

    def render(self) -> str:
        bits = [f"{self.type}"]
        if not self.required:
            bits.append(f"可选, 默认={self.default!r}")
        if self.enum:
            bits.append("取值=" + "|".join(str(e) for e in self.enum))
        if self.description:
            bits.append(self.description)
        return f"({'；'.join(bits)})"


@dataclass
class ToolSpec:
    name: str
    summary: str
    category: str = "utility"  # recon|web|vuln|exploit|utility
    action_level: str = "detect"  # detect|verify|exploit
    params: dict[str, ParamSpec] = field(default_factory=dict)
    destructive: bool = False
    external_bin: str | None = None  # 优先使用的专业工具（缺失则内置引擎兜底）
    produces: list[str] = field(default_factory=list)  # ports/services/findings/credentials/flags/files
    examples: list[str] = field(default_factory=list)
    notes: str = ""

    def render(self, verbose: bool = False, *, include_examples: bool = True) -> str:
        head = f"### {public_tool_name(self.name)} [{self.category}/{self.action_level}]"
        lines = [head, self.summary]
        if self.params:
            lines.append("参数:")
            for pname, pspec in self.params.items():
                lines.append(f"  - {pname}: {pspec.render()}")
        if self.examples and include_examples:
            lines.append("示例: " + " | ".join(self.examples[:3]))
        if self.external_bin and verbose:
            lines.append(f"外部工具: {self.external_bin}（缺失时自动降级到内置引擎）")
        if self.notes and verbose:
            lines.append(f"说明: {self.notes}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """工具返回值。`summary` 会作为观察结果回灌给大模型，必须短且信息密度高。"""

    ok: bool = True
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    command: str = ""
    error: str = ""
    duration: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    next_hints: list[str] = field(default_factory=list)
    denied: bool = False
    category: str = ""
    #: 面向模型的确定性重点，完整结果仍保留在 data/raw/evidence。
    key_findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifact_ref: str = ""
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_observation(self, max_chars: int = 2600) -> str:
        """生成可追溯、确定性的模型观察视图。"""
        view = ToolResultView.from_result(self, max_chars=max_chars)
        self.truncated = self.truncated or view.truncated
        if view.artifact_ref and not self.artifact_ref:
            self.artifact_ref = view.artifact_ref
        return view.render()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "error": self.error,
            "denied": self.denied,
            "command": self.command,
            "duration": round(self.duration, 3),
            "data": self.data,
            "hints": self.next_hints,
            "key_findings": self.key_findings,
            "errors": self.errors,
            "artifact_ref": self.artifact_ref,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ToolResultView:
    """工具原始结果与模型可见摘要之间的稳定边界。"""

    status: str
    summary: str
    key_findings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    data: Any = None
    artifact_ref: str = ""
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: ToolResult, *, max_chars: int = 2600) -> "ToolResultView":
        status = "OK" if result.ok else ("被护栏拒绝" if result.denied else "失败")
        errors = [str(item) for item in result.errors if str(item)]
        if result.error and result.error not in errors:
            errors.insert(0, result.error)
        findings = [str(item) for item in result.key_findings if str(item)]
        if not findings:
            findings = _derive_findings(result.data)
        artifact_ref = result.artifact_ref or _derive_artifact_ref(result)
        compact, truncated = _compact_value(result.data or {}, max_chars=max(400, max_chars - 600))
        return cls(status=status, summary=result.summary or result.error or "(无摘要)",
                   key_findings=tuple(findings[:8]), errors=tuple(errors[:6]), data=compact,
                   artifact_ref=artifact_ref, truncated=bool(result.truncated or truncated),
                   hints=tuple(str(item) for item in (result.next_hints or [])[:4]),
                   metadata=dict(result.metadata or {}))

    def render(self) -> str:
        import json

        parts = [f"[{self.status}] {self.summary}"]
        if self.key_findings:
            parts.append("关键结果: " + "；".join(self.key_findings))
        if self.errors:
            parts.append("错误: " + "；".join(self.errors))
        if self.hints:
            label = "合规提示" if self.status == "被护栏拒绝" else "建议"
            parts.append(label + ": " + "；".join(self.hints))
        if self.data not in (None, {}, [], ""):
            parts.append(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")))
        if self.artifact_ref:
            parts.append(f"原始证据: {self.artifact_ref}")
        if self.truncated:
            parts.append("(模型视图已压缩；可按原始证据引用读取完整结果)" if self.artifact_ref
                         else "(模型视图已压缩；当前结果未提供原始证据引用)")
        return "\n".join(parts)


def _derive_artifact_ref(result: ToolResult) -> str:
    """从现有 evidence/data 里提取已存在的原始工件引用。"""
    keys = ("path", "raw_ref", "artifact", "artifact_ref", "evidence_ref", "transcript")
    for item in result.evidence or ():
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(result.data, dict):
        for key in keys:
            value = result.data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _derive_findings(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    preferred = ("status", "code", "http_status", "title", "url", "target", "count", "total",
                 "ports", "findings", "routes", "assets", "verified", "changed")
    findings: list[str] = []
    for key in preferred:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, (str, int, float, bool)):
            findings.append(f"{key}={value}")
        elif isinstance(value, (list, dict)):
            findings.append(f"{key}[{len(value)}]")
    return findings


def _compact_value(value: Any, *, max_chars: int, depth: int = 0) -> tuple[Any, bool]:
    """递归压缩 JSON 数据，保留计数和稳定的前几个条目。"""
    if depth > 4:
        return "…", True
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value, False
        return value[:max_chars] + "…", True
    if isinstance(value, (int, float, bool)) or value is None:
        return value, False
    if isinstance(value, list):
        limit = 12
        items = []
        truncated = len(value) > limit
        for item in value[:limit]:
            compact, cut = _compact_value(item, max_chars=max(120, max_chars // max(1, limit)), depth=depth + 1)
            items.append(compact)
            truncated = truncated or cut
        if truncated:
            return {"items": items, "total": len(value), "omitted": max(0, len(value) - limit)}, True
        return items, False
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= 24:
                truncated = True
                break
            child_limit = max(120, max_chars // 8)
            # 文件读取和终端工具把主要证据放在单个文本字段中。平均分配
            # 预算会把短源码截成约 250 字符，迫使模型用 sed/cat 重复读取。
            # 给稳定的正文键保留更大的连续窗口，其他字段继续按原策略收缩。
            if str(key).lower() in {"text", "stdout", "stderr", "content", "source"}:
                child_limit = max(child_limit, min(1800, max_chars - 160))
            compact, cut = _compact_value(item, max_chars=child_limit, depth=depth + 1)
            output[str(key)] = compact
            truncated = truncated or cut
        if len(value) > 24:
            output["_omitted_keys"] = len(value) - 24
            truncated = True
        return output, truncated
    return str(value), True


def denied_result(decision: Decision, *, tool: str = "") -> ToolResult:
    return ToolResult(
        ok=False,
        denied=True,
        category=decision.category,
        summary=f"动作被护栏拒绝（{decision.category}）：{decision.reason}",
        error=decision.reason,
        data={"guard": decision.to_dict()},
        next_hints=[h for h in [decision.hint] if h],
    )


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


class _LeaseProxy:
    """即便工具派生线程调用上下文服务，也沿用同一撤销租约。"""
    def __init__(self, value: Any, lease):
        self._value, self._lease = value, lease

    def __getattr__(self, name: str):
        self._lease.check()
        value = getattr(self._value, name)
        if not callable(value):
            return value
        from ..scope import execution_window
        if inspect.iscoroutinefunction(value):
            async def async_call(*args, **kwargs):
                with execution_window(self._lease):
                    return await value(*args, **kwargs)
            return async_call
        def call(*args, **kwargs):
            with execution_window(self._lease):
                return value(*args, **kwargs)
        return call


_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="trina-tool")


class ToolContext:
    """工具执行上下文：护栏、审计、状态、HTTP 客户端、工作区。"""

    def __init__(
        self,
        *,
        config: TrinaConfig,
        guard: ScopeGuard,
        audit: AuditLog,
        state: RunState,
        http: Any,
        workspace: Path,
        tools: "ToolRegistry | None" = None,
        deadline: float | None = None,
        logger: Callable[[str], None] | None = None,
        dry_run: bool = False,
        event_bus: Any = None,
    ):
        from .http import HttpClient

        self.config = config
        self.guard = guard
        self.audit = audit
        self.state = state
        self.http: HttpClient = http
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.tools = tools
        self.deadline = deadline
        self.dry_run = dry_run
        #: 本次调用被护栏放行的**实际**动作等级（由注册表在执行前写入）。
        #: 工具内部再发子请求时应使用它，而不是硬编码等级，否则会出现
        #: 「外层按 verify 放行、内层按 exploit 自拒」的自相矛盾。
        self.effective_level: str = "detect"
        self._logger = logger or (lambda msg: None)
        from ..events import EventBus
        self.events = event_bus or EventBus(self.workspace / "events.sqlite3", state.run_id, redact_keys=config.audit.redact_keys)
        self.state.artifacts["events_path"] = str(self.events.path)
        from .external import BackgroundJobs
        self.extra: dict[str, Any] = {"background_jobs": BackgroundJobs(), "task_deadline": deadline}
        from ..binary.sessions import BinarySessions
        self.extra["binary_sessions"] = BinarySessions(self)
        from ..browser.sessions import BrowserSessions
        self.extra["browser_sessions"] = BrowserSessions(self)
        from ..traffic import TrafficStore
        self.extra["traffic"] = TrafficStore(self.workspace)
        self.state.artifacts["traffic_path"] = str(self.extra["traffic"].path)
        from ..todo import TodoList
        self.extra["todo_list"] = TodoList()

    # -------------------------------------------------------------- 便捷方法
    def log(self, message: str) -> None:
        from ..scope import ensure_execution_active
        ensure_execution_active()
        if self.extra.get("_tool_call_id"):
            self.emit("tool.output", stream="log", delta=message + "\n")
        self._logger(message)

    def emit(self, kind: str, **payload: Any) -> Any:
        """调用链中的事件继承任务来源和租约，迟到工具不能写入事件流。"""
        gate = self.extra.get("_event_gate")
        if gate is not None and kind == "tool.output":
            with gate["lock"]:
                if not gate["open"]:
                    raise RuntimeError("工具事件流已结束")
                return self._emit_event(kind, **payload)
        return self._emit_event(kind, **payload)

    def _emit_event(self, kind: str, **payload: Any) -> Any:
        from ..scope import ensure_execution_active
        ensure_execution_active()
        owner = self.extra.get("_execution_owner")
        if owner is not None:
            owner.check()
        call_id = str(payload.pop("call_id", self.extra.get("_tool_call_id", "")))
        return self.events.publish(kind, run_id=self.state.run_id, source="worker",
                                   task_id=str(self.extra.get("intent_id", "")),
                                   worker_id=str(self.extra.get("worker_id", "")),
                                   call_id=call_id, **payload)

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def throttle(self, host: str = "", tokens: float = 1.0) -> None:
        wait = self.guard.throttle(host, tokens)
        if wait > 0:
            time.sleep(min(wait, 5.0))

    def save_artifact(self, name: str, content: str) -> Path:
        from ..scope import ensure_execution_active
        ensure_execution_active()
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:80]
        path = self.workspace / safe
        path.write_text(content, encoding="utf-8", errors="replace")
        return path

    def evidence(self, kind: str, **fields: Any) -> dict[str, Any]:
        """登记一条证据（同时进状态库与审计）。"""
        entry = {"kind": kind}
        entry.update(fields)
        return self.state.add_evidence(entry)

    def add_hint(self, message: str) -> None:
        self.state.note("hint", message)

    def param(self, name: str, default: Any = None) -> Any:
        return self.extra.get(name, default)


# ---------------------------------------------------------------------------
# 工具基类
# ---------------------------------------------------------------------------


class Tool(ABC):
    spec: ToolSpec

    @abstractmethod
    def run(self, ctx: ToolContext, **params: Any) -> ToolResult:
        ...

    # 可选钩子：工具可用性（例如依赖的外部二进制是否存在）
    def available(self) -> tuple[bool, str]:
        return True, ""

    # 可选钩子：按参数决定动作等级（例如「单凭据验证」与「字典爆破」不是同一等级）
    def required_level(self, params: dict[str, Any]) -> str:
        return self.spec.action_level

    # 可选钩子：因动作等级被拒时给出「更低等级的等价做法」，会回灌给模型
    def level_hint(self, params: dict[str, Any]) -> str:
        return ""


# ---------------------------------------------------------------------------
# 注册表与执行链
# ---------------------------------------------------------------------------


class ToolRegistry:
    """工具注册表：大模型的「双手」。执行链见 `execute()`。"""

    def __init__(self, tools: Iterable[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    # -------------------------------------------------------------- 管理
    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool) or not tool.spec.name or tool.spec.action_level not in ACTION_LEVELS:
            raise ValueError("工具需要合法的名称、动作等级和 Tool 契约")
        name = tool.spec.name
        if any(candidate in self._tools for candidate in (name, public_tool_name(name), internal_tool_name(name))):
            raise ValueError(f"工具名称重复: {tool.spec.name}")
        self._tools[name] = tool

    def resolve_name(self, name: str) -> str:
        """按当前注册表解析公开名称，内部继续使用持久化标识。"""
        return name if name in self._tools else internal_tool_name(name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(self.resolve_name(name))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in self.names()]

    def by_category(self) -> dict[str, list[ToolSpec]]:
        grouped: dict[str, list[ToolSpec]] = {}
        for spec in self.specs():
            grouped.setdefault(spec.category, []).append(spec)
        return grouped

    def prompt_block(
        self, categories: Iterable[str] | None = None, verbose: bool = False,
        *, include: Iterable[str] | None = None, exclude: Iterable[str] = (),
    ) -> str:
        """渲染工具清单给大模型（分组、紧凑）。"""
        grouped = self.by_category()
        included = {self.resolve_name(name) for name in include} if include is not None else None
        excluded = {self.resolve_name(name) for name in exclude}
        blocks: list[str] = []
        order = ["recon", "web", "browser", "vuln", "exploit", "utility"]
        keys = [k for k in order if k in grouped] + [k for k in grouped if k not in order]
        for category in keys:
            if categories and category not in categories:
                continue
            visible = [spec for spec in grouped[category]
                       if (included is None or spec.name in included) and spec.name not in excluded]
            if not visible:
                continue
            blocks.append(f"## {category}")
            blocks.extend(spec.render(verbose=verbose, include_examples=verbose) for spec in visible)
        return "\n".join(blocks)

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.specs():
            out.append(
                {
                    "name": public_tool_name(spec.name),
                    "category": spec.category,
                    "action_level": spec.action_level,
                    "summary": spec.summary,
                    "external_bin": spec.external_bin,
                    "params": {
                        p: {"type": ps.type, "required": ps.required, "default": ps.default, "enum": ps.enum}
                        for p, ps in spec.params.items()
                    },
                }
            )
        return out

    # -------------------------------------------------------------- 执行链
    def execute(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        ctx: ToolContext | None = None,
        *,
        timeout: float | None = None,
        call_id: str | None = None,
        request_emitted: bool = False,
    ) -> ToolResult:
        """所有结果分支都有 requested 和唯一 finished，未开始执行也可复盘。"""
        if ctx is None:
            raise ValueError("execute 需要 ToolContext（护栏与审计的载体）")
        name = self.resolve_name(name)
        import uuid
        invocation = copy.copy(ctx)
        invocation.extra = dict(ctx.extra)
        invocation.extra["_tool_call_id"] = call_id or uuid.uuid4().hex
        import threading
        gate = {"lock": threading.RLock(), "open": True}
        invocation.extra["_event_gate"] = gate
        if not request_emitted:
            invocation.emit("tool.requested", tool=name, params=params or {})
        started = time.monotonic()
        try:
            result = self._execute(name, params, invocation, timeout=timeout)
        except Exception as exc:
            with gate["lock"]:
                gate["open"] = False
                invocation.emit("tool.finished", tool=name, status="failed", ok=False,
                                error=f"{type(exc).__name__}: {exc}", duration=time.monotonic() - started)
            raise
        finally:
            ctx.extra.update({key: value for key, value in invocation.extra.items() if key not in {"_tool_call_id", "_event_gate"}})
            ctx.effective_level = invocation.effective_level
        terminal = {"failed", "invalid", "unavailable", "timed_out", "cancelled", "interrupted"}
        status = "completed" if result.ok else ("denied" if result.denied else
                                                 result.category if result.category in terminal else "failed")
        if not result.ok and ctx.extra.get("stop_event") is not None and ctx.extra["stop_event"].is_set():
            status = "cancelled"
        with gate["lock"]:
            gate["open"] = False
            invocation.emit("tool.finished", tool=name, status=status, raw=result.raw, **result.to_dict())
        if result.facts or result.evidence:
            invocation.emit("state.updated", tool=name, state=ctx.state.to_dict())
        return result

    def _execute(
        self, name: str, params: dict[str, Any] | None, ctx: ToolContext, *, timeout: float | None,
    ) -> ToolResult:
        if ctx is None:
            raise ValueError("execute 需要 ToolContext（护栏与审计的载体）")
        started = time.monotonic()
        params = dict(params or {})
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"未知工具 {name!r}",
                summary=f"未知工具 {name}",
                category="invalid",
                data={"available_tools": [public_tool_name(tool) for tool in self.names()]},
                next_hints=["请从工具清单里选择合法工具名"],
                duration=time.monotonic() - started,
            )
        allowlist = ctx.extra.get("tool_allowlist")
        if allowlist is not None and name not in {self.resolve_name(item) for item in allowlist}:
            reason = f"工具 {name} 未按本题能力快照绑定"
            ctx.audit.append("tool.not_bound", tool=name, decision="deny", reason=reason)
            return ToolResult(
                ok=False,
                denied=True,
                error=reason,
                summary=reason,
                category="invalid",
                next_hints=["使用当前工具 Schema 中的工具，或由 Commander 先绑定对应能力"],
                duration=time.monotonic() - started,
            )

        spec = tool.spec
        ctx.state.counters["tool_calls"] = ctx.state.counters.get("tool_calls", 0) + 1

        # 1) 参数校验
        validated, error = self._validate(spec, params)
        if error:
            ctx.audit.append("tool.invalid", tool=name, decision="deny", reason=error, params=params)
            ctx.state.counters["denied"] = ctx.state.counters.get("denied", 0) + 1
            return ToolResult(
                ok=False,
                error=error,
                summary=f"参数非法: {error}",
                category="invalid",
                data={"valid_params": {p: str(ps.type) for p, ps in spec.params.items()}},
                next_hints=["按参数表重试"],
                duration=time.monotonic() - started,
            )

        # 2) 可用性
        available, why = tool.available()
        if not available:
            ctx.audit.append("tool.unavailable", tool=name, decision="deny", reason=why)
            return ToolResult(ok=False, error=why, summary=f"工具不可用: {why}", category="unavailable", duration=time.monotonic() - started)

        ctx.emit("tool.validated", tool=name, params=validated)

        # 3) 护栏：动作等级 + 破坏性 + 目标范围
        target = str(validated.get("target") or validated.get("url") or validated.get("host") or "")
        try:
            required_level = tool.required_level(validated)
        except Exception:  # noqa: BLE001 - 钩子出错时退回静态等级，绝不放宽
            required_level = spec.action_level
        audit_level = required_level or spec.action_level
        level_decision = ctx.guard.check_action_level(audit_level, target=target, reason=f"工具 {name}")
        if level_decision.denied:
            hint = level_decision.hint
            try:
                extra_hint = tool.level_hint(validated)
            except Exception:  # noqa: BLE001 - 提示生成失败不应影响裁决
                extra_hint = ""
            if extra_hint:
                level_decision = Decision(
                    level_decision.allowed,
                    reason=level_decision.reason,
                    category=level_decision.category,
                    target=level_decision.target,
                    detail=level_decision.detail,
                    hint=f"{extra_hint}（{hint}）" if hint else extra_hint,
                )
            return self._record_denial(ctx, name, level_decision, started, params)
        if spec.destructive:
            decision = ctx.guard.check_destructive(target=target, reason=f"工具 {name} 属破坏性动作")
            if decision.denied:
                return self._record_denial(ctx, name, decision, started, params)
        if target:
            target_decision = ctx.guard.check_target(target, action=f"tool:{name}")
            if target_decision.denied:
                return self._record_denial(ctx, name, target_decision, started, params)

        # 4) 审计：放行
        ctx.effective_level = audit_level
        ctx.emit("tool.started", tool=name, params=validated, action_level=audit_level, target=target)
        ctx.audit.append(
            "tool.start",
            tool=name,
            decision="allow",
            category=spec.category,
            action_level=audit_level,
            declared_level=spec.action_level,
            target=target,
            params=validated,
        )

        # 5) 执行（带超时）
        budget = timeout if timeout is not None else ctx.config.run.tool_timeout
        if ctx.remaining() is not None:
            budget = min(budget, max(.001, ctx.remaining()))
        result = self._run_with_timeout(tool, validated, ctx, budget, started)

        # 6) 吸收事实 + 审计
        self._attach_raw_artifact(ctx, name, result)
        if result.facts:
            changed = ctx.state.absorb(result.facts)
            if changed:
                result.next_hints.extend(changed[:3])
        for item in result.evidence:
            ctx.state.add_evidence(dict(item, tool=name))
        ctx.audit.append(
            "tool.end",
            tool=name,
            decision="allow",
            target=target,
            ok=result.ok,
            error=(result.error or "")[:300],
            summary=result.summary[:300],
            command=(result.command or "")[:400],
            duration=round(result.duration, 3),
            output_digest=sha256_text(result.raw[:20000]) if result.raw else "",
            facts={k: (len(v) if isinstance(v, list) else v) for k, v in (result.facts or {}).items()},
            evidence_count=len(result.evidence),
        )
        ctx.state.log_step(
            {
                "kind": "tool",
                "tool": name,
                "params": {k: v for k, v in validated.items() if k not in ("password_list", "payloads")},
                "ok": result.ok,
                "summary": result.summary[:300],
                "duration": round(result.duration, 3),
            }
        )
        return result

    @staticmethod
    def _attach_raw_artifact(ctx: ToolContext, name: str, result: ToolResult) -> None:
        """长原文只保存在本地工件，模型只收到摘要和引用。"""
        if not result.raw or len(result.raw) <= 2600 or result.artifact_ref:
            return
        import hashlib
        import uuid

        call_id = str(ctx.extra.get("_tool_call_id") or uuid.uuid4().hex)
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:40]
        path = ctx.save_artifact(f"tool-{safe_name}-{call_id[:12]}.raw.txt", result.raw)
        digest = hashlib.sha256(result.raw.encode("utf-8", errors="replace")).hexdigest()
        result.artifact_ref = str(path)
        result.truncated = True
        result.metadata = {**result.metadata, "raw_bytes": len(result.raw.encode("utf-8", errors="replace")),
                           "raw_sha256": digest}
        result.evidence.append({"kind": "tool_raw", "path": str(path), "sha256": digest,
                                "bytes": result.metadata["raw_bytes"]})

    # -------------------------------------------------------------- 内部
    @staticmethod
    def _record_denial(ctx: ToolContext, name: str, decision: Decision, started: float, params: dict[str, Any]) -> ToolResult:
        ctx.state.counters["denied"] = ctx.state.counters.get("denied", 0) + 1
        ctx.state.note("denied", f"{name} 被拒绝: {decision.reason}", category=decision.category)
        ctx.audit.append("tool.denied", tool=name, decision="deny", category=decision.category, reason=decision.reason, params=params)
        result = denied_result(decision, tool=name)
        result.duration = time.monotonic() - started
        return result

    @staticmethod
    def _validate(spec: ToolSpec, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        unknown = [k for k in params if k not in spec.params]
        if unknown:
            return {}, f"存在未知参数 {unknown}；合法参数: {sorted(spec.params)}"
        validated: dict[str, Any] = {}
        for pname, pspec in spec.params.items():
            value = params.get(pname, None)
            if value is None or (value == "" and pspec.type != "str"):
                if pspec.required and pspec.default is None:
                    return {}, f"缺少必填参数 {pname}"
                value = pspec.default
                if value is None:
                    continue
            coerced, err = _coerce_param(pname, pspec, value)
            if err:
                return {}, err
            validated[pname] = coerced
        return validated, ""

    @staticmethod
    def _run_with_timeout(tool: Tool, params: dict[str, Any], ctx: ToolContext, timeout: float, started: float) -> ToolResult:
        signature = inspect.signature(tool.run)
        # 兼容两种写法：显式具名参数，或 `run(self, ctx, **params)`
        accepts_var_keyword = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )
        if accepts_var_keyword:
            kwargs = dict(params)
        else:
            kwargs = {k: v for k, v in params.items() if k in signature.parameters}
        from ..scope import ExecutionLease, execution_window
        lease = ExecutionLease(time.monotonic() + timeout)
        working = copy.copy(ctx)
        baseline = copy.deepcopy(ctx.state)
        working.state = copy.deepcopy(baseline)
        working.extra = dict(ctx.extra)
        working.extra["_execution_owner"] = lease
        working.deadline = min(ctx.deadline, lease.deadline) if ctx.deadline else lease.deadline
        working.guard = _LeaseProxy(ctx.guard, lease)
        working.audit = _LeaseProxy(ctx.audit, lease)
        working.http = _LeaseProxy(ctx.http, lease)
        def run():
            from ..traffic import traffic_window
            with execution_window(lease), traffic_window(ctx.extra["traffic"], str(ctx.extra.get("_tool_call_id", ""))):
                return tool.run(working, **kwargs)
        future = _TOOL_EXECUTOR.submit(run)
        try:
            result = future.result(timeout=timeout)
        except FutureTimeout:
            lease.cancelled.set()
            future.cancel()
            ctx.extra["background_jobs"].stop_owner(lease)
            ctx.extra["binary_sessions"].cancel_owner(lease)
            ctx.extra["browser_sessions"].cancel_owner(lease)
            ctx.state.counters["errors"] = ctx.state.counters.get("errors", 0) + 1
            return ToolResult(
                ok=False,
                error=f"工具 {tool.spec.name} 超时（{timeout:.0f}s）",
                summary=f"{tool.spec.name} 超时",
                category="timed_out",
                next_hints=[
                    "不要原样重试；降低扫描范围或改用更快的工具",
                    f"先检查工作目录 {ctx.workspace} 和任务给出的具体路径",
                ],
                duration=time.monotonic() - started,
            )
        except ScopeViolation as exc:
            ctx.extra["background_jobs"].stop_owner(lease)
            ctx.extra["binary_sessions"].cancel_owner(lease)
            ctx.extra["browser_sessions"].cancel_owner(lease)
            return denied_result(exc.decision, tool=tool.spec.name)
        except Exception as exc:  # noqa: BLE001 - 任何工具异常都不能中断整条链路
            ctx.extra["background_jobs"].stop_owner(lease)
            ctx.extra["binary_sessions"].cancel_owner(lease)
            ctx.extra["browser_sessions"].cancel_owner(lease)
            ctx.state.counters["errors"] = ctx.state.counters.get("errors", 0) + 1
            detail = traceback.format_exc(limit=3)
            ctx.audit.append("tool.error", tool=tool.spec.name, decision="error", error=f"{type(exc).__name__}: {exc}"[:400])
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                summary=f"{tool.spec.name} 执行异常",
                raw=detail,
                duration=time.monotonic() - started,
            )
        finally:
            lease.cancelled.set()
        if not isinstance(result, ToolResult):
            ctx.extra["background_jobs"].stop_owner(lease)
            ctx.extra["binary_sessions"].cancel_owner(lease)
            ctx.extra["browser_sessions"].cancel_owner(lease)
            return ToolResult(ok=False, error=f"工具返回值类型错误: {type(result)!r}", summary="工具契约违反")
        if not result.ok:
            ctx.extra["background_jobs"].stop_owner(lease)
        for key, value in working.state.__dict__.items():
            if value != baseline.__dict__.get(key):
                setattr(ctx.state, key, value)
        working.extra.pop("_execution_owner", None)
        ctx.extra.update(working.extra)
        result.duration = result.duration or (time.monotonic() - started)
        return result


def _coerce_param(name: str, spec: ParamSpec, value: Any) -> tuple[Any, str]:
    try:
        if spec.type == "int":
            value = int(value)
        elif spec.type == "float":
            value = float(value)
        elif spec.type == "bool":
            value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
        elif spec.type == "list":
            if isinstance(value, str):
                value = [item.strip() for item in value.split(",") if item.strip()]
            elif not isinstance(value, (list, tuple)):
                return None, f"参数 {name} 需要 list"
            value = list(value)
        elif spec.type == "dict":
            if not isinstance(value, dict):
                return None, f"参数 {name} 需要 dict"
        elif spec.type == "str":
            if isinstance(value, (dict, list)):
                import json

                value = json.dumps(value, ensure_ascii=False)
            else:
                value = str(value)
        else:
            return None, f"参数 {name} 的类型 {spec.type!r} 未实现"
    except (TypeError, ValueError) as exc:
        return None, f"参数 {name} 无法转换为 {spec.type}: {exc}"
    if spec.enum and value not in spec.enum:
        return None, f"参数 {name} 取值必须是 {spec.enum} 之一"
    return value, ""


__all__ = [
    "ParamSpec",
    "ToolSpec",
    "ToolResult",
    "ToolContext",
    "Tool",
    "ToolRegistry",
    "denied_result",
]
