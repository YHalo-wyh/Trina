"""固定底栏、跨模型会话用量、思考状态及真实按键回归。"""
from __future__ import annotations

import asyncio
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trina.console_state import ConsoleState, TrackedBackend
from trina.llm.base import LLMResponse, LLMStreamEvent, LLMUsage


class ConsoleSessionTests(unittest.TestCase):
    def test_session_accumulates_across_backends_and_resets_without_reusing_their_ledgers(self):
        view = ConsoleState()
        for incoming, outgoing, cached in ((1000, 200, 600), (500, 100, 300)):
            backend = SimpleNamespace(complete=lambda *_, i=incoming, o=outgoing, c=cached:
                LLMResponse(text="answer", usage=LLMUsage(input_tokens=i, output_tokens=o, cache_read_tokens=c)))
            TrackedBackend(backend, view).complete("s", "u")
        data = view.usage_data()
        self.assertEqual((data["calls"], data["total_tokens"]), (2, 1800))
        self.assertEqual(view.context_tokens, 500)
        self.assertEqual(data["cache_hit_rate"], .6)
        self.assertIn("60.0%", view.footer(88))
        view.reset()
        self.assertEqual(view.usage_data()["calls"], 0)
        self.assertEqual(view.usage_data()["total_tokens"], 0)
        self.assertEqual(view.footer(88), "")

    def test_missing_usage_is_not_zero_percent_and_partial_rates_use_only_known_input(self):
        view = ConsoleState()
        view.model_finished(LLMUsage(input_tokens=1000, output_tokens=100))
        self.assertNotIn("缓存命中", view.footer(88))
        self.assertNotIn("未提供", view.footer(88))
        view.model_finished(LLMUsage(input_tokens=100, cache_read_tokens=80))
        self.assertAlmostEqual(view.usage_data()["cache_hit_rate"], .8)
        self.assertIn("80.0%（部分）", view.footer(88))
        view.model_finished(None)
        self.assertIn("≥", view.footer(88))
        empty = ConsoleState()
        empty.model_finished(LLMUsage())
        self.assertEqual(empty.footer(88), "")
        view.model_finished(LLMUsage(input_tokens=10, cost=.1))
        self.assertIn("费用 ≥$0.1000", view.footer(120))

    def test_footer_before_any_call_is_hidden(self):
        self.assertEqual(ConsoleState().footer(88), "")

    def test_individual_missing_fields_are_not_rendered_as_zero(self):
        input_only = ConsoleState()
        input_only.model_finished(LLMUsage(input_tokens=25, input_reported=True))
        self.assertIn("入 25", input_only.footer(88))
        self.assertNotIn("出", input_only.footer(88))
        self.assertNotIn("未提供", input_only.footer(88))
        cache_write_only = ConsoleState()
        cache_write_only.model_finished(LLMUsage(input_tokens=25, cache_write_tokens=5,
                                                 input_reported=True, cache_write_reported=True))
        footer = cache_write_only.footer(100)
        self.assertNotIn("缓存命中", footer)
        self.assertIn("缓存写 5", footer)
        self.assertNotIn("缓存读", footer)
        self.assertNotIn("未提供", footer)

    def test_request_phase_covers_real_wait_and_recovers_after_an_exception(self):
        view = ConsoleState()
        entered, release = threading.Event(), threading.Event()
        def complete(*_):
            entered.set()
            release.wait(2)
            return LLMResponse(text="answer", usage=LLMUsage(input_tokens=10))
        backend = TrackedBackend(SimpleNamespace(complete=complete), view)
        worker = threading.Thread(target=lambda: backend.complete("s", "u"))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(view.activity()[0], "waiting")
        release.set()
        worker.join(2)
        self.assertEqual(view.activity()[0], "processing")
        failed = TrackedBackend(SimpleNamespace(complete=lambda *_: (_ for _ in ()).throw(ValueError("failure"))), view)
        with self.assertRaises(ValueError):
            failed.complete("s", "u")
        self.assertNotEqual(view.activity()[0], "thinking")
        self.assertEqual(view.pending, 0)
        self.assertEqual(view.usage_data()["calls"], 2)

    def test_stream_activity_distinguishes_waiting_reasoning_and_reply(self):
        view, seen, phases = ConsoleState(), [], []
        def complete(_messages, **options):
            phases.append(view.activity()[0])
            for kind in ("start", "thinking_delta", "text_delta"):
                options["on_event"](LLMStreamEvent(kind, delta="内容"))
                phases.append(view.activity()[0])
            return LLMResponse(text="回答")
        backend = TrackedBackend(SimpleNamespace(complete_messages=complete), view)
        backend.complete_messages([{"role": "user", "content": "你好"}], on_event=seen.append)
        self.assertEqual(phases, ["waiting", "waiting", "thinking", "responding"])
        self.assertEqual([event.type for event in seen], ["start", "thinking_delta", "text_delta"])
        self.assertEqual(view.pending, 0)
        self.assertEqual(view.usage_data()["calls"], 1)

    def test_usage_fits_narrow_and_short_terminals(self):
        from rich.cells import cell_len
        view = ConsoleState()
        view.model_finished(LLMUsage(input_tokens=1234567, output_tokens=100200, cache_read_tokens=1000000,
                                    cache_write_tokens=20000, cost=.0123))
        for width in (20, 40, 88, 120):
            for compact in (False, True):
                self.assertLessEqual(max(cell_len(line) for line in view.footer(width, compact).splitlines()), width)
                self.assertIn("缓存命中", view.footer(width, compact))

    def test_reply_has_no_automatic_digest_and_thought_summary_has_a_separate_style(self):
        from trina.terminal import render_answer, render_process, clean_text
        answer = render_answer("## 结果\n\n完成。\n\n```python\nprint('ok')\n```", width=60, color=True)
        self.assertNotIn("[Trina] 输出", clean_text(answer))
        self.assertIn("完成。", clean_text(answer))
        for unwanted in ("会话 chat-", "轮次", "目标 未指定", "假设 待验证"):
            self.assertNotIn(unwanted, answer)
        process = render_process("检查返回的登录状态", label="思考摘要", width=40, color=True)
        self.assertIn("思考摘要", clean_text(process))
        self.assertNotIn("┊", clean_text(process))
        self.assertNotIn("[Trina] 输出", process)

    def test_bottom_stays_fixed_during_output_and_enter_keeps_the_application_alive(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.console_ui import EnhancedConsoleInput
        for columns, rows in ((88, 24), (40, 20), (20, 12)):
            with self.subTest(columns=columns, rows=rows):
                stream, frames, submitted = io.StringIO(), [], []
                output = Vt100_Output(stream, lambda: Size(rows=rows, columns=columns), enable_cpr=False)
                view = ConsoleState()
                view.model_finished(LLMUsage(input_tokens=100, output_tokens=20, cache_read_tokens=60))
                with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
                    editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True,
                        dynamic_values=lambda *_: (), usage=view.footer, activity=lambda: ("thinking", "思考中", 1), plain=True)
                    started = False
                    async def exercise():
                        try:
                            await asyncio.sleep(.05)
                            editor.buffer.text = "继续"
                            editor.write("\n".join(f"output {i}" for i in range(80)))
                            await asyncio.sleep(.25)
                            self.assertEqual(editor.buffer.text, "继续")
                            pipe.send_text("\r")
                            await asyncio.sleep(.15)
                            self.assertTrue(editor.app.is_running)
                            pipe.send_text("/invalid\r")
                            await asyncio.sleep(.1)
                            self.assertTrue(editor.app.is_running)
                            pipe.send_text("/quit\r")
                        except BaseException as exc:
                            if editor.app.is_running:
                                editor.app.exit(exception=exc)
                    def rendered(app):
                        nonlocal started
                        if not app.is_done:
                            screen = app.renderer.last_rendered_screen
                            lines = ["".join(screen.data_buffer[y][x].char for x in range(columns)) for y in range(rows)]
                            frames.append(lines)
                        if not started:
                            started = True
                            app.create_background_task(exercise())
                    editor.app.after_render += rendered
                    def submit(value):
                        submitted.append(value)
                        if value == "/invalid":
                            raise SystemExit(2)
                        return value != "/quit"
                    editor.run(submit)
                self.assertEqual(submitted, ["继续", "/invalid", "/quit"])
                self.assertGreaterEqual(len(frames), 2)
                footer_positions = []
                for lines in frames:
                    matches = [i for i, line in enumerate(lines) if "会话 Token" in line]
                    if matches:
                        footer_positions.append(matches[0])
                        self.assertTrue(any("缓存命中" in line for line in lines[matches[0]:]))
                self.assertTrue(footer_positions, frames)
                self.assertEqual(len(set(footer_positions)), 1)
                self.assertIn("\x1b[?1049h", stream.getvalue())
                self.assertIn("\x1b[?1049l", stream.getvalue())

    def test_real_console_keeps_draft_usage_and_footer_when_model_dialog_returns(self):
        import tempfile
        from pathlib import Path
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.config import TrinaConfig
        from trina.console_ui import EnhancedConsoleInput, ChoiceSpec, select_option
        from trina.interactive import Console
        from trina.brain.chat import ChatAgent
        from trina.tools import build_registry
        entered, release, model_opened, model_closed = (threading.Event() for _ in range(4))
        def complete(*_):
            entered.set()
            release.wait(3)
            return LLMResponse(text="正式回答", usage=LLMUsage(input_tokens=100, output_tokens=20, cache_read_tokens=60))
        def model_dialog():
            model_opened.set()
            self.assertEqual(select_option("Model", [ChoiceSpec("back", "返回")]), "back")
            model_closed.set()
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=26, columns=88), enable_cpr=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TRINA_PROVIDERS_FILE": str(Path(directory) / "providers.json")}):
            config = TrinaConfig.load().with_overrides(policy__mode="free", run__workspace=str(Path(directory) / "runs"), audit__dir=str(Path(directory) / "logs"))
            console = Console(config, backend=SimpleNamespace(complete=complete), registry=build_registry(), plain=True, autostart_lab=False)
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
                def editor_factory(**kwargs):
                    editor = EnhancedConsoleInput(**kwargs)
                    started = False
                    async def until(predicate):
                        for _ in range(1000):
                            if predicate():
                                return
                            await asyncio.sleep(.01)
                        raise AssertionError("UI operation timed out: " + "".join(editor._chunks)[-800:])
                    async def exercise():
                        try:
                            await asyncio.sleep(.05)
                            pipe.send_text("hello\r")
                            await until(entered.is_set)
                            self.assertEqual(console._activity()[0], "waiting")
                            self.assertEqual(console._footer_context().task, "hello")
                            self.assertEqual(console._footer_context().directory, str(Path.cwd()))
                            editor.buffer.text = "保留未发送补充"
                            release.set()
                            await until(lambda: not console.session.running)
                            self.assertEqual(editor.buffer.text, "保留未发送补充")
                            self.assertEqual(console.view.usage_data()["total_tokens"], 120)
                            self.assertIn("正式回答", "".join(editor._chunks))
                            self.assertNotIn("轮次", "".join(editor._chunks))
                            self.assertNotIn("目标 未指定", "".join(editor._chunks))
                            editor.buffer.reset()
                            pipe.send_text("/model\r")
                            await until(model_opened.is_set)
                            pipe.send_text("\r")
                            await until(model_closed.is_set)
                            await until(lambda: not editor.app._running_in_terminal)
                            self.assertTrue(editor.app.is_running)
                            self.assertEqual(console.view.usage_data()["total_tokens"], 120)
                            pipe.send_text("/new\r")
                            await until(lambda: console.view.usage_data()["calls"] == 0)
                            self.assertEqual(console._footer_context().task, "新对话")
                            pipe.send_text("/quit\r")
                        except BaseException as exc:
                            release.set()
                            pipe.send_text("\x03")
                            if editor.app.is_running:
                                editor.app.exit(exception=exc)
                    def rendered(app):
                        nonlocal started
                        if not started:
                            started = True
                            app.create_background_task(exercise())
                    editor.app.after_render += rendered
                    return editor
                with patch("trina.interactive.enhanced_input_available", return_value=True), patch("trina.interactive.EnhancedConsoleInput", side_effect=editor_factory), patch.object(console, "_model_manager", return_value=SimpleNamespace(run=model_dialog)):
                    self.assertEqual(console.run(), 0)
                self.assertTrue(model_closed.is_set())

    def test_thinking_marker_moves_but_idle_does_not(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        phase = ["thinking"]
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True,
                dynamic_values=lambda *_: (), activity=lambda: (phase[0], "state", 1))
            with patch("trina.console_ui.time.monotonic", return_value=0):
                first = editor._activity()
            with patch("trina.console_ui.time.monotonic", return_value=.5):
                second = editor._activity()
            self.assertNotEqual(first, second)
            phase[0] = "idle"
            with patch("trina.console_ui.time.monotonic", return_value=.25):
                self.assertEqual(editor._activity(), editor._activity())


    def test_context_footer_keeps_model_directory_task_and_draft_at_different_widths(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from rich.cells import cell_len
        from trina.console_state import ConsoleContext
        from trina.console_ui import EnhancedConsoleInput
        for columns in (170, 88, 40, 20):
            with self.subTest(columns=columns):
                frames, stream = [], io.StringIO()
                context = [ConsoleContext("运行中", "DeepSeek V4.1 Flash Max",
                                          "C:/Users/WYH/Desktop/网安实训中级", "修复会话日志",
                                          "步骤 3/12", 125_000, 500_000)]
                output = Vt100_Output(stream, lambda: Size(rows=30, columns=columns), enable_cpr=False)
                with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
                    editor = EnhancedConsoleInput(status=lambda: "running", running=lambda: True,
                        dynamic_values=lambda *_: (), activity=lambda: ("thinking", "思考中", 20),
                        context=lambda: context[0], plain=True)
                    editor.buffer.text = "保留草稿"
                    started = False
                    async def exercise():
                        try:
                            await asyncio.sleep(.08)
                            context[0] = ConsoleContext("运行中", "GPT-5.2 High", context[0].directory,
                                                        "检查事件流", "步骤 4/12", 150_000, 500_000)
                            editor.write("事件输出")
                            await asyncio.sleep(.12)
                            self.assertEqual(editor.buffer.text, "保留草稿")
                            footer = "".join(text for _, text in editor._status())
                            rail = "".join(text for _, text in editor._rail())
                            activity = "".join(text for _, text in editor._activity())
                            self.assertNotIn("\n", activity)
                            self.assertEqual(editor._activity_height(), 1)
                            self.assertEqual(editor._hint(), "")
                            self.assertLessEqual(max(cell_len(line) for line in footer.splitlines()), columns)
                            self.assertLessEqual(cell_len(rail), columns)
                            self.assertLessEqual(max(cell_len(line) for line in activity.splitlines()), columns)
                            editor.app.exit()
                        except BaseException as exc:
                            editor.app.exit(exception=exc)
                    def rendered(app):
                        nonlocal started
                        if not app.is_done:
                            screen = app.renderer.last_rendered_screen
                            frames.append("\n".join("".join(screen.data_buffer[y][x].char for x in range(columns)) for y in range(30)))
                        if not started:
                            started = True
                            app.create_background_task(exercise())
                    editor.app.after_render += rendered
                    editor.run(lambda _: True)
                visible = "\n".join(frames)
                for expected in ("DeepSeek", "GPT-5.2 High", "📁", "网安实训中级",
                                 "修复会话日志" if columns >= 40 else "修复…",
                                 "检查事件流" if columns >= 40 else "检查…"):
                    self.assertIn(expected, visible)
                self.assertNotIn("Enter 发送补充", visible)
                self.assertNotIn("已上翻历史", visible)

    def test_session_title_contract_uses_short_goal_and_safe_fallback(self):
        from trina.session_title import fallback_session_title, generate_session_title, parse_generated_title

        self.assertEqual(parse_generated_title("<title>修复损坏的会话日志</title>"), "修复损坏的会话日志")
        self.assertIsNone(parse_generated_title("解释文字，没有标记"))
        self.assertIsNone(parse_generated_title("<title/>"))
        self.assertEqual(fallback_session_title("你好"), "新对话")
        fallback = fallback_session_title("请帮我修复 corrupt session log。然后补回归测试")
        self.assertEqual(fallback, "修复 corrupt session log")
        self.assertLessEqual(len(fallback), 32)
        requests = []
        backend = SimpleNamespace(complete_messages=lambda messages, **kwargs:
                                  requests.append((messages, kwargs)) or
                                  LLMResponse(text="<title>修复损坏的会话日志</title>"))
        title, response = generate_session_title("修复损坏的会话日志", backend)
        self.assertEqual(title, "修复损坏的会话日志")
        self.assertTrue(response.ok)
        self.assertEqual(requests[0][1]["max_tokens"], 96)
        self.assertIn("3-6", requests[0][0][0]["content"])


if __name__ == "__main__":
    unittest.main()
