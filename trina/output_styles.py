"""输出语义配色：界面角色与状态共用，颜色不携带执行含义。"""
from __future__ import annotations

from typing import Callable

Palette = str | Callable[[], str]
PALETTES = {
    "trina": {"text": "#ede5f2", "heading": "bold #edb2d2", "code": "#c7a5e8", "quote": "#9b7bc7",
              "muted": "#a595b3", "thinking": "#a995be", "running": "#91b9ed", "success": "#9bd3b2",
              "warning": "#e5bb86", "error": "#f18b9f", "diff_added": "#9bd3b2", "diff_removed": "#f18b9f",
              "diff_header": "#a9bdf0"},
    "contrast": {"text": "#ffffff", "heading": "bold #ffb7e8", "code": "#e2b7ff", "quote": "#8adfff",
                 "muted": "#b8b8c8", "thinking": "#c5b4e3", "running": "#73cfff", "success": "#8ff0ac",
                 "warning": "#ffdb70", "error": "#ff8d9e", "diff_added": "#8ff0ac", "diff_removed": "#ff8d9e",
                 "diff_header": "#73cfff"},
    "mono": {},
    "dark": {"text": "#ede5f2", "heading": "bold #edb2d2", "code": "#c7a5e8", "quote": "#9b7bc7",
             "muted": "#a595b3", "thinking": "#a995be", "running": "#91b9ed", "success": "#9bd3b2",
             "warning": "#e5bb86", "error": "#f18b9f", "diff_added": "#9bd3b2", "diff_removed": "#f18b9f",
             "diff_header": "#a9bdf0"},
    "light": {"text": "#25212a", "heading": "bold #7a315f", "code": "#5b3d77", "quote": "#806b8e",
              "muted": "#625b69", "thinking": "#72547f", "running": "#175f9b", "success": "#176b4b",
              "warning": "#815500", "error": "#a52743", "diff_added": "#176b4b", "diff_removed": "#a52743",
              "diff_header": "#175f9b"},
    "high_contrast": {"text": "#ffffff", "heading": "bold #ffb7e8", "code": "#e2b7ff", "quote": "#8adfff",
                      "muted": "#d0d0dc", "thinking": "#c5b4e3", "running": "#73cfff", "success": "#8ff0ac",
                      "warning": "#ffdb70", "error": "#ff8d9e", "diff_added": "#8ff0ac", "diff_removed": "#ff8d9e",
                      "diff_header": "#73cfff"},
    "ascii": {},
}


def palette_name(palette: Palette = "trina") -> str:
    value = palette() if callable(palette) else palette
    return value if value in PALETTES else "trina"


def output_style(role: str, palette: Palette = "trina", *, color: bool = True) -> str:
    return PALETTES[palette_name(palette)].get(role, "") if color else ""


def diff_style(line: str, palette: Palette = "trina", *, color: bool = True) -> str:
    if line.startswith(("diff ", "index ", "@@", "---", "+++", "rename ", "new file", "deleted file")):
        role = "diff_header"
    elif line.startswith("+"):
        role = "diff_added"
    elif line.startswith("-"):
        role = "diff_removed"
    else:
        role = "text"
    return output_style(role, palette, color=color)
