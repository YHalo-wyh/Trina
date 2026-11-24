"""工具目录的只读投影：注册、路由、可用性与会话统计。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .tool_names import public_tool_name
from .tools.profile import (
    CTF_BINARY_COORDINATION_TOOLS,
    CTF_BINARY_WEB_TOOLS,
    CTF_BROWSER_TOOLS,
    CTF_PRUNED_TOOLS,
)
from .tools.registry import ToolRegistry, ToolSpec


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 3)


@dataclass(frozen=True)
class ToolStats:
    """当前会话中一个工具的轻量调用统计。"""

    calls: int = 0
    failed: int = 0
    p50_seconds: float | None = None
    p95_seconds: float | None = None


@dataclass(frozen=True)
class ToolCatalogEntry:
    """一个注册工具的最终目录行，不拥有也不修改真实工具。"""

    internal_name: str
    name: str
    spec: ToolSpec
    source: str
    source_kind: str
    visible: bool
    available: bool
    availability_reason: str
    visibility_reason: str
    resolution: tuple[str, ...]
    route: str
    role: str
    stats: ToolStats = field(default_factory=ToolStats)

    @property
    def status(self) -> str:
        if not self.available:
            return "unavailable"
        return "visible" if self.visible else "hidden"

    @property
    def search_text(self) -> str:
        params = " ".join(
            f"{name} {item.type} {item.description}" for name, item in self.spec.params.items()
        )
        return " ".join((
            self.name, self.internal_name, self.spec.summary, self.spec.category,
            self.spec.action_level, self.source, self.source_kind, self.visibility_reason,
            self.route, self.role, params,
        )).casefold()


def _session_stats(state: Any | None) -> dict[str, ToolStats]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in getattr(state, "steps", ()) or ():
        if not isinstance(row, dict) or row.get("kind") != "tool" or not row.get("tool"):
            continue
        grouped.setdefault(str(row["tool"]), []).append(row)
    result: dict[str, ToolStats] = {}
    for name, rows in grouped.items():
        durations = []
        for row in rows:
            try:
                durations.append(max(0.0, float(row.get("duration", 0.0))))
            except (TypeError, ValueError):
                pass
        result[name] = ToolStats(
            calls=len(rows),
            failed=sum(not bool(row.get("ok")) for row in rows),
            p50_seconds=_percentile(durations, .50),
            p95_seconds=_percentile(durations, .95),
        )
    return result


def _source(tool: Any) -> tuple[str, str]:
    module = str(type(tool).__module__ or "")
    short = module.removeprefix("trina.") or module or "unknown"
    if module.startswith("trina.capabilities"):
        return short, "Capability"
    if module.startswith("trina.tools"):
        return short, "System"
    return short, "Custom"


def _hidden_reason(name: str, route: str, *, browser: bool, terminal_session: bool,
                   subworkers: bool) -> str:
    if name == "worker_tasks" and not subworkers:
        return "当前会话未连接 Subworker 调度器"
    if name in {"terminal_start", "terminal_status", "terminal_stop"} and not terminal_session:
        return "后台终端按需加载；当前请求没有声明后台作业"
    if name in CTF_BROWSER_TOOLS and not browser:
        return "浏览器工具按需加载；当前请求没有声明网页交互"
    if name.startswith(("binary_", "debug_")) and route != "binary":
        return "原生分析工具只在 Binary/Pwn/Reverse 路由绑定"
    if route == "binary" and name in CTF_BINARY_WEB_TOOLS:
        return "Binary 路由隐藏 Web/浏览器分支，减少无关 Schema"
    if route == "binary" and name in CTF_BINARY_COORDINATION_TOOLS:
        return "Binary 单目标路由未绑定跨任务协作能力"
    if name in CTF_PRUNED_TOOLS:
        return "深度或企业工具按需绑定，不进入默认 CTF 工具面"
    return f"不在当前 {route} 路由的模型工具白名单"


def build_tool_catalog(
    registry: ToolRegistry,
    *,
    visible: Iterable[str] | None = None,
    route: str = "general",
    role: str = "commander",
    browser: bool = False,
    terminal_session: bool = False,
    subworkers: bool = False,
    state: Any | None = None,
    check_availability: bool = True,
) -> list[ToolCatalogEntry]:
    """把真实注册表投影为可解释目录；不改变工具定义或绑定。"""
    allowed = ({registry.resolve_name(name) for name in visible}
               if visible is not None else set(registry.names()))
    stats = _session_stats(state)
    entries: list[ToolCatalogEntry] = []
    availability_cache: dict[tuple[Any, ...], tuple[bool, str]] = {}
    for internal_name in registry.names():
        tool = registry.get(internal_name)
        if tool is None:
            continue
        is_visible = internal_name in allowed
        if check_availability:
            try:
                method = type(tool).available
                code = getattr(method, "__code__", None)
                key = (str(getattr(method, "__module__", "")),
                       bytes(code.co_code) if code is not None else id(method),
                       tuple(code.co_names) if code is not None else (),
                       repr(code.co_consts) if code is not None else "")
                if key not in availability_cache:
                    availability_cache[key] = tool.available()
                available, why = availability_cache[key]
            except Exception as exc:  # 目录不能因第三方可用性探针失败而崩溃。
                available, why = False, f"{type(exc).__name__}: {exc}"
        else:
            available, why = True, ""
        source, source_kind = _source(tool)
        visibility_reason = (
            f"已由 {route} 路由绑定到 {role}，进入当前模型 Schema"
            if is_visible else _hidden_reason(
                internal_name, route, browser=browser,
                terminal_session=terminal_session, subworkers=subworkers,
            )
        )
        resolution = ["ToolRegistry 已注册", f"任务路由 {route}"]
        if role:
            resolution.append(f"Agent {role}")
        resolution.append("当前会话白名单命中" if is_visible else "当前会话白名单未命中")
        resolution.append("模型 Schema 已注入" if is_visible else "未注入模型 Schema")
        entries.append(ToolCatalogEntry(
            internal_name=internal_name,
            name=public_tool_name(internal_name),
            spec=tool.spec,
            source=source,
            source_kind=source_kind,
            visible=is_visible,
            available=bool(available),
            availability_reason=str(why or ""),
            visibility_reason=visibility_reason,
            resolution=tuple(resolution),
            route=route,
            role=role,
            stats=stats.get(internal_name, ToolStats()),
        ))
    return entries


def filter_tool_catalog(entries: Iterable[ToolCatalogEntry], *, query: str = "",
                        group: str = "all") -> list[ToolCatalogEntry]:
    """选择器和测试共用的确定性过滤、排序。"""
    terms = query.casefold().split()
    result = []
    for item in entries:
        if group == "visible" and not item.visible:
            continue
        if group == "hidden" and item.visible:
            continue
        if group == "unavailable" and item.available:
            continue
        if group.startswith("category:") and item.spec.category != group.partition(":")[2]:
            continue
        if group.startswith("source:") and item.source_kind.casefold() != group.partition(":")[2].casefold():
            continue
        if all(term in item.search_text for term in terms):
            result.append(item)
    needle = query.casefold().strip()
    def rank(item: ToolCatalogEntry) -> tuple[Any, ...]:
        names = (item.name.casefold(), item.internal_name.casefold())
        match = (0 if needle and needle in names else
                 1 if needle and any(name.startswith(needle) for name in names) else
                 2 if needle and all(term in " ".join(names) for term in terms) else 3)
        return match, not item.visible, not item.available, item.name
    return sorted(result, key=rank)


__all__ = [
    "ToolCatalogEntry", "ToolStats", "build_tool_catalog", "filter_tool_catalog",
]
