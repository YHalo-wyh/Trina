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
    missing_cache: int = 0
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
            self.missing_cache += int(not usage.cache_reported)
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
            self.missing_tokens = self.missing_cache = self.missing_cost = self.pending = 0
            self.set_activity("idle", "就绪")

    def usage_data(self) -> dict[str, Any]:
        with self._lock:
            return {**self.usage.to_dict(), "missing_tokens": self.missing_tokens, "missing_cache": self.missing_cache, "missing_cost": self.missing_cost}

    def footer(self, width: int, compact: bool = False) -> str:
        data = self.usage_data()
        known = data["tokens_reported"] or not data["calls"]
        prefix = "≥" if data["missing_tokens"] and known else ""
        total = prefix + _number(data["total_tokens"]) if known else "未提供"
        rate = data["cache_hit_rate"]
        cache = f"{rate:.1%}" if rate is not None else "—" if data["cache_reported"] or not data["calls"] else "未提供"
        if rate is not None and data["missing_cache"]:
            cache += "（部分）"
        groups = [f"会话 Token {total}", f"缓存命中 {cache}"]
        if not compact:
            groups.insert(1, f"入 {prefix + _number(data['input_tokens']) if known else '—'} / 出 {prefix + _number(data['output_tokens']) if known else '—'}")
            if data["cache_reported"]:
                groups.append(f"缓存读 {_number(data['cache_read_tokens'])} / 写 {_number(data['cache_write_tokens'])}")
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
