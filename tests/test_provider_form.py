"""实际按键驱动供应商单页表单、Key 显隐及异步测试。"""
from __future__ import annotations

import contextlib
import io
import threading
import time
import unittest

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from trina.provider_form import ProviderEditor
from trina.providers import ProviderProfile


class ProviderFormTests(unittest.TestCase):
    @contextlib.contextmanager
    def editor(self, *, url="https://service.example/v1", probe=lambda _: "地址可达", width=80):
        original = ProviderProfile("work", "custom", url, "vendor/model", api_key="test-private")
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=width), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            editor = ProviderEditor(original, is_new=True, test_connection=probe, test_model=lambda _: "模型验证通过")
            yield editor, original, pipe, stream

    def test_one_page_contains_all_fields_and_hides_key_by_default(self):
        with self.editor() as (editor, original, pipe, stream):
            pipe.send_text("\x13")
            saved = editor.run()
        self.assertEqual(saved.api_key, original.api_key)
        rendered = stream.getvalue()
        for label in ("供应商名称", "请求地址", "API Key", "模型", "备注", "连通测试", "模型验证", "保存", "取消"):
            self.assertIn(label, rendered)
        self.assertNotIn("test-private", rendered)
        self.assertIn("\x1b[?1049l", rendered)

    def test_tab_edit_and_save_returns_whole_draft_with_normalized_endpoint(self):
        with self.editor() as (editor, original, pipe, _):
            clear = "\x01\x0b"
            pipe.send_text(clear + "new supplier\t" + clear + "https://new.example/v1/responses\t"
                           + clear + "test-new\t" + clear + "vendor/new\t" + clear + "memo\x13")
            saved = editor.run()
        self.assertEqual((saved.id, saved.base_url, saved.api_key, saved.model, saved.note, saved.api),
                         ("new supplier", "https://new.example/v1", "test-new", "vendor/new", "memo", "responses"))
        self.assertEqual(original.api_key, "test-private")
        self.assertEqual(original.id, "work")

    def test_cancel_discards_all_edits(self):
        with self.editor() as (editor, original, pipe, _):
            pipe.send_text("\x01\x0bunsaved\x03")
            self.assertIsNone(editor.run())
        self.assertEqual(original.id, "work")

    def test_form_border_stretches_across_narrow_and_wide_terminals(self):
        for width in (40, 90):
            with self.editor(width=width) as (editor, _, pipe, _):
                captured = []
                def capture(_):
                    screen = editor.app.renderer.last_rendered_screen
                    if screen:
                        captured.append(screen.data_buffer[5][width - 2].char)
                editor.app.after_render += capture
                pipe.send_text("\x03")
                editor.run()
            self.assertEqual(captured[0], "│", f"{width} 列时右边框必须对齐")

    def test_key_is_visible_only_after_explicit_toggle(self):
        with self.editor() as (editor, _, pipe, stream):
            pipe.send_text("\x1bOQ")  # F2
            timer = threading.Timer(.15, lambda: pipe.send_text("\x13"))
            timer.start()
            try:
                editor.run()
            finally:
                timer.cancel()
        self.assertIn("test-private", stream.getvalue())
        self.assertTrue(editor.visible)
        self.assertIn("隐藏", stream.getvalue())

    def test_invalid_save_stays_in_form_with_error(self):
        with self.editor(url="invalid") as (editor, _, pipe, stream):
            def close_after_error(_):
                if "请求地址必须" in editor.message:
                    pipe.send_text("\x03")
            editor.app.after_render += close_after_error
            pipe.send_text("\x13")
            self.assertIsNone(editor.run())
        self.assertIn("请求地址必须", stream.getvalue())

    def test_model_candidates_are_visible_and_selectable_inside_form(self):
        with self.editor() as (editor, _, pipe, stream):
            from prompt_toolkit.completion import WordCompleter
            editor.fields["model"].buffer.completer = WordCompleter(["vendor/model-a", "vendor/model-b"], sentence=True)
            editor.app.layout.focus(editor.fields["model"])
            stage = [0]
            def drive(_):
                if stage[0] == 0:
                    stage[0] = 1
                    pipe.send_text("\x01\x0bvendor/model")
                elif stage[0] == 1 and editor.fields["model"].buffer.complete_state:
                    stage[0] = 2
                    pipe.send_text("\x1b[B\r\x13")
            editor.app.after_render += drive
            timer = threading.Timer(3, lambda: pipe.send_text("\x03"))
            timer.start()
            try:
                saved = editor.run()
            finally:
                timer.cancel()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.model, "vendor/model-a")
        self.assertIn("vendor/model-b", stream.getvalue())

    def test_async_probe_does_not_freeze_editing_or_show_stale_success(self):
        seen = []
        def probe(item):
            seen.append(item)
            time.sleep(.05)
            return "地址可达 · HTTP 200"
        with self.editor(probe=probe) as (editor, original, pipe, _):
            stage = [0]
            def drive(_):
                if stage[0] == 0:
                    stage[0] = 1
                    pipe.send_text("\x1b[15~")  # F5
                elif stage[0] == 1 and editor.busy:
                    stage[0] = 2
                    pipe.send_text("\x01\x0bchanged")
                elif stage[0] == 2 and not editor.busy:
                    stage[0] = 3
                    pipe.send_text("\x03")
            editor.app.after_render += drive
            timer = threading.Timer(3, lambda: pipe.send_text("\x03"))
            timer.start()
            try:
                self.assertIsNone(editor.run())
            finally:
                timer.cancel()
        self.assertEqual(stage[0], 3)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, "work")
        self.assertEqual(original.id, "work")
        self.assertEqual(editor.message, "配置已修改，请重新测试")


if __name__ == "__main__":
    unittest.main()
