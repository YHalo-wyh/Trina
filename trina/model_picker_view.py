"""模型目录的列宽、数值和按键提示，共用终端字符宽度。"""
from __future__ import annotations

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
    if width >= 80:
        fields.append(("输出", 6))
    return [("模型 ID", max(0, width - 4 - sum(n + 2 for _, n in fields))), *fields]


def padded(text: str, width: int, *, right: bool = False) -> str:
    text = _fit_cells(text, width)
    pad = " " * max(0, width - _cells(text))
    return pad + text if right else text + pad


def key_hints(pairs, width: int):
    result = []
    used = 0
    for key, action in pairs:
        size = _cells(key + " " + action) + (3 if result else 0)
        if used + size > width:
            continue
        if result:
            result.append(("", "   "))
        result.extend([("class:model.key", key), ("class:dim", " " + action)])
        used += size
    return result
