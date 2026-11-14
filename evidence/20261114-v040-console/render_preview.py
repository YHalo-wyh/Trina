"""将真实 prompt_toolkit 屏幕缓冲渲染为预览；模型、角色及用量为演示数据。"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from trina.console_state import ConsoleContext, ConsoleState
from trina.console_ui import EnhancedConsoleInput
from trina.llm.base import LLMUsage


def main():
    columns, rows = 100, 19
    context = ConsoleContext("就绪", "DeepSeek V4.1 Flash Max · max", "C:/work/Trina", "优化控制台体验",
                             "轮次 3", 180_000, 500_000, role="Code Worker", branch="main")
    view = ConsoleState()
    view.model_finished(LLMUsage(input_tokens=180_000, output_tokens=8_000, cache_read_tokens=144_000))
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=rows, columns=columns), enable_cpr=False)
    frames = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(status=lambda: "就绪", running=lambda: False, dynamic_values=lambda *_: (),
                                      context=lambda: context, usage=view.footer, activity=lambda: ("idle", "就绪", 0))
        editor.show_home()
        editor.buffer.text = "检查代码并运行测试"
        async def finish():
            await asyncio.sleep(.4)
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
    cell, line, margin = 11, 29, 20
    canvas = Image.new("RGB", (columns * cell + margin * 2, rows * line + margin * 2), "#15111c")
    draw = ImageDraw.Draw(canvas)
    fonts = [ImageFont.truetype(path, 18) for path in
             ("C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/seguiemj.ttf")]
    lines = []
    for y in range(rows):
        lines.append("".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip())
        for x in range(columns):
            char = screen.data_buffer[y][x]
            attrs = editor.app.style.get_attrs_for_style_str(char.style)
            if attrs.bgcolor and len(attrs.bgcolor) == 6:
                draw.rectangle((margin + x * cell, margin + y * line, margin + (x + 1) * cell, margin + (y + 1) * line), fill="#" + attrs.bgcolor)
            if not char.char.strip():
                continue
            font = fonts[3] if any(ord(c) >= 0x1F000 for c in char.char) else fonts[2] if any(0x2190 <= ord(c) <= 0x2BFF for c in char.char) else fonts[1] if any(ord(c) > 0x2FFF for c in char.char) else fonts[0]
            color = "#" + attrs.color if attrs.color and len(attrs.color) == 6 else "#ede5f2"
            draw.text((margin + x * cell, margin + y * line), char.char, font=font, fill=color)
    directory = Path(__file__).resolve().parent
    canvas.save(directory / "console-preview.png")
    (directory / "console-preview.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(directory / "console-preview.png")


if __name__ == "__main__":
    main()
