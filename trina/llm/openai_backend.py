"""标准消息与流式事件后端。

内部只接受 ``role/content`` 消息；Chat Completions、Anthropic Messages 与
OpenAI Responses 的差异在请求边界翻译。Agent 不拼接角色标签，也不把动态状态
塞回稳定的 system 前缀。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from .. import __version__
from ..config import LLMConfig
from .base import (
    LLMBackend, LLMMessage, LLMResponse, LLMStreamEvent, LLMUsage, StreamCallback, ToolCall,
)


class OpenAICompatBackend(LLMBackend):
    """支持三种常用官方方言；多轮消息始终以结构化列表传递。"""

    name = "openai"
    #: chat/completions 方言支持原生 tools；anthropic / responses 走各自翻译层，暂不启用
    supports_native_tools = True

    def __init__(self, config: LLMConfig, *, key_override: str | None = None):
        super().__init__(config)
        self.supports_native_tools = config.api == "chat"
        self._key_override = key_override
        self.base_url = (config.base_url or os.environ.get("TRINA_LLM_BASE_URL") or "").rstrip("/")
        parts = urlsplit(self.base_url)
        self._opencode_go = (
            parts.hostname in {"opencode.ai", "opencode.ai.tsecbench.gw"}
            and parts.path.rstrip("/") == "/inference/go/openai/v1"
        )
        # 一个后端实例对应一段 Agent 会话；Go 用稳定会话号做路由与缓存。
        self._session_id = uuid.uuid4().hex if self._opencode_go else ""
        self._cache_rejected: set[tuple[str, str, str]] = set()

    def api_key(self) -> str:
        if self._key_override is not None:
            return self._key_override
        if self.config.profile:
            from ..providers import ProfileStore

            return ProfileStore().key(self.config.profile, self.config.api_key_env)
        return os.environ.get(self.config.api_key_env, "")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"Trina/{__version__}",
        }
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

    def _redact_error(self, value: object) -> str:
        detail = str(value)
        key = self.api_key()
        return detail.replace(key, "<redacted>") if key else detail

    def _request_json(
        self, url: str, payload: dict | None = None, *, connectivity: bool = False,
    ) -> tuple[dict, str]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"User-Agent": f"Trina/{__version__}"} if connectivity else self._headers(),
            method="POST" if payload is not None else "GET",
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
            detail = self._redact_error(exc.read().decode("utf-8", "replace"))
            return {}, f"HTTP {exc.code}: {detail[:300]}"
        except Exception as exc:  # noqa: BLE001 - 网络层统一转成后端错误
            return {}, f"请求失败: {self._redact_error(exc)}"

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
        return sorted({
            str(row["id"])
            for row in rows
            if isinstance(row, dict) and row.get("id")
            and not any(tag in str(row["id"]).lower() for tag in ("embed", "whisper", "tts", "rerank"))
        })

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
        """单轮兼容入口；健康检查和供应商表单使用非流式响应。"""
        messages: list[LLMMessage] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._complete(messages, temperature=temperature, max_tokens=max_tokens, stream=False)

    def complete_messages(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_event: StreamCallback | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """使用 SSE 流式请求；无回调时也按流读取，避免整包等待。"""
        return self._complete(
            messages, temperature=temperature, max_tokens=max_tokens, stream=True,
            on_event=on_event, tools=tools,
        )

    def _complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        on_event: StreamCallback | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if not self.base_url:
            return self._finish_error("llm.base_url 未配置（或设置 TRINA_LLM_BASE_URL）", on_event=on_event)
        try:
            endpoint, payload = self._build_payload(
                messages, temperature=temperature, max_tokens=max_tokens, stream=stream, tools=tools,
            )
        except ValueError as exc:
            return self._finish_error(str(exc), on_event=on_event)
        from .cache import CachePolicy, cache_parameter_rejected
        identity = (self.base_url, self.config.model, self.config.api)
        policy = CachePolicy.for_request(self.config, messages, tools, disabled=identity in self._cache_rejected,
                                         base_url=self.base_url)
        # 健康检查不申请缓存写入，正式业务请求才协商扩展。
        if stream:
            try:
                cached_payload = policy.apply(payload, messages, self.config.api)
            except ValueError as exc:
                return self._finish_error(str(exc), on_event=on_event)
            errors, visible = [], []
            def forward(event):
                if event.type == "error":
                    errors.append(event)
                else:
                    if event.type in {"text_delta", "thinking_delta"}:
                        visible.append(True)
                    if on_event is not None:
                        on_event(event)
            response = self._complete_stream(f"{self.base_url}/{endpoint}", cached_payload, forward)
            if (not response.ok and not visible and cached_payload != payload
                    and cache_parameter_rejected(response.error)):
                self._cache_rejected.add(identity)
                response = self._complete_stream(f"{self.base_url}/{endpoint}", payload, on_event)
                policy.source = "server_rejected_fallback"
                policy.provider_features = set()
            elif on_event is not None:
                for event in errors:
                    on_event(event)
        else:
            response = self._complete_json(f"{self.base_url}/{endpoint}", payload)
        response.cache_diagnostic = policy.diagnostic()
        return response

    def _build_payload(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        normalized = _normalize_messages(messages)
        limit = max_tokens or self.config.max_tokens
        value = self.config.temperature if temperature is None else temperature
        endpoint = "chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": normalized,
            "temperature": value,
            "max_tokens": limit,
            "stream": stream,
        }
        if tools:
            if self.config.api != "chat":
                raise ValueError(
                    f"PROTOCOL_UNSUPPORTED: 当前 api={self.config.api} 未启用原生工具调用，请使用 JSON Action 协议"
                )
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            # OpenAI 标准通过最后一个空 choices 事件返回 usage。
            payload["stream_options"] = {"include_usage": True}
        if self.config.api == "anthropic":
            endpoint = "messages"
            system, conversation = _split_system(normalized)
            payload = {
                "model": self.config.model,
                "system": system,
                "messages": conversation,
                "temperature": value,
                "max_tokens": limit,
                "stream": stream,
            }
        elif self.config.api == "responses":
            endpoint = "responses"
            system, conversation = _split_system(normalized)
            payload = {
                "model": self.config.model,
                "instructions": system,
                "input": conversation,
                "max_output_tokens": limit,
                "stream": stream,
            }
        elif self.config.model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload.pop("temperature")
        from .reasoning import apply_reasoning

        apply_reasoning(self.config, payload)
        return endpoint, payload

    def _complete_json(self, url: str, payload: dict[str, Any]) -> LLMResponse:
        started = time.monotonic()
        body, error = self._request_json(url, payload)
        if error:
            return self._tally(LLMResponse(ok=False, error=error))
        duration = round(time.monotonic() - started, 2)
        usage = _parse_usage(body.get("usage"), self.config.api, str(body.get("model") or self.config.model))
        message: dict[str, Any] = {}
        try:
            if self.config.api == "anthropic":
                text = "\n".join(
                    block.get("text", "") for block in body["content"] if block.get("type") == "text"
                )
            elif self.config.api == "responses":
                text = "\n".join(
                    block.get("text", "")
                    for row in body["output"]
                    for block in row.get("content", [])
                    if block.get("type") == "output_text"
                )
            else:
                message = body["choices"][0]["message"]
                text = message.get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            return self._tally(LLMResponse(
                ok=False, error="响应格式异常：缺少模型输出字段", duration=duration, usage=usage,
            ))
        if isinstance(text, list):
            text = "\n".join(
                str(block.get("text", "")) for block in text if isinstance(block, dict)
            )
        if not isinstance(text, str):
            text = ""
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        if not text.strip() and not tool_calls:
            return self._tally(LLMResponse(
                ok=False, error="模型未返回正式输出", usage=usage, duration=duration,
            ))
        finish_reason = ""
        try:
            finish_reason = str(body["choices"][0].get("finish_reason") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            finish_reason = "tool_calls" if tool_calls else ""
        return self._tally(LLMResponse(
            text=text, usage=usage, duration=duration, raw=body,
            tool_calls=tool_calls, reasoning=reasoning, finish_reason=finish_reason,
        ))

    def _complete_stream(
        self, url: str, payload: dict[str, Any], on_event: StreamCallback | None,
    ) -> LLMResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        started = time.monotonic()
        parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        usage_raw: dict[str, Any] = {}
        terminal = False
        terminal_error = ""
        model = self.config.model
        last_event: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                if on_event is not None:
                    on_event(LLMStreamEvent("start"))
                for event_name, data in _iter_sse(response):
                    if data == "[DONE]":
                        if self.config.api == "chat":
                            terminal = True
                        continue
                    if not isinstance(data, dict):
                        continue
                    last_event = data
                    if isinstance(data.get("error"), dict):
                        terminal_error = str(data["error"].get("message") or data["error"])
                        break
                    response_data = data.get("response")
                    response_model = response_data.get("model") if isinstance(response_data, dict) else ""
                    model = str(data.get("model") or response_model or model)
                    if isinstance(data.get("usage"), dict):
                        usage_raw = _merge_usage(usage_raw, data["usage"])
                    deltas, ended, error, nested_usage, tool_deltas = _decode_stream_event(
                        self.config.api, event_name, data,
                    )
                    for chunk in tool_deltas:
                        _accumulate_tool_call(tool_parts, chunk)
                    if nested_usage:
                        usage_raw = _merge_usage(usage_raw, nested_usage)
                    terminal = terminal or ended
                    if error:
                        terminal_error = error
                    for kind, delta in deltas:
                        if kind == "text_delta":
                            parts.append(delta)
                        elif kind == "thinking_delta":
                            reasoning_parts.append(delta)
                        if on_event is not None and delta:
                            on_event(LLMStreamEvent(kind, delta=delta))
                    if terminal_error:
                        break
        except urllib.error.HTTPError as exc:
            detail = self._redact_error(exc.read().decode("utf-8", "replace"))
            return self._finish_error(f"HTTP {exc.code}: {detail[:300]}", on_event=on_event)
        except Exception as exc:  # noqa: BLE001 - 网络/流解析失败统一返回
            return self._finish_error(
                f"请求失败: {self._redact_error(exc)}", on_event=on_event,
                usage=_parse_usage(usage_raw, self.config.api, model),
                duration=round(time.monotonic() - started, 2),
            )

        duration = round(time.monotonic() - started, 2)
        usage = _parse_usage(usage_raw, self.config.api, model)
        if terminal_error:
            return self._finish_error(
                f"流式响应失败: {terminal_error}", usage=usage, duration=duration,
                raw=last_event, on_event=on_event,
            )
        if not terminal:
            return self._finish_error(
                "流式响应在终止事件前结束，已丢弃不完整输出", usage=usage,
                duration=duration, raw=last_event, on_event=on_event,
            )
        text = "".join(parts)
        tool_calls = _build_tool_calls(tool_parts)
        if not text.strip() and not tool_calls:
            return self._finish_error(
                "模型未返回正式输出", usage=usage, duration=duration,
                raw=last_event, on_event=on_event,
            )
        result = self._tally(LLMResponse(
            text=text, usage=usage, duration=duration, raw=last_event,
            tool_calls=tool_calls, reasoning="".join(reasoning_parts),
            finish_reason="tool_calls" if tool_calls else "",
        ))
        if on_event is not None:
            on_event(LLMStreamEvent("done", response=result))
        return result

    def _finish_error(
        self,
        error: str,
        *,
        usage: LLMUsage | None = None,
        duration: float = 0.0,
        raw: dict[str, Any] | None = None,
        on_event: StreamCallback | None = None,
    ) -> LLMResponse:
        result = self._tally(LLMResponse(
            ok=False, error=self._redact_error(error), usage=usage or LLMUsage(model=self.config.model),
            duration=duration, raw=raw or {},
        ))
        if on_event is not None:
            on_event(LLMStreamEvent("error", delta=result.error, response=result))
        return result


_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}


def _normalize_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """保留原生协议必需的结构字段。

    ``tool`` 角色承载工具结果；assistant 消息里的 ``tool_calls`` 与
    ``reasoning_content`` 必须原样带回——DeepSeek 思考模式要求上一轮的思维链
    与调用结构在同一条 assistant 消息里出现，否则下一轮会 400。
    """
    if not messages:
        raise ValueError("模型消息不能为空")
    normalized: list[LLMMessage] = []
    for item in messages:
        role = item.get("role")
        content = item.get("content")
        if role not in _MESSAGE_ROLES:
            raise ValueError("模型消息角色必须是 system/user/assistant/tool")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError("模型消息 content 必须是字符串")
        row: dict[str, Any] = {"role": role, "content": content}
        tool_calls = item.get("tool_calls")
        if tool_calls:
            if role != "assistant":
                raise ValueError("tool_calls 只能出现在 assistant 消息上")
            row["tool_calls"] = [call.to_dict() for call in (_parse_tool_calls(tool_calls))]
        tool_call_id = item.get("tool_call_id")
        if tool_call_id:
            if role != "tool":
                raise ValueError("tool_call_id 只能出现在 tool 消息上")
            row["tool_call_id"] = str(tool_call_id)
        reasoning = item.get("reasoning_content")
        if reasoning and role == "assistant":
            row["reasoning_content"] = str(reasoning)
        normalized.append(row)  # type: ignore[arg-type]
    return normalized


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    """供应商给的 tool_calls → 规范化列表；缺函数名的条目丢弃（不执行无名调用）。"""
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        call = ToolCall.from_dict(item)
        if call.name:
            calls.append(call)
    return calls


def _split_system(messages: list[LLMMessage]) -> tuple[str, list[LLMMessage]]:
    """官方 Messages/Responses 把首个 system 翻译到原生 instructions 字段。"""
    if messages and messages[0]["role"] == "system":
        return messages[0]["content"], [dict(item) for item in messages[1:]]  # type: ignore[list-item]
    return "", [dict(item) for item in messages]  # type: ignore[list-item]


def _iter_sse(response: Any) -> Iterator[tuple[str, dict[str, Any] | str]]:
    """解析标准 SSE；多行 data 在空行处合并，EOF 只刷新数据、不代表成功。"""
    event_name = ""
    data_lines: list[str] = []

    def flush() -> tuple[str, dict[str, Any] | str] | None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = ""
            return None
        raw = "\n".join(data_lines)
        name = event_name
        event_name, data_lines = "", []
        if raw == "[DONE]":
            return name, raw
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SSE data 不是合法 JSON") from exc
        return (name or str(value.get("type") or ""), value) if isinstance(value, dict) else None

    for raw_line in response:
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.rstrip("\r\n")
        if not line:
            item = flush()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    item = flush()
    if item is not None:
        yield item


def _decode_stream_event(
    api: str, event_name: str, data: dict[str, Any],
) -> tuple[list[tuple[str, str]], bool, str, dict[str, Any], list[dict[str, Any]]]:
    """SSE 事件 → (文本增量, 是否终止, 错误, usage, 工具调用增量)。"""
    deltas: list[tuple[str, str]] = []
    tool_deltas: list[dict[str, Any]] = []
    terminal = False
    error = ""
    usage: dict[str, Any] = {}
    kind = str(data.get("type") or event_name)
    if api == "anthropic":
        if kind == "content_block_delta":
            delta = data.get("delta") or {}
            value = str(delta.get("text") or delta.get("thinking") or "")
            if value:
                deltas.append(("thinking_delta" if delta.get("type") == "thinking_delta" else "text_delta", value))
        if kind in {"message_start", "message_delta"}:
            container = data.get("message") if kind == "message_start" else data
            if isinstance(container, dict) and isinstance(container.get("usage"), dict):
                usage = container["usage"]
        terminal = kind == "message_stop"
        if kind == "error":
            detail = data.get("error") or {}
            error = str(detail.get("message") if isinstance(detail, dict) else detail)
        return deltas, terminal, error, usage, tool_deltas
    if api == "responses":
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            value = str(data.get("delta") or "")
            if value:
                deltas.append(("text_delta", value))
        elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            value = str(data.get("delta") or "")
            if value:
                deltas.append(("thinking_delta", value))
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            terminal = True
            response = data.get("response") or {}
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                usage = response["usage"]
            if kind != "response.completed":
                detail = response.get("error") or response.get("incomplete_details") or kind
                error = str(detail)
        elif kind == "error":
            terminal = True
            error = str(data.get("message") or data.get("error") or "Responses 流错误")
        return deltas, terminal, error, usage, tool_deltas

    choices = data.get("choices") or []
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                content = delta.get("content")
                if isinstance(reasoning, str) and reasoning:
                    deltas.append(("thinking_delta", reasoning))
                if isinstance(content, str) and content:
                    deltas.append(("text_delta", content))
                for item in delta.get("tool_calls") or []:
                    if isinstance(item, dict):
                        tool_deltas.append(item)
            finish_reason = choice.get("finish_reason")
            terminal = terminal or finish_reason is not None
            if finish_reason in {"length", "content_filter"}:
                error = f"模型输出未完整结束: {finish_reason}"
    return deltas, terminal, error, usage, tool_deltas


def _accumulate_tool_call(slots: dict[int, dict[str, str]], chunk: dict[str, Any]) -> None:
    """流式 tool_calls 是分片到达的：按 index 聚合 id/name/arguments。"""
    try:
        index = int(chunk.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if chunk.get("id"):
        slot["id"] = str(chunk["id"])
    function = chunk.get("function") if isinstance(chunk.get("function"), dict) else chunk
    if function.get("name"):
        slot["name"] += str(function["name"])
    if function.get("arguments"):
        slot["arguments"] += str(function["arguments"])


def _build_tool_calls(slots: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(slots):
        slot = slots[index]
        if not slot.get("name"):
            continue
        calls.append(ToolCall(id=slot.get("id") or f"call_{index}", name=slot["name"],
                              arguments=slot.get("arguments") or "{}"))
    return calls


def _merge_usage(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """SSE 可能把输入与输出用量拆到不同事件；后值只覆盖非空字段。"""
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_usage(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


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
    input_known = any(raw.get(key) is not None for key in ("input_tokens", "prompt_tokens"))
    if not input_known and all(raw.get(key) is not None for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")):
        inputs = cached + count(raw["prompt_cache_miss_tokens"])
        input_known = True
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
        input_reported=input_known,
        output_reported=any(raw.get(key) is not None for key in ("output_tokens", "completion_tokens")),
        cache_read_reported=(details.get("cached_tokens") is not None or any(
            raw.get(key) is not None for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"))),
        cache_write_reported=(details.get("cache_write_tokens") is not None or
                              raw.get("cache_creation_input_tokens") is not None),
        tokens_reported=any(raw.get(key) is not None for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")),
        cache_reported=cache_known, cache_input_tokens=inputs if cache_known else 0,
        cost=cost_value, cost_reported=cost is not None, model=model,
    )
