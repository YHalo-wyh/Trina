"""工具层核心契约：ToolSpec / ToolResult / ToolContext / ToolRegistry。

**单一收口**：所有工具调用都必须经过 `ToolRegistry.execute()`，它负责
参数校验 → 动作等级与范围护栏 → 审计留痕 → 超时与异常兜底 → 结果归一。

新增工具的写法（放在 `trina/tools/` 下并在 `trina/tools/__init__.py` 导入）::

    from .registry import ParamSpec, Tool, ToolResult, ToolSpec, register

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

    register(MyTool())
"""

from __future__ import annotations

import inspect
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

    def render(self, verbose: bool = False) -> str:
        head = f"### {self.name} [{self.category}/{self.action_level}]"
        lines = [head, self.summary]
        if self.params:
            lines.append("参数:")
            for pname, pspec in self.params.items():
                lines.append(f"  - {pname}: {pspec.render()}")
        if self.examples:
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

    def to_observation(self, max_chars: int = 2600) -> str:
        """转成大模型看的观察文本。"""
        parts: list[str] = []
        status = "OK" if self.ok else ("被护栏拒绝" if self.denied else "失败")
        parts.append(f"[{status}] {self.summary or self.error or '(无摘要)'}")
        if self.denied and self.next_hints:
            parts.append("合规提示: " + "；".join(self.next_hints))
        payload = self.data or {}
        if payload:
            import json

            rendered = json.dumps(payload, ensure_ascii=False, indent=1)
            if len(rendered) > max_chars:
                rendered = rendered[:max_chars] + "\n…(截断)"
            parts.append(rendered)
        if self.error and self.ok:
            parts.append(f"错误: {self.error}")
        if self.next_hints and not self.denied:
            parts.append("建议: " + "；".join(self.next_hints[:3]))
        return "\n".join(parts)

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
        }


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
        self.extra: dict[str, Any] = {}

    # -------------------------------------------------------------- 便捷方法
    def log(self, message: str) -> None:
        self._logger(message)

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def throttle(self, host: str = "", tokens: float = 1.0) -> None:
        wait = self.guard.throttle(host, tokens)
        if wait > 0:
            time.sleep(min(wait, 5.0))

    def save_artifact(self, name: str, content: str) -> Path:
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


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    """注册到全局表（模块导入时调用）。"""
    if not isinstance(tool, Tool):
        raise TypeError(f"必须注册 Tool 实例，得到 {type(tool)!r}")
    if not tool.spec.name:
        raise ValueError("工具必须有名字")
    if tool.spec.action_level not in ACTION_LEVELS:
        raise ValueError(f"{tool.spec.name}: 非法动作等级 {tool.spec.action_level!r}")
    _REGISTRY[tool.spec.name] = tool
    return tool


def registry() -> dict[str, Tool]:
    return _REGISTRY


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
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in self.names()]

    def by_category(self) -> dict[str, list[ToolSpec]]:
        grouped: dict[str, list[ToolSpec]] = {}
        for spec in self.specs():
            grouped.setdefault(spec.category, []).append(spec)
        return grouped

    def prompt_block(self, categories: Iterable[str] | None = None, verbose: bool = False) -> str:
        """渲染工具清单给大模型（分组、紧凑）。"""
        grouped = self.by_category()
        blocks: list[str] = []
        order = ["recon", "web", "vuln", "exploit", "utility"]
        keys = [k for k in order if k in grouped] + [k for k in grouped if k not in order]
        for category in keys:
            if categories and category not in categories:
                continue
            blocks.append(f"## {category}")
            blocks.extend(spec.render(verbose=verbose) for spec in grouped[category])
        return "\n".join(blocks)

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.specs():
            out.append(
                {
                    "name": spec.name,
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
                data={"available_tools": self.names()},
                next_hints=["请从工具清单里选择合法工具名"],
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
                data={"valid_params": {p: str(ps.type) for p, ps in spec.params.items()}},
                next_hints=["按参数表重试"],
                duration=time.monotonic() - started,
            )

        # 2) 可用性
        available, why = tool.available()
        if not available:
            ctx.audit.append("tool.unavailable", tool=name, decision="deny", reason=why)
            return ToolResult(ok=False, error=why, summary=f"工具不可用: {why}", duration=time.monotonic() - started)

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
            if not ctx.config.scope.destructive:
                decision = ctx.guard.check_destructive(target=target, reason=f"工具 {name} 属破坏性动作")
                return self._record_denial(ctx, name, decision, started, params)
        if target:
            target_decision = ctx.guard.check_target(target, action=f"tool:{name}")
            if target_decision.denied:
                return self._record_denial(ctx, name, target_decision, started, params)

        # 4) 审计：放行
        ctx.effective_level = audit_level
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
            budget = min(budget, max(5.0, ctx.remaining()))
        result = self._run_with_timeout(tool, validated, ctx, budget, started)

        # 6) 吸收事实 + 审计
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
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"trina-{tool.spec.name}")
        future = executor.submit(tool.run, ctx, **kwargs)
        try:
            result = future.result(timeout=timeout)
        except FutureTimeout:
            ctx.state.counters["errors"] = ctx.state.counters.get("errors", 0) + 1
            return ToolResult(
                ok=False,
                error=f"工具 {tool.spec.name} 超时（{timeout:.0f}s）",
                summary=f"{tool.spec.name} 超时",
                next_hints=["降低扫描范围或改用更快的工具"],
                duration=time.monotonic() - started,
            )
        except ScopeViolation as exc:
            return denied_result(exc.decision, tool=tool.spec.name)
        except Exception as exc:  # noqa: BLE001 - 任何工具异常都不能中断整条链路
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
            executor.shutdown(wait=False)
        if not isinstance(result, ToolResult):
            return ToolResult(ok=False, error=f"工具返回值类型错误: {type(result)!r}", summary="工具契约违反")
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
    "register",
    "registry",
    "denied_result",
]
