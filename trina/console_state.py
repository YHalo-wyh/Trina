"""控制台的被动观测状态：跨模型累计会话用量，区分模型等待和工具执行。"""
from __future__ import annotations

import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .llm.base import LLMBackend, LLMResponse, LLMUsage


def _number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _cells(value: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


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
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def set_activity(self, phase: str, label: str) -> None:
        with self._lock:
            self.phase, self.label, self.since = phase, label, time.monotonic()

    def model_started(self) -> None:
        with self._lock:
            self.pending += 1
            self.set_activity("thinking", "思考中")

    def model_finished(self, usage: LLMUsage | None) -> None:
        with self._lock:
            usage = usage or LLMUsage()
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
            self.set_activity("idle", "就绪")

    def usage_data(self) -> dict[str, Any]:
        with self._lock:
            return {**self.usage.to_dict(), "missing_tokens": self.missing_tokens,
                    "missing_input": self.missing_input, "missing_output": self.missing_output,
                    "missing_cache": self.missing_cache, "missing_cache_read": self.missing_cache_read,
                    "missing_cache_write": self.missing_cache_write, "missing_cost": self.missing_cost}

    def footer(self, width: int, compact: bool = False) -> str:
        data = self.usage_data()
        if not data["calls"]:
            # 一次模型都没调用：没有"未提供"可言，直接说明状态，避免 0 与未提供并存的歧义。
            from .terminal import _wrap
            return _wrap("会话 Token 0 · 尚未调用模型", max(20, width))
        known = data["tokens_reported"]
        prefix = "≥" if (data["missing_tokens"] or data["missing_input"] or data["missing_output"]) and known else ""
        total = prefix + _number(data["total_tokens"]) if known else "未提供"
        rate = data["cache_hit_rate"]
        cache = f"{rate:.1%}" if rate is not None else "—" if not data["calls"] else "未提供"
        if rate is not None and data["missing_cache"]:
            cache += "（部分）"
        groups = [f"会话 Token {total}", f"缓存命中 {cache}"]
        if not compact:
            incoming = (("≥" if data["missing_input"] else "") + _number(data["input_tokens"])) if data["input_reported"] else "未提供"
            outgoing = (("≥" if data["missing_output"] else "") + _number(data["output_tokens"])) if data["output_reported"] else "未提供"
            groups.insert(1, f"入 {incoming} / 出 {outgoing}")
            if data["cache_reported"]:
                read = (("≥" if data["missing_cache_read"] else "") + _number(data["cache_read_tokens"])) if data["cache_read_reported"] else "未提供"
                write = (("≥" if data["missing_cache_write"] else "") + _number(data["cache_write_tokens"])) if data["cache_write_reported"] else "未提供"
                groups.append(f"缓存读 {read} / 写 {write}")
            if data["cost_reported"]:
                groups.append(f"费用 {'≥' if data['missing_cost'] else ''}${data['cost_usd']:.4f}")
        lines, line = [], ""
        for group in groups:
            combined = line + " · " + group if line else group
            if line and _cells(combined) > width:
                lines.append(line)
                line = group
            else:
                line = combined
        if line:
            lines.append(line)
        from .terminal import _wrap
        return "\n".join(_wrap(line, max(20, width)) for line in lines)


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

    def health(self) -> tuple[bool, str]:
        return LLMBackend.health(self)
