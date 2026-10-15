"""OpenAI 兼容后端：对接任意 `/chat/completions` 端点（本地 vLLM / 网关 / 云 API）。

仅使用标准库 urllib，避免为可选后端引入额外依赖。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..config import LLMConfig
from .base import LLMBackend, LLMResponse, LLMUsage


class OpenAICompatBackend(LLMBackend):
    name = "openai"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self.base_url = (config.base_url or os.environ.get("TRINA_LLM_BASE_URL") or "").rstrip("/")

    def api_key(self) -> str:
        return os.environ.get(self.config.api_key_env, "")

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.base_url:
            return self._tally(LLMResponse(ok=False, error="llm.base_url 未配置（或设置 TRINA_LLM_BASE_URL）"))
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key()}"} if self.api_key() else {})},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310 - 用户显式配置的端点
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return self._tally(LLMResponse(ok=False, error=f"HTTP {exc.code}: {detail}"))
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转为错误响应
            return self._tally(LLMResponse(ok=False, error=f"请求失败: {exc}"))

        duration = round(time.monotonic() - started, 2)
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return self._tally(LLMResponse(ok=False, error=f"响应格式异常: {str(body)[:200]}", duration=duration))
        text = message.get("content") or ""
        if not text.strip():
            # 推理型模型（如 DeepSeek 系列）可能把内容放在 reasoning_content
            text = message.get("reasoning_content") or message.get("reasoning") or ""
        if not text.strip():
            return self._tally(
                LLMResponse(ok=False, error=f"模型返回空内容: {str(message)[:200]}", duration=duration)
            )
        raw_usage = body.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
            model=str(body.get("model") or self.config.model),
        )
        return self._tally(LLMResponse(text=text, usage=usage, duration=duration, ok=True))
