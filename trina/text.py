"""无界面依赖的字面文本净化；核心运行时和终端共用。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


def clean_text(value: Any) -> str:
    text = _ANSI.sub("", str(value)).replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
