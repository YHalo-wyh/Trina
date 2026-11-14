"""控制台的被动观测状态：跨模型累计会话用量，区分模型等待和工具执行。"""
from __future__ import annotations

import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .llm.base import LLMBackend, LLMMessage, LLMResponse, LLMUsage, StreamCallback


def _number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _cells(value: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def _fit_cells(value: str, width: int, *, tail: bool = False) -> str:
    """按终端显示宽度省略文本；路径保留末尾目录。"""
    from .terminal import clean_text
    value = clean_text(value).replace("\n", " ").strip()
    if width <= 0:
        return ""
    if _cells(value) <= width:
        return value
    selected, used = [], 0
    for char in reversed(value) if tail else value:
        size = _cells(char)
        if used + size > width - 1:
            break
        selected.append(char)
        used += size
    return "…" + "".join(reversed(selected)) if tail else "".join(selected) + "…"


def _join_segments(groups: list[list[tuple[str, str]]], separator: str = "dot") -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for group in groups:
        if not group:
            continue
        if result:
            result.append(("class:context.label", {"pipe": " │ ", "slash": " / "}.get(separator, " · ")))
        result.extend(group)
    return result


@dataclass(frozen=True)
class ConsoleContext:
    """底栏只读上下文：当前模型、任务、进度与窗口占用。"""

    status: str
    model: str
    directory: str
    task: str
    progress: str = ""
    context_tokens: int | None = None
    context_window: int = 0
    role: str = "Commander"
    branch: str = ""
    preset: str = "default"
    separator: str = "dot"
    context_bar: bool = True

    def footer(self, width: int) -> list[tuple[str, str]]:
        """单行按优先级显示角色、模型、状态和进度，不因长模型换行。"""
        width = max(1, width)
        icon = "◌"
        if "运行" in self.status:
            icon = "●"
        elif "暂停" in self.status:
            icon = "Ⅱ"
        elif "审批" in self.status:
            icon = "?"
        elif "停止" in self.status:
            icon = "■"
        role = _fit_cells(self.role, min(18, max(4, width // 3)))
        groups = [[("class:context.role", f" {role} ")]]
        remaining = width - _cells(role) - 2
        if remaining >= 8:
            model_budget = remaining - 3
            extras = []
            if width >= 64 and self.preset != "compact":
                extras.append([("class:context.state", icon + " "), ("class:status", self.status)])
            if width >= 100 and self.progress and self.preset != "compact":
                extras.append([("class:context.progress", self.progress)])
            model_budget -= sum(sum(_cells(text) for _, text in group) + 3 for group in extras)
            groups.append([("class:context.model", _fit_cells("◈ " + self.model, max(4, model_budget)))])
            groups.extend(extras)
        result = _join_segments(groups, self.separator)
        if sum(_cells(text) for _, text in result) > width:
            return [("class:context.role", _fit_cells(f" {self.role} ", width))]
        return result

    def rail(self, width: int) -> list[tuple[str, str]]:
        """上下文仪表贴最左，工作目录锚定到最右，中间用横线填满。"""
        width = max(1, width)
        path_prefix = " 📁 " if width >= 16 else " "
        path_limit = width if width < 28 else max(12, min(width // 2, 54))
        path_text = path_prefix + _fit_cells(self.directory, path_limit - _cells(path_prefix), tail=True)
        context_text = ""
        if width >= 28 and self.context_window > 0:
            if self.context_tokens is None:
                context_text = f"◔ {_number(self.context_window)} "
            else:
                ratio = max(0.0, self.context_tokens / self.context_window)
                percent = min(999, round(ratio * 100))
                if width < 64 or not self.context_bar or self.preset == "compact":
                    context_text = f"◔ {percent}% "
                else:
                    filled = min(8, max(0, round(min(1.0, ratio) * 8)))
                    gauge = "▰" * filled + "▱" * (8 - filled)
                    context_text = f"◔ {percent}% {gauge} "
        branch = ""
        if self.branch and width >= 80:
            branch = " ⎇ " + _fit_cells(self.branch, min(20, width // 6)) + " "
        remaining = max(0, width - _cells(path_text) - _cells(context_text) - _cells(branch))
        fragments: list[tuple[str, str]] = []
        if context_text:
            ratio = (self.context_tokens or 0) / max(1, self.context_window)
            tone = "context.hot" if ratio >= .85 else "context.warn" if ratio >= .65 else "context.meter"
            fragments.append(("class:" + tone, context_text))
        if branch:
            fragments.append(("class:context.branch", branch))
        fragments.append(("class:rail", "─" * remaining))
        fragments.append(("class:context.path", path_text))
        return fragments


@dataclass
class ConsoleState:
    """只记录实际返回的用量；不估算 Token、缓存或费用。"""

    usage: LLMUsage = field(default_factory=lambda: LLMUsage(calls=0))
    phase: str = "idle"
    label: str = "就绪"
    since: float = field(default_factory=time.monotonic)
    pending: int = 0
    missing_tokens: int = 0
    missing_input: int = 0
    missing_output: int = 0
    missing_cache: int = 0
    missing_cache_read: int = 0
    missing_cache_write: int = 0
    missing_cost: int = 0
    context_tokens: int | None = None
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def set_activity(self, phase: str, label: str) -> None:
        with self._lock:
            self.phase, self.label, self.since = phase, label, time.monotonic()

    def model_started(self) -> None:
        with self._lock:
            self.pending += 1
            self.set_activity("waiting", "请求模型")

    def model_event(self, event: Any) -> None:
        """仅在实际流事件到达后改变标签，计时继续覆盖完整请求。"""
        labels = {"start": ("waiting", "等待输出"), "thinking_delta": ("thinking", "思考中"),
                  "text_delta": ("responding", "正在回复"), "error": ("error", "模型请求失败")}
        with self._lock:
            if event.type in labels:
                self.phase, self.label = labels[event.type]

    def model_finished(self, usage: LLMUsage | None) -> None:
        with self._lock:
            usage = usage or LLMUsage()
            if usage.input_reported:
                # 最近一次完整请求的输入量近似当前窗口占用；累计 Token 另行统计。
                self.context_tokens = usage.input_tokens
            self.usage = self.usage.merge(usage)
            self.missing_tokens += int(not usage.tokens_reported)
            self.missing_input += int(not usage.input_reported)
            self.missing_output += int(not usage.output_reported)
            self.missing_cache += int(not usage.cache_reported)
            self.missing_cache_read += int(not usage.cache_read_reported)
            self.missing_cache_write += int(not usage.cache_write_reported)
            self.missing_cost += int(not usage.cost_reported)
            self.pending = max(0, self.pending - 1)
            if not self.pending:
                self.set_activity("processing", "整理结果")

    def activity(self) -> tuple[str, str, float]:
        with self._lock:
            return self.phase, self.label, max(0.0, time.monotonic() - self.since)

    def reset(self) -> None:
        with self._lock:
            self.usage = LLMUsage(calls=0)
            self.missing_tokens = self.missing_input = self.missing_output = 0
            self.missing_cache = self.missing_cache_read = self.missing_cache_write = 0
            self.missing_cost = self.pending = 0
            self.context_tokens = None
            self.set_activity("idle", "就绪")

    def usage_data(self) -> dict[str, Any]:
        with self._lock:
            return {**self.usage.to_dict(), "missing_tokens": self.missing_tokens,
                    "missing_input": self.missing_input, "missing_output": self.missing_output,
                    "missing_cache": self.missing_cache, "missing_cache_read": self.missing_cache_read,
                    "missing_cache_write": self.missing_cache_write, "missing_cost": self.missing_cost}

    def footer(self, width: int, compact: bool = False, *, preset: str = "default", separator: str = "dot") -> str:
        data = self.usage_data()
        if not data["calls"]:
            return ""
        known = data["tokens_reported"]
        prefix = "≥" if (data["missing_tokens"] or data["missing_input"] or data["missing_output"]) and known else ""
        groups: list[str] = []
        if known:
            groups.append(f"会话 Token {prefix}{_number(data['total_tokens'])}")
        rate = data["cache_hit_rate"]
        if rate is not None:
            cache = f"{rate:.1%}" + ("（部分）" if data["missing_cache"] else "")
            groups.append(f"缓存命中 {cache}")
        if not compact and preset != "compact":
            io_parts: list[str] = []
            if data["input_reported"]:
                io_parts.append(f"入 {('≥' if data['missing_input'] else '')}{_number(data['input_tokens'])}")
            if data["output_reported"]:
                io_parts.append(f"出 {('≥' if data['missing_output'] else '')}{_number(data['output_tokens'])}")
            if io_parts:
                groups.insert(1 if groups else 0, " / ".join(io_parts))
            cache_parts: list[str] = []
            if data["cache_read_reported"]:
                cache_parts.append(f"读 {('≥' if data['missing_cache_read'] else '')}{_number(data['cache_read_tokens'])}")
            if data["cache_write_reported"]:
                cache_parts.append(f"写 {('≥' if data['missing_cache_write'] else '')}{_number(data['cache_write_tokens'])}")
            if cache_parts:
                groups.append("缓存" + " / ".join(cache_parts))
            if data["cost_reported"]:
                groups.append(f"费用 {'≥' if data['missing_cost'] else ''}${data['cost_usd']:.4f}")
        if not groups:
            return ""
        # 始终一行：先保留总量与命中率，宽屏再展示实际 I/O、缓存和费用。
        sep = {"pipe": " │ ", "slash": " / "}.get(separator, " · ")
        priority = sorted(groups, key=lambda item: 0 if item.startswith("会话") else 1 if item.startswith("缓存命中") else 2 if item.startswith("费用") else 3)
        if width < 44:
            priority.sort(key=lambda item: 0 if item.startswith("缓存命中") else 1)
        chosen: list[str] = []
        for group in priority:
            if _cells(sep.join(chosen + [group])) <= width:
                chosen.append(group)
            if preset == "default" and len(chosen) >= 4:
                break
        if not chosen:
            # 极窄屏先显示缓存命中，若没有缓存数据则显示已报告总量。
            chosen = [_fit_cells(next((item for item in priority if item.startswith("缓存命中")), priority[0]), width)]
        return sep.join(chosen)


class TrackedBackend:
    """原样委托模型后端，旁路观测调用区间与返回用量，不重复计费。"""

    def __init__(self, backend: Any, view: ConsoleState) -> None:
        self.backend, self.view = backend, view

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.view.model_started()
        response = None
        try:
            response = self.backend.complete(*args, **kwargs)
            return response
        finally:
            self.view.model_finished(getattr(response, "usage", None))

    def complete_messages(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_event: StreamCallback | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """保持结构化消息不变地委托，并只在终止响应处累计一次用量。"""
        self.view.model_started()
        response = None
        def observe(event: Any) -> None:
            self.view.model_event(event)
            if on_event is not None:
                on_event(event)
        try:
            method = getattr(self.backend, "complete_messages", None)
            if method is not None:
                options = {"temperature": temperature, "max_tokens": max_tokens, "on_event": observe}
                if tools is not None:
                    options["tools"] = tools
                response = method(messages, **options)
            else:
                # 仅供旧测试替身；真实后端都实现 complete_messages。
                system = next((item["content"] for item in messages if item["role"] == "system"), "")
                user = next((item["content"] for item in reversed(messages) if item["role"] == "user"), "")
                options = {}
                if temperature is not None:
                    options["temperature"] = temperature
                if max_tokens is not None:
                    options["max_tokens"] = max_tokens
                response = self.backend.complete(system, user, **options)
            return response
        finally:
            self.view.model_finished(getattr(response, "usage", None))

    def health(self) -> tuple[bool, str]:
        return LLMBackend.health(self)
