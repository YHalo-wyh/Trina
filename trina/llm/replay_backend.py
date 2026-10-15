"""离线回放后端：让大脑的决策过程可确定性复现（测试与回归评测的基础设施）。

脚本格式（YAML/JSON）::

    steps:
      - match: "阶段: recon"          # 可选：提示词正则；不匹配则按顺序消耗
        response: '{"thought": "先扫端口", "tool": "port_scan", "params": {"target": "127.0.0.1"}}'
      - response: '{"thought": "收尾", "final": {"summary": "完成"}}'
    default: '{"thought": "无更多动作", "final": {"summary": "回放脚本耗尽"}}'

用途：录制一次真实 run 的 LLM 交互（`trina run --record`）后离线重放，
用于回归对比（工具/护栏改动不应改变决策序列）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import LLMConfig
from .base import LLMBackend, LLMResponse, LLMUsage


class ReplayBackend(LLMBackend):
    name = "replay"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self.calls = 0
        self.script: list[dict[str, str]] = []
        self.default = '{"thought": "回放脚本已耗尽", "final": {"summary": "REPLAY_EXHAUSTED"}}'
        if config.replay_script:
            self.load(config.replay_script)

    def load(self, path: str | Path) -> None:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            import yaml

            data = yaml.safe_load(raw) or {}
        if isinstance(data, list):
            data = {"steps": data}
        self.script = list(data.get("steps") or [])
        self.default = data.get("default", self.default)

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls += 1
        haystack = f"{system}\n{user}"
        # 1) 先按 match 正则挑选（与调用序号无关）
        for step in list(self.script):
            pattern = step.get("match")
            if pattern and re.search(pattern, haystack, re.S):
                self.script.remove(step)
                return self._respond(step)
        # 2) 否则按顺序消费第一个无 match 的步骤
        for step in list(self.script):
            if not step.get("match"):
                self.script.remove(step)
                return self._respond(step)
        return self._tally(
            LLMResponse(text=self.default, usage=LLMUsage(model="replay"), duration=0.0, ok=True)
        )

    def _respond(self, step: dict[str, str]) -> LLMResponse:
        return self._tally(
            LLMResponse(text=str(step.get("response", "")), usage=LLMUsage(model="replay"), duration=0.0, ok=True)
        )
