"""无界面依赖的字面文本净化；核心运行时和终端共用。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


def clean_text(value: Any, *, preserve_tabs: bool = False) -> str:
    text = _ANSI.sub("", str(value))
    if not preserve_tabs:
        text = text.replace("\t", "    ")
    whitespace = "\n\t" if preserve_tabs else "\n"
    return "".join(ch for ch in text if ch in whitespace or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
