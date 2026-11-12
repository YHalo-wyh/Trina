"""将思考选项转换为接口参数；不向提示词插入思考指令。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ..config import LLMConfig

LEVEL_LABELS = {"default": "默认", "off": "关闭", "minimal": "最少", "low": "低", "medium": "中",
                "high": "高", "xhigh": "很高", "max": "最高"}


@dataclass(frozen=True)
class ReasoningSupport:
    mode: str
    levels: tuple[str, ...]

    def label(self, level: str) -> str:
        return "开启" if self.mode in {"toggle", "qwen"} and level == "high" else LEVEL_LABELS[level]


def reasoning_support(config: LLMConfig) -> ReasoningSupport:
    model = config.model.lower().split("/")[-1]
    host = urlsplit(config.base_url).hostname or ""
    common = ("default", "low", "medium", "high")
    if config.backend != "openai":
        return ReasoningSupport("none", ("default",))
    # 固定思考 / 非思考型号不应显示无法生效的开关。
    if "deepseek-r1" in model or (model.startswith("qwen3") and any(x in model for x in ("coder", "thinking", "instruct"))):
        return ReasoningSupport("none", ("default",))
    if "deepseek" in model or host == "api.deepseek.com":
        return ReasoningSupport("deepseek", ("default", "off", "low", "high", "max"))
    if config.api == "anthropic" and "claude" in model:
        if re.search(r"claude-(?:(?:opus|sonnet|haiku)[-.])?5(?:[-.]|$)|4[-.][678](?:[-.]|$)", model):
            levels = ("default", "off", "low", "medium", "high")
            if "4-6" not in model and "4.6" not in model:
                levels += ("xhigh",)
            return ReasoningSupport("adaptive", levels + ("max",))
        if re.search(r"3[-.]7|4[-.][015]", model):
            return ReasoningSupport("budget", ("default", "off", "low", "medium", "high"))
    if config.api == "chat" and (model.startswith("glm-5") or model.startswith("glm-4.5") or model.startswith("glm-4.6") or model.startswith("glm-4.7") or model.startswith("kimi-k2.5")):
        return ReasoningSupport("toggle", ("default", "off", "high"))
    if config.api == "chat" and model.startswith(("qwen3", "qwen-plus", "qwen-turbo")):
        return ReasoningSupport("qwen", ("default", "off", "high"))
    if config.api == "chat" and model.startswith(("gemini-2.5", "gemini-3", "gemini-4")):
        return ReasoningSupport("effort", common)
    if model.startswith(("gpt-6", "o1", "o3", "o4", "gpt-oss")):
        return ReasoningSupport("effort", common + (("xhigh", "max") if model.startswith("gpt-6") else ()))
    if model.startswith("gpt-5"):
        if "-chat" in model:
            return ReasoningSupport("none", ("default",))
        if "-pro" in model:
            levels = ("medium", "high", "xhigh") if re.match(r"gpt-5[.-][2-9]", model) else ("high",)
            return ReasoningSupport("effort", ("default",) + levels)
        if "codex" in model:
            return ReasoningSupport("effort", common + (("xhigh",) if re.match(r"gpt-5[.-][2-9]", model) else ()))
        if re.match(r"gpt-5[.-][1-9]", model):
            return ReasoningSupport("effort", ("default", "off", "low", "medium", "high", "xhigh"))
        return ReasoningSupport("effort", ("default", "minimal", "low", "medium", "high"))
    return ReasoningSupport("none", ("default",))


def apply_reasoning(config: LLMConfig, payload: dict) -> None:
    level = config.reasoning_effort
    if level == "default":
        return
    support = reasoning_support(config)
    if level not in support.levels:
        raise ValueError(f"当前模型不支持思考档位 {level}；请在 /model 重新选择")
    mode = support.mode
    if urlsplit(config.base_url).hostname == "openrouter.ai":
        payload["reasoning"] = {"enabled": False} if level == "off" else {"effort": level}
        return
    if mode == "deepseek":
        payload.pop("temperature", None)
        if config.api == "responses":
            payload["reasoning"] = {"effort": "none" if level == "off" else level}
        else:
            payload["thinking"] = {"type": "disabled" if level == "off" else "enabled"}
            if level != "off":
                if config.api == "anthropic":
                    payload["output_config"] = {"effort": level}
                else:
                    payload["reasoning_effort"] = level
    elif mode == "adaptive":
        payload.pop("temperature", None)
        payload["thinking"] = {"type": "disabled" if level == "off" else "adaptive"}
        if level != "off":
            payload["output_config"] = {"effort": level}
    elif mode == "budget":
        payload.pop("temperature", None)
        budget = min({"low": 1024, "medium": 2048, "high": 8192}.get(level, 0), payload["max_tokens"] - 1)
        # 小输出健康检查不足以分配供应商要求的最少 1024 个思考 token。
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget} if budget >= 1024 else {"type": "disabled"}
    elif mode == "toggle":
        payload["thinking"] = {"type": "disabled" if level == "off" else "enabled"}
    elif mode == "qwen":
        payload["enable_thinking"] = level != "off"
    elif mode == "effort":
        effort = "none" if level == "off" else level
        if config.api == "responses":
            payload["reasoning"] = {"effort": effort}
        else:
            payload["reasoning_effort"] = effort
