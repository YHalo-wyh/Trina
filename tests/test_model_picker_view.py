"""模型目录在真实 VT 布局中的列宽、筛选可见性与快捷键。"""
from __future__ import annotations

import asyncio
import io
import unittest
from unittest.mock import patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.utils import get_cwidth

from trina import console_ui
from trina.model_picker import ModelChoice, select_model
from trina.models import CatalogModel


class ModelDirectoryViewTests(unittest.TestCase):
    def test_narrow_directory_shows_connection_list_and_returns_selected_preset(self):
        from trina.providers import PRESET_BY_ID
        output = Vt100_Output(io.StringIO(), lambda: Size(rows=12, columns=38), enable_cpr=False)
        preset = PRESET_BY_ID["ollama"]
        choices = [ModelChoice("workspace", "Workspace", "chat-test", current=True)]
        factory = console_ui._panel_app

        def panel(*args, **kwargs):
            app = factory(*args, **kwargs)
            frames = 0

            def ready(_):
                nonlocal frames
                frames += 1
                if frames == 1:
                    pipe.send_text("\x1b[D\x1b[B\x1b[B")
                elif frames == 2:
                    app.after_render -= ready
                    try:
                        screen = app.renderer.last_rendered_screen
                        text = "\n".join("".join(cell.char for _, cell in sorted(row.items()))
                                         for row in screen.data_buffer.values())
                        self.assertIn("ollama", text)
                        self.assertIn("Workspace", text)
                        self.assertIn("未连接", text)
                        pipe.send_text("\r")
                    except AssertionError as exc:
                        app.exit(exception=exc)

            app.after_render += ready
            return app

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output), patch.object(console_ui, "_panel_app", panel):
            self.assertEqual(select_model(choices, plain=True, presets=(preset,)), preset)

    def test_resize_keeps_filter_visible_and_help_returns_to_selection(self):
        output_size = [Size(rows=22, columns=116)]
        output = Vt100_Output(io.StringIO(), lambda: output_size[0], enable_cpr=False)
        choices = [ModelChoice("a", "中文连接", "chat-current", current=True, meta=CatalogModel("chat", 1_000_000)),
                   ModelChoice("b", "work", "vector-alpha", meta=CatalogModel("embedding", 8192)),
                   ModelChoice("b", "work", "chat-recent", meta=CatalogModel("chat", 32_768))]
        factory = console_ui._panel_app

        def panel(*args, **kwargs):
            app = factory(*args, **kwargs)

            def lines():
                screen = app.renderer.last_rendered_screen
                return ["".join(row[x].char for x in range(output_size[0].columns)).rstrip()
                        for _, row in sorted(screen.data_buffer.items())]

            async def until(text):
                for _ in range(100):
                    if any(text in line for line in lines()):
                        return
                    await asyncio.sleep(.01)
                self.fail(f"界面缺少 {text!r}，实际内容为 {lines()}")

            async def exercise():
                try:
                    header = next(line for line in lines() if "模型 ID" in line and "类型" in line)
                    row = next(line for line in lines() if "vector-alpha" in line and "embedding" in line)
                    self.assertEqual(get_cwidth(header.split("类型")[0]), get_cwidth(row.split("embedding")[0]))
                    rendered = "\n".join(lines())
                    self.assertLess(rendered.index("chat-recent"), rendered.index("其他模型"))
                    self.assertLess(rendered.index("其他模型"), rendered.index("vector-alpha"))
                    output_size[0] = Size(rows=12, columns=34)
                    pipe.send_text("\x1b\x1b[D")
                    await until("embedding")
                    self.assertFalse(any("chat-current" in line and "●" in line and "模型目录" not in line
                                         for line in lines()))
                    pipe.send_text("\x1bOP")
                    await until("轮换连接")
                    pipe.send_text("\x1b")
                    for _ in range(100):
                        if not any("轮换连接" in line for line in lines()):
                            break
                        await asyncio.sleep(.01)
                    self.assertFalse(any("轮换连接" in line for line in lines()))
                    self.assertTrue(any("embedding" in line for line in lines()))
                    pipe.send_text("\x1b\x1b[C\r")
                except BaseException as exc:
                    if not app.is_done:
                        app.exit(exception=exc)

            def ready(_):
                app.after_render -= ready
                app.create_background_task(exercise())

            app.after_render += ready
            return app

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output), patch.object(console_ui, "_panel_app", panel):
            self.assertEqual(select_model(choices, plain=True, recent=[("b", "chat-recent")]), choices[0])
