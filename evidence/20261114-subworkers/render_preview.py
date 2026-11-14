"""采集真实终端屏幕缓冲；模型、任务与用量为明确标注的 UI 演示数据。"""
from __future__ import annotations

import asyncio
import io
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from prompt_toolkit import Application
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from trina.config import TrinaConfig
from trina.console_state import ConsoleContext, ConsoleState
from trina.console_ui import EnhancedConsoleInput
from trina.llm.base import LLMUsage
from trina.subworkers import Subworkers
from trina.subworker_ui import render_workers, show_subworkers

OUT = Path(__file__).resolve().parent
COLUMNS, ROWS = 144, 24


def save_screen(app, screen, name):
    cell, line, margin = 11, 28, 18
    canvas = Image.new("RGB", (COLUMNS * cell + margin * 2, ROWS * line + margin * 2), "#15111c")
    draw = ImageDraw.Draw(canvas)
    fonts = [ImageFont.truetype(path, 18) for path in ("C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/seguisym.ttf")]
    lines = []
    for y in range(ROWS):
        lines.append("".join(screen.data_buffer[y][x].char for x in range(COLUMNS)).rstrip())
        for x in range(COLUMNS):
            char = screen.data_buffer[y][x]
            attrs = app.style.get_attrs_for_style_str(char.style)
            point = (margin + x * cell, margin + y * line)
            if attrs.bgcolor and len(attrs.bgcolor) == 6:
                draw.rectangle((*point, point[0] + cell, point[1] + line), fill="#" + attrs.bgcolor)
            if char.char.strip():
                font = fonts[2] if any(0x2190 <= ord(c) <= 0x2BFF for c in char.char) else fonts[1] if any(ord(c) > 0x2FFF for c in char.char) else fonts[0]
                draw.text(point, char.char, font=font, fill="#" + attrs.color if attrs.color and len(attrs.color) == 6 else "#ede5f2")
    canvas.save(OUT / (name + ".png"))
    (OUT / (name + ".txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")


def hook(app, name):
    started = False
    async def finish():
        await asyncio.sleep(.35)
        app.exit()
    def rendered(instance):
        nonlocal started
        if not instance.is_done:
            save_screen(instance, instance.renderer.last_rendered_screen, name)
        if not started:
            started = True
            instance.create_background_task(finish())
    app.after_render += rendered
    return app


def main():
    usage = ConsoleState()
    usage.model_finished(LLMUsage(input_tokens=180000, output_tokens=8000, cache_read_tokens=144000))
    context = ConsoleContext("运行中", "DeepSeek Flash Max", "C:/work/Trina", "改进子 Worker", "轮次 8", 180000, 500000,
                             branch="main", metrics=usage.metrics(), workers="2 运行 / 1 等待")
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=ROWS, columns=COLUMNS), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(status=lambda: "运行中", running=lambda: False, dynamic_values=lambda *_: (), context=lambda: context)
        editor.show_home()
        editor.buffer.text = "/subworker"
        hook(editor.app, "footer-preview")
        editor.run(lambda _: True)
    manager = Subworkers(lambda _: None)
    manager.usage = usage
    now = time.time()
    for name, goal, model, effort, status, tokens in (
        ("WebSurface", "梳理测试应用入口，保留响应证据", "deepseek-chat", "max", "running", 58000),
        ("BinaryTrace", "分析 ELF 并验证地址偏移", "gpt-5.2", "high", "running", 42000),
        ("ReviewEvidence", "交叉核验前序结果和失败经验", "deepseek-chat", "high", "pending", 0),
        ("CodeTests", "检查修改并运行回归测试", "gpt-5.2", "high", "completed", 88000),
    ):
        row = {"id": name, "goal": goal, "model": model, "reasoning_effort": effort, "status": status}
        if status != "pending":
            row.update(tokens=tokens, calls=6, tools=9, started_at=now - 93, ended_at=now if status == "completed" else 0)
        manager._rows[name] = row
    manager._badge = "2 运行 / 1 等待 · UI 演示"
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        with patch("prompt_toolkit.Application", side_effect=lambda *args, **kwargs: hook(Application(*args, **kwargs), "subworker-preview")):
            show_subworkers(manager, TrinaConfig())
    rows = manager.rows() * 2500
    timings = {}
    for label, operation in (("footer_144_columns", lambda: context.footer(144)),
                             ("panel_10000_tasks_visible8", lambda: render_workers(rows, 144, 5000, count=8))):
        samples = []
        for _ in range(1000):
            start = time.perf_counter()
            operation()
            samples.append((time.perf_counter() - start) * 1000)
        timings[label] = {"median_ms": statistics.median(samples), "p99_ms": sorted(samples)[989], "iterations": 1000}
    (OUT / "render-performance.json").write_text(json.dumps({"scope": "本地 Python 布局函数耗时，不代表网络延迟或实际终端 FPS", "measurements": timings}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(timings))


if __name__ == "__main__":
    main()
