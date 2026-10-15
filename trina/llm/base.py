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

    def merge(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost=round(self.cost + other.cost, 6),
            model=other.model or self.model,
            calls=self.calls + other.calls,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
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
        self.ledger = LLMUsage(model=config.model)
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
        """自检：返回 (是否可用, 说明)。"""
        try:
            response = self.complete("You are a health probe. Be terse.", "Reply with exactly: TRINA_OK", max_tokens=16)
        except Exception as exc:  # noqa: BLE001 - 自检需吞掉一切异常
            return False, f"{self.name} 后端异常: {exc}"
        if not response.ok:
            return False, response.error
        return True, f"{self.name} 后端可用（{response.duration:.1f}s，模型 {self.config.model}）"


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


__all__ = ["LLMBackend", "LLMResponse", "LLMUsage", "make_backend", "start_timer"]


def start_timer() -> float:
    return time.monotonic()
