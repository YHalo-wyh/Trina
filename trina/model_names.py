"""模型代号：把 API 模型 ID 翻译成面向人的展示名。

API 只认 ``deepseek-flash`` 这类 ID；界面、报告、审计里显示成
``DeepSeek V4.1 Flash Max``。本模块只做字符串映射，不发请求、不改配置。
"""

from __future__ import annotations

#: 已知模型的展示代号（键为去掉 provider 前缀后的小写 ID）。
#: 新增官方模型时在这里补一行；未收录的 ID 原样展示，不做猜测。
MODEL_ALIASES: dict[str, str] = {
    # DeepSeek
    "deepseek-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-chat": "DeepSeek Chat",
    "deepseek-reasoner": "DeepSeek Reasoner",
    "deepseek-r1": "DeepSeek R1",
    # OpenAI
    "gpt-5.2": "GPT-5.2",
    "gpt-5.1": "GPT-5.1",
    "gpt-5": "GPT-5",
    "gpt-6": "GPT-6",
    "gpt-4.1": "GPT-4.1",
    "gpt-4.1-mini": "GPT-4.1 mini",
    # Anthropic
    "claude-opus-4.8": "Claude Opus 4.8",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    # Google
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    # 国内模型
    "qwen-plus": "Qwen Plus",
    "qwen-turbo": "Qwen Turbo",
    "qwen-max": "Qwen Max",
    "glm-5": "GLM-5",
    "glm-4-flash": "GLM-4 Flash",
    "kimi-k2.5": "Kimi K2.5",
    "moonshot-v1-32k": "Moonshot v1 32K",
    # 其他
    "mistral-large-latest": "Mistral Large",
    "mistral-small-latest": "Mistral Small",
    "llama-3.3-70b-versatile": "Llama 3.3 70B",
    "sonar": "Perplexity Sonar",
    "sonar-pro": "Perplexity Sonar Pro",
}

#: 思考档位后缀；`default` 表示沿用供应商默认，不显示后缀。
EFFORT_LABELS: dict[str, str] = {
    "default": "",
    "off": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
}


def base_name(model: str) -> str:
    """模型 ID → 展示名；未收录的保持原样。"""
    raw = (model or "").strip()
    if not raw:
        return "未配置"
    key = raw.rsplit("/", 1)[-1].lower()
    return MODEL_ALIASES.get(key, raw)


def effort_label(effort: str) -> str:
    """思考档位 → 后缀词；`default` 与未知档位返回空串。"""
    return EFFORT_LABELS.get((effort or "default").strip().lower(), "")


def display_model(model: str, effort: str = "default") -> str:
    """``deepseek-flash`` + ``max`` → ``DeepSeek V4.1 Flash Max``。"""
    raw = (model or "").strip()
    if not raw:
        return "未配置"
    name = base_name(raw)
    label = effort_label(effort)
    return f"{name} {label}".strip()


__all__ = ["EFFORT_LABELS", "MODEL_ALIASES", "base_name", "display_model", "effort_label"]
