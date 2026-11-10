"""LLM 后端抽象：omp / OpenAI 兼容 / 离线回放。

统一契约：`complete(system, user) -> LLMResponse`。
大脑只依赖该接口，因此可以：用 omp 跑真实决策、用回放做确定性测试、用第三方 API 做横向对比。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import LLMConfig


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    calls: int = 1
    cache_write_tokens: int = 0
    tokens_reported: bool = False
    cache_reported: bool = False
    cost_reported: bool = False
    cache_input_tokens: int | None = None
    input_reported: bool = False
    output_reported: bool = False
    cache_read_reported: bool = False
    cache_write_reported: bool = False
    input_reported_calls: int = 0
    output_reported_calls: int = 0
    cache_read_reported_calls: int = 0
    cache_write_reported_calls: int = 0
    cost_reported_calls: int = 0

    def __post_init__(self) -> None:
        self.input_reported = self.input_reported or bool(self.input_tokens)
        self.output_reported = self.output_reported or bool(self.output_tokens)
        if self.tokens_reported and not (self.input_reported or self.output_reported):
            self.input_reported = self.output_reported = True
        self.tokens_reported = self.tokens_reported or self.input_reported or self.output_reported
        self.cache_read_reported = self.cache_read_reported or bool(self.cache_read_tokens)
        self.cache_write_reported = self.cache_write_reported or bool(self.cache_write_tokens)
        if self.cache_reported and not (self.cache_read_reported or self.cache_write_reported):
            self.cache_read_reported = self.cache_write_reported = True
        self.cache_reported = self.cache_reported or self.cache_read_reported or self.cache_write_reported
        self.cost_reported = self.cost_reported or bool(self.cost)
        for name, reported in (
            ("input_reported_calls", self.input_reported), ("output_reported_calls", self.output_reported),
            ("cache_read_reported_calls", self.cache_read_reported),
            ("cache_write_reported_calls", self.cache_write_reported),
            ("cost_reported_calls", self.cost_reported),
        ):
            if reported and not getattr(self, name) and self.calls:
                setattr(self, name, self.calls)
        if self.cache_input_tokens is None:
            self.cache_input_tokens = self.input_tokens if self.cache_reported else 0

    def merge(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost=round(self.cost + other.cost, 6),
            model=other.model or self.model,
            calls=self.calls + other.calls,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            tokens_reported=self.tokens_reported or other.tokens_reported,
            cache_reported=self.cache_reported or other.cache_reported,
            cost_reported=self.cost_reported or other.cost_reported,
            cache_input_tokens=(self.cache_input_tokens or 0) + (other.cache_input_tokens or 0),
            input_reported=self.input_reported or other.input_reported,
            output_reported=self.output_reported or other.output_reported,
            cache_read_reported=self.cache_read_reported or other.cache_read_reported,
            cache_write_reported=self.cache_write_reported or other.cache_write_reported,
            input_reported_calls=self.input_reported_calls + other.input_reported_calls,
            output_reported_calls=self.output_reported_calls + other.output_reported_calls,
            cache_read_reported_calls=self.cache_read_reported_calls + other.cache_read_reported_calls,
            cache_write_reported_calls=self.cache_write_reported_calls + other.cache_write_reported_calls,
            cost_reported_calls=self.cost_reported_calls + other.cost_reported_calls,
        )

    def delta(self, before: dict[str, Any]) -> "LLMUsage":
        """所有可加用量字段在此统一求任务差量，派生字段重新计算。"""
        input_calls = max(0, self.input_reported_calls - int(before.get("input_reported_calls", 0)))
        output_calls = max(0, self.output_reported_calls - int(before.get("output_reported_calls", 0)))
        read_calls = max(0, self.cache_read_reported_calls - int(before.get("cache_read_reported_calls", 0)))
        write_calls = max(0, self.cache_write_reported_calls - int(before.get("cache_write_reported_calls", 0)))
        cost_calls = max(0, self.cost_reported_calls - int(before.get("cost_reported_calls", 0)))
        return LLMUsage(
            input_tokens=max(0, self.input_tokens - int(before.get("input_tokens", 0))),
            output_tokens=max(0, self.output_tokens - int(before.get("output_tokens", 0))),
            cache_read_tokens=max(0, self.cache_read_tokens - int(before.get("cache_read_tokens", 0))),
            cache_write_tokens=max(0, self.cache_write_tokens - int(before.get("cache_write_tokens", 0))),
            cache_input_tokens=max(0, (self.cache_input_tokens or 0) - int(before.get("cache_input_tokens") or 0)),
            calls=max(0, self.calls - int(before.get("calls", 0))),
            cost=round(max(0, self.cost - float(before.get("cost_usd", 0))), 6),
            model=self.model, tokens_reported=bool(input_calls or output_calls),
            cache_reported=bool(read_calls or write_calls), cost_reported=bool(cost_calls),
            input_reported=bool(input_calls), output_reported=bool(output_calls),
            cache_read_reported=bool(read_calls), cache_write_reported=bool(write_calls),
            input_reported_calls=input_calls, output_reported_calls=output_calls,
            cache_read_reported_calls=read_calls, cache_write_reported_calls=write_calls,
            cost_reported_calls=cost_calls,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "tokens_reported": self.tokens_reported,
            "cache_reported": self.cache_reported,
            "cost_reported": self.cost_reported,
            "input_reported": self.input_reported,
            "output_reported": self.output_reported,
            "cache_read_reported": self.cache_read_reported,
            "cache_write_reported": self.cache_write_reported,
            "input_reported_calls": self.input_reported_calls,
            "output_reported_calls": self.output_reported_calls,
            "cache_read_reported_calls": self.cache_read_reported_calls,
            "cache_write_reported_calls": self.cache_write_reported_calls,
            "cost_reported_calls": self.cost_reported_calls,
            "cache_input_tokens": self.cache_input_tokens,
            "cache_hit_rate": (min(1.0, self.cache_read_tokens / self.cache_input_tokens)
                               if self.cache_read_reported and self.cache_input_tokens else None),
            "cost_usd": round(self.cost, 6),
            "model": self.model,
        }


@dataclass
class LLMResponse:
    text: str = ""
    ok: bool = True
    error: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    duration: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


class LLMBackend(ABC):
    """所有后端的基类。实现方只关心「给定提示词，返回文本 + 用量」。"""

    name: str = "base"

    def __init__(self, config: LLMConfig):
        self.config = config
        self.ledger = LLMUsage(model=config.model, calls=0)
        self.errors: list[str] = []

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        ...

    def _tally(self, response: LLMResponse) -> LLMResponse:
        self.ledger = self.ledger.merge(response.usage)
        if not response.ok:
            self.errors.append(response.error)
        return response

    def health(self) -> tuple[bool, str]:
        """自检：返回 (是否可用, 说明)。

        思考型模型（DeepSeek effort 档位、Claude adaptive 等）会先消耗思考 token，
        16 个 token 的预算常常连正文都产不出来，故留足 64。
        """
        try:
            response = self.complete("", "Reply with exactly: TRINA_OK", max_tokens=64)
        except Exception as exc:  # noqa: BLE001 - 自检需吞掉一切异常
            return False, f"{self.name} 后端异常: {exc}"
        if not response.ok:
            return False, response.error
        from ..model_names import display_model

        return True, f"{self.name} 后端可用（{response.duration:.1f}s，模型 {display_model(self.config.model, self.config.reasoning_effort)}）"


def make_backend(config: LLMConfig) -> LLMBackend:
    """按配置构造后端（延迟导入避免循环依赖）。"""
    if config.backend == "omp":
        from .omp_backend import OmpBackend

        return OmpBackend(config)
    if config.backend == "openai":
        from .openai_backend import OpenAICompatBackend

        return OpenAICompatBackend(config)
    if config.backend == "replay":
        from .replay_backend import ReplayBackend

        return ReplayBackend(config)
    raise ValueError(f"未知 LLM 后端: {config.backend!r}")


__all__ = ["LLMBackend", "LLMResponse", "LLMUsage", "make_backend"]
