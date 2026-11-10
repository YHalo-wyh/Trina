"""OpenAI 兼容后端：对接任意 `/chat/completions` 端点（本地 vLLM / 网关 / 云 API）。

仅使用标准库 urllib，避免为可选后端引入额外依赖。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit

from ..config import LLMConfig
from .base import LLMBackend, LLMResponse, LLMUsage


class OpenAICompatBackend(LLMBackend):
    name = "openai"

    def __init__(self, config: LLMConfig, *, key_override: str | None = None):
        super().__init__(config)
        self._key_override = key_override
        self.base_url = (config.base_url or os.environ.get("TRINA_LLM_BASE_URL") or "").rstrip("/")
        parts = urlsplit(self.base_url)
        self._opencode_go = (
            parts.hostname in {"opencode.ai", "opencode.ai.tsecbench.gw"}
            and parts.path.rstrip("/") == "/inference/go/openai/v1"
        )
        # 同一个后端实例对应一段 Agent 对话；Go 用稳定会话号做路由与缓存。
        self._session_id = uuid.uuid4().hex if self._opencode_go else ""

    def api_key(self) -> str:
        if self._key_override is not None:
            return self._key_override
        if self.config.profile:
            from ..providers import ProfileStore
            return ProfileStore().key(self.config.profile, self.config.api_key_env)
        return os.environ.get(self.config.api_key_env, "")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "Trina/0.2.0"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if self.config.api == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
        if self._opencode_go:
            headers["x-opencode-session"] = self._session_id
        return headers

    def _request_json(self, url: str, payload: dict | None = None, *, connectivity: bool = False) -> tuple[dict, str]:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"User-Agent": "Trina/0.2.0"} if connectivity else self._headers(), method="POST" if payload is not None else "GET",
        )
        try:
            timeout = min(max(1, self.config.timeout), 10) if connectivity else self.config.timeout
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if connectivity:
                    return {"status": response.status}, ""
                body = json.loads(response.read().decode("utf-8", "replace"))
                if not isinstance(body, dict):
                    return {}, "模型响应必须是 JSON 对象"
                return body, ""
        except urllib.error.HTTPError as exc:
            if connectivity:
                exc.close()
                return {"status": exc.code}, ""
            detail = exc.read().decode("utf-8", "replace")
            key = self.api_key()
            if key:
                detail = detail.replace(key, "<redacted>")
            return {}, f"HTTP {exc.code}: {detail[:300]}"
        except Exception as exc:
            detail = str(exc)
            key = self.api_key()
            return {}, f"请求失败: {detail.replace(key, '<redacted>') if key else detail}"

    def list_remote_models(self) -> list[str]:
        """仅在模型管理页显式刷新时查询当前连接的模型目录。"""
        base = self.base_url
        if self._opencode_go:
            base = base.replace("/go/openai/v1", "/go/v1")
        body, error = self._request_json(f"{base}/models")
        if error:
            raise ValueError(error)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("供应商没有返回 data 模型数组；可手动输入模型 ID")
        return sorted({str(row["id"]) for row in rows
                       if isinstance(row, dict) and row.get("id")
                       and not any(tag in str(row["id"]).lower() for tag in ("embed", "whisper", "tts", "rerank"))})

    def check_connectivity(self) -> tuple[bool, str]:
        """只测试配置地址是否可达，不发送凭据或生成模型输出。"""
        from ..providers import validate_address
        validate_address(self.base_url)
        started = time.monotonic()
        body, error = self._request_json(self.base_url, connectivity=True)
        if error:
            return False, f"地址不可达：{error}"
        elapsed = round((time.monotonic() - started) * 1000)
        return True, f"地址可达 · HTTP {body['status']} · {elapsed} ms；Key / 模型未验证"

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
        endpoint = "chat/completions"
        if self.config.api == "anthropic":
            endpoint = "messages"
            payload.pop("messages")
            payload.update(system=system, messages=[{"role": "user", "content": user}])
        elif self.config.api == "responses":
            endpoint = "responses"
            payload = {"model": self.config.model, "instructions": system, "input": user,
                       "max_output_tokens": max_tokens or self.config.max_tokens, "stream": False}
        elif self.config.model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload.pop("temperature")
        from .reasoning import apply_reasoning
        try:
            apply_reasoning(self.config, payload)
        except ValueError as exc:
            return self._tally(LLMResponse(ok=False, error=str(exc)))
        started = time.monotonic()
        body, error = self._request_json(f"{self.base_url}/{endpoint}", payload)
        if error:
            return self._tally(LLMResponse(ok=False, error=error))

        duration = round(time.monotonic() - started, 2)
        usage = _parse_usage(body.get("usage"), self.config.api, str(body.get("model") or self.config.model))
        try:
            if self.config.api == "anthropic":
                message = {"content": "\n".join(block.get("text", "") for block in body["content"] if block.get("type") == "text")}
            elif self.config.api == "responses":
                message = {"content": "\n".join(block.get("text", "") for row in body["output"] for block in row.get("content", []) if block.get("type") == "output_text")}
            else:
                message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, AttributeError):
            return self._tally(LLMResponse(ok=False, error="响应格式异常：缺少模型输出字段", duration=duration, usage=usage))
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "\n".join(str(block.get("text", "")) for block in text if isinstance(block, dict))
        if not isinstance(text, str):
            text = ""
        if not text.strip():
            return self._tally(LLMResponse(ok=False, error="模型未返回正式输出", usage=usage, duration=duration))
        return self._tally(LLMResponse(text=text, usage=usage, duration=duration, ok=True))


def _parse_usage(raw: Any, api: str, model: str) -> LLMUsage:
    """将不同协议的输入统一为总输入，缓存读写是其中的子集。"""
    if not isinstance(raw, dict):
        return LLMUsage(model=model)

    def count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = count(details.get("cached_tokens", raw.get("prompt_cache_hit_tokens", raw.get("cache_read_input_tokens"))))
    written = count(details.get("cache_write_tokens", raw.get("cache_creation_input_tokens")))
    inputs = count(raw.get("prompt_tokens", raw.get("input_tokens")))
    if api == "anthropic":
        inputs += cached + written
    cache_known = any(details.get(key) is not None for key in ("cached_tokens", "cache_write_tokens")) or any(
        raw.get(key) is not None for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    cost = raw.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("total")
    try:
        cost_value = max(0.0, float(cost or 0))
    except (TypeError, ValueError, OverflowError):
        cost_value = 0.0
    return LLMUsage(
        input_tokens=inputs, output_tokens=count(raw.get("completion_tokens", raw.get("output_tokens"))),
        cache_read_tokens=cached, cache_write_tokens=written,
        input_reported=any(raw.get(key) is not None for key in ("input_tokens", "prompt_tokens")),
        output_reported=any(raw.get(key) is not None for key in ("output_tokens", "completion_tokens")),
        cache_read_reported=(details.get("cached_tokens") is not None or any(
            raw.get(key) is not None for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"))),
        cache_write_reported=(details.get("cache_write_tokens") is not None or
                              raw.get("cache_creation_input_tokens") is not None),
        tokens_reported=any(raw.get(key) is not None for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")),
        cache_reported=cache_known, cache_input_tokens=inputs if cache_known else 0,
        cost=cost_value, cost_reported=cost is not None, model=model,
    )
