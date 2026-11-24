"""终端目录的列宽、标题和按键提示。"""
from __future__ import annotations

from re import Pattern

from .console_state import _cells, _fit_cells


def tokens(value: int) -> str:
    if value <= 0:
        return "—"
    for unit, size in (("M", 1_000_000), ("K", 1_000)):
        if value >= size:
            return f"{value / size:.1f}".removesuffix(".0") + unit
    return str(value)


def columns(width: int) -> list[tuple[str, int]]:
    fields = []
    if width >= 60:
        fields.append(("连接", 16 if width >= 84 else 12))
    if width >= 50:
        fields.append(("类型", 9))
    if width >= 35:
        fields.append(("窗口", 6))
    if width >= 76:
        fields.append(("输出", 6))
    return [("模型 ID", max(0, width - 4 - sum(n + 2 for _, n in fields))), *fields]


def padded(text: str, width: int, *, right: bool = False) -> str:
    text = _fit_cells(text, width)
    pad = " " * max(0, width - _cells(text))
    return pad + text if right else text + pad


def directory_header(icon: str, title: str, context: str, width: int, *, style: str = "class:dim"):
    left = _fit_cells(f"{icon}  {title}", width)
    right = _fit_cells(context, width - _cells(left) - 3)
    return [("class:model.accent", left),
            ("", " " * max(0, width - _cells(left + right))),
            (style, right)]


def marked_text(text: str, pattern: Pattern[str] | None, style: str, mark_style: str):
    """在已截断的显示文本上标出搜索命中，保持列宽。"""
    fragments = []
    start = 0
    for match in pattern.finditer(text) if pattern else ():
        fragments.extend([(style, text[start:match.start()]), (f"{style} {mark_style}", match.group())])
        start = match.end()
    fragments.append((style, text[start:]))
    return fragments


def key_hints(pairs, width: int, *, key_style: str = "class:model.key", label_style: str = "class:dim"):
    result = []
    used = 0
    for key, action in pairs:
        size = _cells(key + " " + action) + (3 if result else 0)
        if used + size > width:
            continue
        if result:
            result.append(("", "   "))
        result.extend([(key_style, key), (label_style, " " + action)])
        used += size
    return result
