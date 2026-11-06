"""手绘终端字标：ASCII 衬线字形与粉紫前景渐变，无图片依赖。"""
from __future__ import annotations


#: 花星与尖角只由可打印字符组成，各行共享左边距，保留字形对齐。
_WIDE = r"""           .:*:.                                   .:*:.
ooooooooooooo ooooooooo.   ooooo ooooo      ooo       .o.
8'   888   `8 `888   `Y88. `888' `888b.     `8'      .888.
     888      888   .d88'  888   8 `88b.    8      .8"888.
     888      888ooo88P'   888   8   `88b.  8     .8' `888.
     888      888`88b.     888   8     `88b.8    .88ooo8888.
     888      888  `88b.   888   8       `888   .8'     `888.
    o888o    o888o  o888o o888o o8o        `8  o88o     o8888o
""".splitlines()
_COMPACT = r""" _____ ___ ___ _  _   _
|_   _| _ \_ _| \| | /_\
  | | |   /| || .` |/ _ \
  |_| |_|_\___|_|\_/_/ \_\
""".splitlines()


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
        return "\n".join(inset + line for line in lines)

    start, end = _rgb(colors[0]), _rgb(colors[1])
    # 每四列一个颜色步长，减少控制序列，同时保持从粉到紫的连续过渡。
    palette = []
    for column in range(width):
        ratio = min(1.0, (column // 4 * 4) / max(1, width - 4))
        rgb = (round(a + (b - a) * ratio) for a, b in zip(start, end))
        palette.append("\x1b[38;2;" + ";".join(map(str, rgb)) + "m")
    result = []
    for line in lines:
        row, previous = [inset], ""
        for column, character in enumerate(line):
            if character != " " and palette[column] != previous:
                previous = palette[column]
                row.append(previous)
            row.append(character)
        result.append("".join(row) + "\x1b[0m")
    return "\n".join(result)
