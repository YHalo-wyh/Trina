"""终端填充字标：斜切字形、浮雕阴影与粉紫高光，无图片依赖。"""
from __future__ import annotations


#: 上部向右错开形成斜势，半格切角和深色填充构成笔画的浮雕边缘。
_WIDE_LETTERS = (
    ("▄█████▓▄", "▀▀ █▓ ▀▀", "   █▓   ", "   █▓   ", "   █▓   ", "   █▓   ", "  ▄█▓▄  "),
    ("▄████▓▄ ", "█▓   ▀█▓", "█▓   ▄█▓", "█████▓▀ ", "█▓ ▀█▄  ", "█▓  ▀█▄ ", "█▓   ▀█▓"),
    (" ▄███▓▄ ", "   █▓   ", "   █▓   ", "   █▓   ", "   █▓   ", "   █▓   ", " ▀███▓▀ "),
    ("█▓▄   █▓", "██▓▄  █▓", "█▓▀█▄ █▓", "█▓ ▀█▄█▓", "█▓  ▀██▓", "█▓   ▀█▓", "█▓    █▓"),
    ("  ▄█▓▄  ", " ▄█▀▀█▄ ", "█▓    █▓", "█▓    █▓", "███████▓", "█▓    █▓", "█▓    █▓"),
)
_WIDE = ("       ·                  ✦                  ·",) + tuple(
    " " * ((6 - row) // 2) + "  ".join(letter[row] for letter in _WIDE_LETTERS)
    for row in range(7)
)
_COMPACT_LETTERS = (
    ("▄███▓▄", "  █▓  ", "  █▓  ", "  █▓  ", " ▄█▓▄ "),
    ("▄██▓▄ ", "█▓ ▄█▀", "███▓▀ ", "█▓ ▀█▄", "█▓  █▓"),
    ("▄███▓▄", "  █▓  ", "  █▓  ", "  █▓  ", "▀███▓▀"),
    ("█▓▄ █▓", "██▓▄█▓", "█▓▀██▓", "█▓ ▀█▓", "█▓  █▓"),
    (" ▄█▓▄ ", "█▓  █▓", "█████▓", "█▓  █▓", "█▓  █▓"),
)
_COMPACT = tuple(
    " " * ((4 - row) // 2) + " ".join(letter[row] for letter in _COMPACT_LETTERS)
    for row in range(5)
)


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def render_banner(columns: int, *, color: bool = False,
                  colors: tuple[str, str] = ("#edb2d2", "#9b7bc7")) -> str:
    """按可用列数选择字形；颜色只使用 SGR 前景，不改变字符布局。"""
    columns = max(5, columns)
    lines = next((font for font in (_WIDE, _COMPACT)
                  if max(map(len, font)) <= columns), ("TRINA",))
    width = max(map(len, lines))
    inset = " " * ((columns - width) // 2)
    if not color:
        return "\n".join(inset + line.rstrip() for line in lines)

    start, end = _rgb(colors[0]), _rgb(colors[1])
    result = []
    for row_index, line in enumerate(lines):
        row, previous = [inset], ""
        for column, character in enumerate(line):
            if character != " ":
                ratio = min(1.0, (column // 3 * 3) / max(1, width - 3))
                rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
                # 字面从上至下收暗；切角受光，侧边阴影独立压低亮度。
                light = .12 - .22 * row_index / max(1, len(lines) - 1)
                if character == "▓":
                    rgb = tuple(round(channel * .58) for channel in rgb)
                else:
                    light += .18 if character == "▀" else (.55 if character in "✦·" else 0)
                    rgb = tuple(round(channel + (255 - channel) * light) if light >= 0
                                else round(channel * (1 + light)) for channel in rgb)
                foreground = "\x1b[38;2;" + ";".join(map(str, rgb)) + "m"
                if foreground != previous:
                    previous = foreground
                    row.append(foreground)
            row.append(character)
        result.append("".join(row).rstrip() + "\x1b[0m")
    return "\n".join(result)
