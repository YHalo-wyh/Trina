"""终端工作台主题：集中定义界面颜色与 ASCII 兼容字形。"""
from __future__ import annotations

from typing import Callable, Final


THEME_ALIASES: Final = {
    "trina": "dark",
    "contrast": "high_contrast",
    "mono": "ascii",
    "high-contrast": "high_contrast",
}

THEME_LABELS: Final = {
    "dark": "Dark · 深色粉紫",
    "light": "Light · 浅色高可读",
    "high_contrast": "High Contrast · 高对比",
    "ascii": "ASCII · 无色兼容",
}

_COLORS: Final = {
    "dark": {
        "text": "#ede5f2", "muted": "#a595b3", "primary": "#edb2d2", "secondary": "#c7a5e8",
        "border": "#9b7bc7", "surface": "#33283c", "surface_alt": "#211a2a", "ink": "#17131c",
        "success": "#99d7bc", "waiting": "#e5bb86", "error": "#f18b9f", "running": "#91b9ed",
    },
    "light": {
        "text": "#25212a", "muted": "#625b69", "primary": "#7a315f", "secondary": "#5b3d77",
        "border": "#806b8e", "surface": "#eadfec", "surface_alt": "#f4edf5", "ink": "#fffaff",
        "success": "#176b4b", "waiting": "#815500", "error": "#a52743", "running": "#175f9b",
    },
    "high_contrast": {
        "text": "#ffffff", "muted": "#d0d0dc", "primary": "#ffb7e8", "secondary": "#8adfff",
        "border": "#ffffff", "surface": "#151515", "surface_alt": "#000000", "ink": "#000000",
        "success": "#8ff0ac", "waiting": "#ffdb70", "error": "#ff8d9e", "running": "#73cfff",
    },
    "ascii": {},
}


def theme_name(value: str | Callable[[], str]) -> str:
    """兼容旧配色名，并对未知值安全回退到深色主题。"""
    value = value() if callable(value) else value
    value = THEME_ALIASES.get(value, value)
    return value if value in _COLORS else "dark"


def ascii_mode(value: str | Callable[[], str]) -> bool:
    return theme_name(value) == "ascii"


def ui_style(value: str | Callable[[], str], *, plain: bool = False) -> dict[str, str]:
    """生成 prompt_toolkit 共用样式；ASCII/无色模式不依赖颜色表达状态。"""
    colors = _COLORS[theme_name(value)]
    if plain or not colors:
        return {"selection": "reverse", "chosen": "reverse", "model.row": "reverse",
                "model.tab": "reverse", "button.focused": "reverse", "toast": "reverse"}
    return {
        "prompt": "bold " + colors["primary"], "index": colors["muted"],
        "selected": f"bg:{colors['surface']} {colors['text']}", "hint": colors["muted"],
        "command.rule": colors["border"], "command.key": "bold " + colors["secondary"],
        "command.match": "bold underline", "command.description": colors["muted"],
        "status": colors["secondary"], "title": "bold " + colors["primary"],
        "thinking": colors["primary"], "thinking.dim": colors["border"],
        "border": colors["border"], "rail": colors["border"],
        "usage": colors["muted"], "usage.value": colors["secondary"],
        "context.model": colors["primary"], "context.path": colors["secondary"],
        "context.role": colors["secondary"], "context.branch": colors["muted"],
        "context.cache": colors["success"], "context.tokens": colors["secondary"],
        "context.duration": colors["secondary"], "context.workers": "bold " + colors["primary"],
        "context.model.icon": "bold " + colors["primary"], "context.state": "bold " + colors["secondary"],
        "context.progress": colors["secondary"], "context.progress.icon": "bold " + colors["border"],
        "context.meter": "bold " + colors["primary"], "context.warn": "bold " + colors["waiting"],
        "context.hot": "bold " + colors["error"], "context.label": colors["muted"],
        "context.task": "italic " + colors["secondary"],
        "workspace.brand": "bold " + colors["primary"], "workspace.session": "bold " + colors["text"],
        "workspace.state": "bold " + colors["running"], "workspace.meta": colors["muted"],
        "workspace.model": colors["secondary"],
        "toast": f"bg:{colors['surface']} {colors['text']} bold",
        "toast.success": f"bg:{colors['success']} {colors['ink']} bold",
        "toast.error": f"bg:{colors['error']} {colors['ink']} bold",
        "update.notice": f"bg:{colors['surface']} {colors['secondary']} bold",
        "selection": f"bg:{colors['surface']} {colors['text']}",
        "scrollbar.thumb": colors["primary"], "scrollbar.track": colors["muted"],
        "todo.header": "bold " + colors["text"], "todo.active": colors["primary"],
        "todo.pending": colors["text"], "todo.blocked": colors["waiting"],
        "todo.closed": colors["muted"], "todo.more": colors["muted"],
        "panel.border": colors["border"], "panel.label": "bold " + colors["primary"],
        "frame.border": colors["border"], "frame.label": "bold " + colors["primary"],
        "model.accent": colors["secondary"] + " bold", "model.live": colors["success"],
        "model.rule": colors["border"], "model.key": colors["secondary"] + " bold",
        "model.search": f"bg:{colors['surface_alt']} {colors['text']}",
        "model.row": f"bg:{colors['surface']} {colors['text']}",
        "model.tab": f"bg:{colors['secondary']} {colors['ink']} bold",
        "model.heading": f"bg:{colors['surface_alt']} {colors['muted']}",
        "model.warning": colors["waiting"], "dim": colors["muted"],
        "chosen": f"bg:{colors['surface']} {colors['text']} bold",
        "field": f"bg:{colors['ink']} {colors['text']}",
        "button.focused": f"bg:{colors['surface']} {colors['text']} bold",
        "running": colors["running"], "success": colors["success"], "error": colors["error"],
    }


def glyph(value: str | Callable[[], str], unicode_value: str, ascii_value: str) -> str:
    return ascii_value if ascii_mode(value) else unicode_value
