"""omp 后端 —— 接入已配置的模型与鉴权。

复用 omp 已配置的模型与鉴权，无需在 Trina 侧管理任何 API Key：

    omp -p --no-session --no-tools --mode=json --model <model>

提示词经 stdin 传入，模型输出以 JSONL 事件流返回，我们从中提取
`message_end.message.content[].text` 与 usage/cost。`--no-tools` 很关键：
工具由 Trina 的护栏收口执行，绝不能交给 omp 内建工具绕过审计。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from ..config import LLMConfig
from .base import LLMBackend, LLMResponse, LLMUsage

DEFAULT_TIMEOUT = 180


class OmpBackend(LLMBackend):
    name = "omp"

    def __init__(self, config: LLMConfig, binary: str | None = None):
        super().__init__(config)
        self.binary = binary or os.environ.get("TRINA_OMP_BIN") or shutil.which("omp") or "omp"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None or os.path.exists(self.binary)

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.available():
            return self._tally(LLMResponse(ok=False, error=f"未找到 omp 可执行文件: {self.binary}"))

        payload = user if not system else f"{system}\n\n---\n\n{user}"
        argv = [
            self.binary,
            "-p",
            "--no-session",
            "--no-tools",
            "--mode=json",
            "--model",
            self.config.model,
            "--no-title",
        ]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=max(5, self.config.timeout or DEFAULT_TIMEOUT),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._tally(
                LLMResponse(
                    ok=False,
                    error=f"omp 调用超时（{self.config.timeout}s）",
                    duration=round(time.monotonic() - started, 2),
                )
            )
        except OSError as exc:
            return self._tally(LLMResponse(ok=False, error=f"omp 启动失败: {exc}"))

        duration = round(time.monotonic() - started, 2)
        text, usage, error = self._parse_jsonl(proc.stdout)
        if not text and proc.returncode != 0:
            return self._tally(
                LLMResponse(
                    ok=False,
                    error=f"omp 退出码 {proc.returncode}: {(proc.stderr or '').strip()[-300:]}",
                    duration=duration,
                    usage=usage,
                )
            )
        if not text:
            return self._tally(
                LLMResponse(ok=False, error="omp 未返回文本内容", duration=duration, usage=usage, raw={"stderr": proc.stderr[-500:]})
            )
        usage.model = usage.model or self.config.model
        return self._tally(LLMResponse(text=text, usage=usage, duration=duration, ok=True))

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _parse_jsonl(stdout: str) -> tuple[str, LLMUsage, str]:
        """从 `--mode=json` 事件流里取最终文本与用量。"""
        usage = LLMUsage()
        chunks: list[str] = []
        final_text = ""
        error = ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "message_update":
                inner = event.get("assistantMessageEvent") or {}
                if inner.get("type") == "text_delta":
                    chunks.append(str(inner.get("delta", "")))
                elif inner.get("type") == "text_end" and not chunks:
                    chunks.append(str(inner.get("content", "")))
            elif etype in ("message_end", "turn_end"):
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    final_text = _message_text(message) or final_text
                    usage = _usage_from(message.get("usage") or {}, usage)
            elif etype == "agent_end":
                for message in event.get("messages") or []:
                    if message.get("role") == "assistant":
                        final_text = _message_text(message) or final_text
                        usage = _usage_from(message.get("usage") or {}, usage)
            elif etype == "error":
                error = str(event.get("error") or event.get("message") or "")[:300]
        text = final_text or "".join(chunks)
        return text, usage, error


def _message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _usage_from(raw: dict[str, Any], fallback: LLMUsage) -> LLMUsage:
    if not raw:
        return fallback
    cost = raw.get("cost") or {}
    if isinstance(cost, dict):
        total_cost = float(cost.get("total") or 0.0)
    else:
        total_cost = float(cost or 0.0)
    return LLMUsage(
        input_tokens=int(raw.get("input") or raw.get("inputTokens") or 0),
        output_tokens=int(raw.get("output") or raw.get("outputTokens") or 0),
        cache_read_tokens=int(raw.get("cacheRead") or 0),
        cost=total_cost,
        model=str(raw.get("model") or ""),
    )
