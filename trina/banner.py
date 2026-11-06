"""启动字标：使用随包原图，按终端能力输出图片或彩色字符。"""
from __future__ import annotations

import base64
import io
import math
import os
import shutil
from functools import lru_cache
from importlib.resources import files
from itertools import groupby


@lru_cache(maxsize=1)
def logo_bytes() -> bytes:
    return files("trina").joinpath("assets/trina-logo.png").read_bytes()


@lru_cache(maxsize=1)
def _original():
    from PIL import Image
    return Image.open(io.BytesIO(logo_bytes())).convert("RGB")


def layout(columns: int, lines: int | None = None) -> tuple[int, int, int]:
    image = _original()
    max_rows = max(2, min(18, (lines or shutil.get_terminal_size((88, 24)).lines) - 6))
    columns = max(1, columns)
    cells = max(1, min(columns - 4 if columns >= 8 else columns,
                       int(max_rows * 2 * image.width / image.height)))
    rows = math.ceil(cells * image.height / image.width / 2)
    return cells, rows, (columns - cells) // 2


def _scaled(columns: int, lines: int | None = None):
    from PIL import Image
    cells, rows, inset = layout(columns, lines)
    image = _original().resize((cells, rows * 2), Image.Resampling.LANCZOS)
    return image, inset


def render_blocks(columns: int, lines: int | None = None) -> str:
    """普通真彩色终端以双半格保留原图的配色和构图。"""
    image, inset = _scaled(columns, lines)
    pixels = image.load()
    result = []
    for y in range(0, image.height, 2):
        row = [" " * inset]
        for x in range(image.width):
            top, bottom = pixels[x, y], pixels[x, y + 1]
            row.append(f"\x1b[38;2;{bottom[0]};{bottom[1]};{bottom[2]}m"
                       f"\x1b[48;2;{top[0]};{top[1]};{top[2]}m▄")
        result.append("".join(row) + "\x1b[0m")
    return "\n".join(result)


def native_protocol() -> str | None:
    mode = os.environ.get("TRINA_LOGO_MODE", "auto").lower()
    if mode != "auto":
        return mode if mode in {"sixel", "iterm", "kitty"} else None
    program, term = os.environ.get("TERM_PROGRAM", ""), os.environ.get("TERM", "")
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in term:
        return "kitty"
    if program in {"iTerm.app", "WezTerm"} or os.environ.get("WEZTERM_PANE"):
        return "iterm"
    if os.environ.get("WT_SESSION") or "sixel" in term or program == "mintty":
        return "sixel"
    return None


def _rle(value: str) -> str:
    result = []
    for character, group in groupby(value):
        count = sum(1 for _ in group)
        result.append(f"!{count}{character}" if count > 3 else character * count)
    return "".join(result)


def _sixel(columns: int, lines: int | None) -> str:
    from PIL import Image
    cells, rows, _ = layout(columns, lines)
    original = _original()
    image = original.resize((cells * 8, round(cells * 8 * original.height / original.width)), Image.Resampling.LANCZOS)
    image = image.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    palette, pixels = image.getpalette(), image.load()
    output = [f'\x1bP0;1;0q"1;1;{image.width};{image.height}']
    for index in range(64):
        rgb = palette[index * 3:index * 3 + 3]
        output.append(f"#{index};2;" + ";".join(str(round(channel * 100 / 255)) for channel in rgb))
    for y in range(0, image.height, 6):
        colors: dict[int, list[int]] = {}
        for bit in range(min(6, image.height - y)):
            for x in range(image.width):
                index = pixels[x, y + bit]
                if index not in colors:
                    colors[index] = [0] * image.width
                colors[index][x] |= 1 << bit
        for index, masks in colors.items():
            output.append(f"#{index}" + _rle("".join(chr(63 + mask) for mask in masks).rstrip("?")) + "$")
        if y + 6 < image.height:
            output.append("-")
    output.append("\x1b\\")
    return "".join(output)


def render_native(columns: int, protocol: str, lines: int | None = None) -> str:
    """先预留行数，再绘图并恢复到版本号所在行，不读取终端输入。"""
    cells, rows, inset = layout(columns, lines)
    data = base64.b64encode(logo_bytes()).decode("ascii")
    if protocol == "sixel":
        payload = _sixel(columns, lines)
    elif protocol == "iterm":
        payload = f"\x1b]1337;File=inline=1;width={cells};height={rows};preserveAspectRatio=1:{data}\x07"
    elif protocol == "kitty":
        chunks = [data[index:index + 4096] for index in range(0, len(data), 4096)]
        payload = "".join("\x1b_G" + (f"a=T,f=100,t=d,q=2,c={cells},r={rows}," if index == 0 else "")
                          + f"m={int(index < len(chunks) - 1)};{chunk}\x1b\\" for index, chunk in enumerate(chunks))
    else:
        raise ValueError("未知终端图片协议")
    return "\n" * rows + f"\x1b[{rows}A\r\x1b7\x1b[{inset + 1}G" + payload + f"\x1b8\x1b[{rows}B\r"
