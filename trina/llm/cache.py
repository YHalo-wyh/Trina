"""供应商缓存能力适配；内部前缀 ID 只映射到供应商明确支持的字段。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass
class CachePolicy:
    enabled: bool
    mode: str
    retention: str
    stable_prefix_id: str
    provider_features: set[str] = field(default_factory=set)
    source: str = "passive"

    @classmethod
    def for_request(cls, config, messages, tools=None, *, disabled=False, base_url=None):
        host = urlsplit(config.base_url if base_url is None else base_url).hostname
        features = set(config.cache_features)
        source = "declared" if features else "passive"
        if host == "api.openai.com" and config.api in {"chat", "responses"}:
            features.add("openai_key")
            match = re.match(r"gpt-(\d+)(?:\.(\d+))?", config.model)
            modern = match and (int(match[1]), int(match[2] or 0)) >= (5, 6)
            features.add("openai_options" if modern else "openai_retention")
            source = "official"
        elif host == "api.anthropic.com" and config.api == "anthropic":
            features.add("anthropic_cache")
            source = "official"
        elif host == "api.deepseek.com":
            source = "official_automatic"
        if disabled or config.cache_mode == "off":
            features = set()
            source = "rejected" if disabled else "disabled"
        core = next((item["content"] for item in messages if item.get("role") == "system"), "")
        prefix_id = _digest([config.cache_namespace, core, tools or []])
        return cls(not disabled and config.cache_mode != "off", config.cache_mode,
                   config.cache_ttl, prefix_id, features, source)

    def apply(self, payload: dict, messages: list, api: str) -> dict:
        if not self.enabled:
            return payload
        features = self.provider_features
        if self.mode == "explicit" and not features & {"openai_options", "anthropic_cache"}:
            raise ValueError("此连接未声明显式缓存能力；使用 cache_mode=auto 或配置 cache_features")
        result = copy.deepcopy(payload)
        stable = [index for index, row in enumerate(messages) if
                  row.get("role") == "system" or row.get("cache_scope") == "stable"][:2]
        if "openai_key" in features and api in {"chat", "responses"}:
            result["prompt_cache_key"] = "trina-" + self.stable_prefix_id
        if "openai_options" in features and api in {"chat", "responses"}:
            allowed = {"", "30m", "12h"} if api == "responses" else {"", "30m"}
            if self.retention not in allowed:
                raise ValueError(f"此 OpenAI {api} 缓存策略支持的 ttl 为 {sorted(allowed - {''})}")
            result["prompt_cache_options"] = {"mode": "explicit" if self.mode == "explicit" else "implicit"}
            if self.retention:
                result["prompt_cache_options"]["ttl"] = self.retention
            if api == "responses":
                # instructions 是字符串字段；显式断点必须放入原生 input 内容块。
                instructions = result.pop("instructions", "")
                if messages and messages[0].get("role") == "system":
                    result["input"].insert(0, {"role": "developer", "content": instructions})
                for row in result["input"]:
                    if row["role"] == "system":
                        row["role"] = "developer"
            rows = result["input"] if api == "responses" else result["messages"]
            for index in stable:
                rows[index]["content"] = [{"type": "input_text" if api == "responses" else "text",
                                          "text": rows[index]["content"],
                                          "prompt_cache_breakpoint": {"mode": "explicit"}}]
        elif "openai_retention" in features and api in {"chat", "responses"}:
            if self.retention not in {"", "in_memory", "24h"}:
                raise ValueError("较早 OpenAI 模型使用 cache_ttl=in_memory|24h")
            if self.retention:
                result["prompt_cache_retention"] = self.retention
        if "anthropic_cache" in features and api == "anthropic":
            if self.retention not in {"", "5m", "1h"}:
                raise ValueError("Anthropic 缓存使用 cache_ttl=5m|1h")
            control = {"type": "ephemeral"}
            if self.retention:
                control["ttl"] = self.retention
            if self.mode == "auto":
                result["cache_control"] = dict(control)
            has_system = bool(messages and messages[0].get("role") == "system")
            for index in stable:
                if has_system and index == 0:
                    result["system"] = [{"type": "text", "text": result["system"], "cache_control": dict(control)}]
                else:
                    row = result["messages"][index - int(has_system)]
                    row["content"] = [{"type": "text", "text": row["content"], "cache_control": dict(control)}]
        return result

    def diagnostic(self) -> dict:
        return {"mode": self.mode, "source": self.source, "stable_prefix_id": self.stable_prefix_id,
                "features": sorted(self.provider_features), "ttl": self.retention or None}


def cache_parameter_rejected(error: str) -> bool:
    value = error.lower()
    return any(code in value for code in ("http 400", "http 422")) and any(
        field in value for field in ("prompt_cache", "cache_control")) and any(
        word in value for word in ("unsupported", "unknown", "unrecognized", "not supported", "not permitted", "extra inputs"))
