"""按会话目标生成短标题；协议参考 OMP 的独立 title 请求。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


TITLE_SYSTEM_PROMPT = """你负责给 Agent 会话命名。根据下一条用户消息生成一个短标题。

只输出一行：<title>标题</title>；没有明确任务时输出 <title/>。

规则：
- 标题概括用户最终目标，不要写当前微步骤。
- 优先复用用户点名的功能、组件或错误；不要使用“处理问题”“相关功能”等空泛词。
- 中文标题控制在 6-16 个汉字；英文标题使用 3-6 个词。
- 使用简短的祈使短语，不加引号、句号、解释、文件名或会话编号。
"""

_TITLE_RE = re.compile(r"<title>([\s\S]*?)</title>", re.I)
_IMAGE_RE = re.compile(r"\[Image\s*#\d+[^\]]*\]", re.I)
_LOW_SIGNAL_RE = re.compile(
    r"^(?:hi|hello|hey|ok|okay|thanks?|thank you|你好|您好|嗨|在吗|谢谢|好的|嗯+|继续)[!！,.，。?？\s]*$",
    re.I,
)


def _cells(value: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in value)


def _fit(value: str, width: int) -> str:
    if _cells(value) <= width:
        return value
    selected, used = [], 0
    for char in value:
        size = _cells(char)
        if used + size > width - 1:
            break
        selected.append(char)
        used += size
    return "".join(selected).rstrip() + "…"


def low_signal_title_input(message: str) -> bool:
    return not message.strip() or bool(_LOW_SIGNAL_RE.fullmatch(message.strip()))


def fallback_session_title(message: str) -> str:
    """模型不可用时从首条请求提取一段可读目标，避免把整段提示塞进底栏。"""
    text = _IMAGE_RE.sub("", " ".join(str(message).split())).strip()
    if low_signal_title_input(text):
        return "新对话"
    text = re.sub(r"^(?:请|麻烦)?(?:你)?(?:帮我|帮忙)?(?:直接)?(?:看一下|看看|查一下)?\s*", "", text)
    text = re.split(r"[。！？!?；;\n]", text, maxsplit=1)[0].strip(" ，,：:。.!！?？")
    return _fit(text or "新对话", 32)


def parse_generated_title(value: str) -> str | None:
    """只接受显式 title 标记，防止模型解释文字进入终端标题。"""
    if re.search(r"<title\s*/>", value, re.I):
        return None
    match = _TITLE_RE.search(value)
    if match is None:
        return None
    title = " ".join(match.group(1).split()).strip(" \t\r\n\"'“”‘’。.!！")
    if not title or title.lower() in {"none", "null", "untitled"}:
        return None
    title = "".join(char for char in title if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    return _fit(title, 40) if title else None


def generate_session_title(message: str, backend: Any) -> tuple[str | None, Any | None]:
    """使用隔离的小请求命名，不把标题协议写进主对话上下文。"""
    if low_signal_title_input(message):
        return None, None
    messages = [
        {"role": "system", "content": TITLE_SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    method = getattr(backend, "complete_messages", None)
    response = (method(messages, max_tokens=96) if method is not None
                else backend.complete(TITLE_SYSTEM_PROMPT, message, max_tokens=96))
    if not getattr(response, "ok", False):
        return None, response
    return parse_generated_title(getattr(response, "text", "")), response


__all__ = [
    "TITLE_SYSTEM_PROMPT", "fallback_session_title", "generate_session_title",
    "low_signal_title_input", "parse_generated_title",
]
