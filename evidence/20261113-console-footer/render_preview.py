"""将实际 prompt_toolkit 屏幕缓冲导出为底栏预览，模型和任务使用演示值。"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from trina.console_state import ConsoleContext
from trina.console_ui import EnhancedConsoleInput


def main() -> None:
    columns, rows = 144, 14
    frames = []
    context = ConsoleContext("运行中", "DeepSeek V4.1 Flash Max", str(ROOT),
                             "检查模型请求与错误回显", "步骤 4/12", 247_000, 500_000)
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=rows, columns=columns), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(status=lambda: "运行中", running=lambda: True, dynamic_values=lambda *_: (),
                                      activity=lambda: ("waiting", "等待输出", 23.4), context=lambda: context)
        editor.buffer.text = "继续检查显示效果"
        from trina.terminal import render_message
        editor.write(render_message("你好", label="你", color=True))
        editor.write(render_message("权限或授权拒绝 · HTTP 403: forbidden（错误展示演示）", label="错误", color=True))
        async def finish():
            await asyncio.sleep(.1)
            editor.app.exit()
        started = False
        def rendered(app):
            nonlocal started
            if not app.is_done:
                frames.append(app.renderer.last_rendered_screen)
            if not started:
                started = True
                app.create_background_task(finish())
        editor.app.after_render += rendered
        editor.run(lambda _: True)
    screen = frames[-1]
    first = rows - 4
    cell, line, margin = 11, 30, 18
    canvas = Image.new("RGB", (columns * cell + margin * 2, 4 * line + margin * 2), "#101012")
    draw = ImageDraw.Draw(canvas)
    latin = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 19)
    chinese = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    symbols = ImageFont.truetype("C:/Windows/Fonts/seguisym.ttf", 19)
    emoji = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 19)
    lines = []
    for y in range(first, rows):
        lines.append("".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip())
        for x in range(columns):
            char = screen.data_buffer[y][x]
            if not char.char.strip():
                continue
            attrs = editor.app.style.get_attrs_for_style_str(char.style)
            color = "#" + attrs.color if attrs.color and len(attrs.color) == 6 else "#d8d8dc"
            font = emoji if any(ord(c) >= 0x1F000 for c in char.char) else symbols if any(0x2190 <= ord(c) <= 0x2BFF for c in char.char) else (
                chinese if any(ord(c) > 127 for c in char.char) else latin)
            draw.text((margin + x * cell, margin + (y - first) * line), char.char, font=font, fill=color,
                      embedded_color=font is emoji)
    destination = Path(__file__).resolve().parent
    canvas.save(destination / "footer-preview.png")
    (destination / "footer-preview.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "columns": columns, "source": "prompt_toolkit 实际屏幕缓冲",
                      "preview": str(destination / "footer-preview.png")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
