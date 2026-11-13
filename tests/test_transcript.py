"""缓存视口、拖选复制与持续流输出时的操作回归。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.layout.screen import Char, Screen

from trina.console_ui import EnhancedConsoleInput
from trina.terminal import ResponsiveText


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.pipe = create_pipe_input()
        self.input = self.pipe.__enter__()
        self.session = create_app_session(input=self.input, output=DummyOutput())
        self.session.__enter__()
        self.addCleanup(lambda: self.pipe.__exit__(None, None, None))
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: (), plain=True)

    def test_scrolling_does_not_reparse_or_rerender_stable_history(self):
        renders = []
        for index in range(100):
            self.editor.write(ResponsiveText(f"block {index}", lambda width, index=index:
                              renders.append(index) or (f"block {index}\n" * 20)))
        control = self.editor.transcript_control
        control.create_content(80, 20)
        self.assertEqual(len(renders), 100)
        for _ in range(1000):
            self.editor._scroll_by(3)
            content = control.create_content(80, 20)
            content.get_line(content.cursor_position.y)
            self.editor._cursor_line()
        self.assertEqual(len(renders), 100, "滚动不能重新排版历史块")
        self.editor.update_stream("new answer")
        control.create_content(80, 20)
        self.assertEqual(len(renders), 100, "新流只更新可变尾部")

    def test_plain_long_lines_are_wrapped_once_and_window_does_not_wrap_again(self):
        self.editor.write("1234567890中中")
        content = self.editor.transcript_control.create_content(6, 3)
        from prompt_toolkit.formatted_text import fragment_list_to_text
        rows = [fragment_list_to_text(content.get_line(index)) for index in range(content.line_count)]
        self.assertEqual(rows[:3], ["123456", "7890中", "中"])
        self.assertFalse(self.editor.output_window.wrap_lines())

    def test_oldest_position_has_no_dead_scroll_range(self):
        self.editor.write("\n".join(f"line {index}" for index in range(20)))
        control = self.editor.transcript_control
        control.create_content(80, 5)
        self.editor._scroll_to_oldest()
        self.assertEqual(control.viewport_start(), 0)
        self.assertEqual(self.editor._scrollback, control.max_scrollback())
        before = self.editor._scrollback
        with patch.object(self.editor.app, "invalidate") as invalidate:
            self.editor._scroll_by(100)
            invalidate.assert_not_called()
        self.assertEqual(self.editor._scrollback, before)

    def test_terminal_scroll_region_reuses_unchanged_rows(self):
        control = self.editor.transcript_control
        control.viewport_height = 4
        self.editor._rendered_lines = 10
        screen = Screen(initial_width=20, initial_height=8)
        for row, value in enumerate("ABCD"):
            screen.data_buffer[row][0] = Char(value, "")
        self.editor.app.renderer._last_screen = screen
        self.editor.output_window.render_info = SimpleNamespace(window_height=4, y_offset=0)
        with patch.object(self.editor.app.output, "write_raw") as raw:
            self.editor._scroll_by(2)
        sequence = raw.call_args.args[0]
        self.assertIn("\x1b[1;4r", sequence)
        self.assertIn("\x1b[2T", sequence)
        self.assertEqual([screen.data_buffer[row][0].char for row in range(4)], [" ", " ", "A", "B"])

    def test_deltas_are_coalesced_until_a_frame_needs_them(self):
        control = self.editor.transcript_control
        with patch("trina.terminal.render_answer", wraps=__import__("trina.terminal", fromlist=["render_answer"]).render_answer) as render:
            for index in range(100):
                self.editor.update_stream("answer " * (index + 1))
            self.assertEqual(render.call_count, 0)
            control.create_content(80, 20)
            self.assertEqual(render.call_count, 1)
            for _ in range(30):
                control.create_content(80, 20)
            self.assertEqual(render.call_count, 1)

    def test_drag_selection_copies_chinese_lines_and_keeps_input_draft(self):
        self.editor.write("第一行 abc\n第二行 xyz")
        control = self.editor.transcript_control
        control.create_content(80, 20)
        self.editor.buffer.text = "尚未发送"
        def mouse(kind, x, y):
            return MouseEvent(Point(x=x, y=y), kind, MouseButton.LEFT, frozenset())
        with patch("trina.transcript.copy_to_terminal") as clipboard:
            control.mouse_handler(mouse(MouseEventType.MOUSE_DOWN, 1, 0))
            control.mouse_handler(mouse(MouseEventType.MOUSE_MOVE, 3, 1))
            selected_row = control.get_line(0)
            self.assertTrue(any("class:selection" in style for style, _ in selected_row))
            control.mouse_handler(mouse(MouseEventType.MOUSE_UP, 3, 1))
            self.assertEqual(self.editor.app.clipboard.get_data().text, "一行 abc\n第二行")
            clipboard.assert_called_once()
        self.assertEqual(self.editor.buffer.text, "尚未发送")

    def test_selection_and_history_anchor_survive_stream_updates(self):
        self.editor.write("\n".join(f"line {i}" for i in range(30)))
        control = self.editor.transcript_control
        control.create_content(80, 20)
        self.editor._scroll_by(10)
        anchor = self.editor._cursor_line()
        control.anchor, control.head = (0, 0), (0, 6)
        text = control.selected_text()
        self.editor.update_stream("new\n" * 15)
        control.create_content(80, 20)
        self.assertEqual(self.editor._cursor_line(), anchor)
        self.assertEqual(control.selected_text(), text)

    def test_native_selection_mode_can_release_mouse_tracking(self):
        from types import SimpleNamespace
        binding = self.editor.app.key_bindings.get_bindings_for_keys(("f2",))[0]
        self.assertTrue(self.editor.app.mouse_support())
        binding.handler(SimpleNamespace())
        self.assertFalse(self.editor.app.mouse_support())
        binding.handler(SimpleNamespace())
        self.assertTrue(self.editor.app.mouse_support())

    def test_switching_chat_replaces_transcript_and_keeps_input_app(self):
        self.editor.write("old conversation\n" * 20)
        control = self.editor.transcript_control
        control.create_content(80, 20)
        self.editor.buffer.text = "draft"
        app = self.editor.app
        self.editor.clear_transcript()
        self.editor.write("restored conversation")
        content = control.create_content(80, 20)
        from prompt_toolkit.formatted_text import fragment_list_to_text
        text = "\n".join(fragment_list_to_text(content.get_line(i)) for i in range(content.line_count))
        self.assertNotIn("old conversation", text)
        self.assertIn("restored conversation", text)
        self.assertEqual(self.editor.buffer.text, "draft")
        self.assertIs(self.editor.app, app)

    def test_windows_clipboard_uses_valid_owner_and_unicode_memory(self):
        import ctypes
        from types import SimpleNamespace
        from unittest.mock import Mock
        from trina.transcript import copy_to_terminal
        user, kernel = Mock(), Mock()
        memory = ctypes.create_string_buffer(200)
        user.CreateWindowExW.return_value = 55
        user.OpenClipboard.return_value = True
        user.EmptyClipboard.return_value = True
        user.SetClipboardData.return_value = 22
        kernel.GlobalAlloc.return_value = 22
        kernel.GlobalLock.return_value = ctypes.addressof(memory)
        with patch("trina.transcript.os.name", "nt"), patch.object(ctypes, "windll", SimpleNamespace(user32=user, kernel32=kernel), create=True):
            copy_to_terminal("中文\ncopy", Mock())
        user.OpenClipboard.assert_called_once_with(55)
        user.SetClipboardData.assert_called_once_with(13, 22)
        expected = ("中文\ncopy\0").encode("utf-16-le")
        self.assertEqual(memory.raw[:len(expected)], expected)
        user.DestroyWindow.assert_called_once_with(55)

    def test_osc52_clipboard_payload_is_encoded_and_flushed(self):
        from unittest.mock import Mock
        from trina.transcript import copy_to_terminal
        output = Mock()
        with patch("trina.transcript.os.name", "posix"):
            copy_to_terminal("hello", output)
        output.write_raw.assert_called_once_with("\x1b]52;c;aGVsbG8=\x07")
        output.flush.assert_called_once()

    def test_terminal_sgr_drag_reaches_selection_without_taking_input_focus(self):
        import asyncio
        import io
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output
        output = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False)
        with create_app_session(input=self.input, output=output), patch("trina.transcript.copy_to_terminal"):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: (), plain=True)
            editor.write("alpha beta\nsecond line")
            scheduled = False
            async def exercise():
                try:
                    editor.buffer.text = "keep draft"
                    self.input.send_text("\x1b[<0;1;1M\x1b[<32;7;2M\x1b[<0;7;2m")
                    await asyncio.sleep(.15)
                    self.assertEqual(editor.app.clipboard.get_data().text, "alpha beta\nsecond")
                    self.assertEqual(editor.buffer.text, "keep draft")
                    editor.app.exit()
                except BaseException as exc:
                    editor.app.exit(exception=exc)
            def rendered(app):
                nonlocal scheduled
                if not scheduled:
                    scheduled = True
                    app.create_background_task(exercise())
            editor.app.after_render += rendered
            editor.app.run()

    def test_real_vt_wheel_uses_scroll_region_and_keeps_bottom_chrome(self):
        import asyncio
        import io
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
        with create_app_session(input=self.input, output=output):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: (), plain=True)
            editor.write("\n".join(f"history {index}" for index in range(80)))
            scheduled = False
            async def exercise():
                try:
                    before = len(stream.getvalue())
                    self.input.send_text("\x1b[<64;1;1M")
                    await asyncio.sleep(.15)
                    delta = stream.getvalue()[before:]
                    self.assertEqual(editor._scrollback, 3)
                    self.assertIn("\x1b[3T", delta)
                    self.assertIn("\x1b[r", delta)
                    self.assertEqual(editor.buffer.text, "")
                    editor.app.exit()
                except BaseException as exc:
                    editor.app.exit(exception=exc)
            def rendered(app):
                nonlocal scheduled
                if not scheduled:
                    scheduled = True
                    app.create_background_task(exercise())
            editor.app.after_render += rendered
            editor.app.run()


if __name__ == "__main__":
    unittest.main()
