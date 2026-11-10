"""终端可读性和交互回归：宽度、无色输出、控制序列、状态选择与审批。"""
from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.board import render_board, summarise
from trina.brain.agent import StepRecord
from trina.config import TrinaConfig
from trina.console_ui import SlashCommandCompleter, command_hint, command_names
from trina.interactive import Console
from trina.state import RunState
from trina.terminal import clean_text, render_approval, render_home, render_message, render_panel, render_step, use_color


class TestTerminalUI(unittest.TestCase):
    def console(self) -> tuple[Console, list[str]]:
        output = []
        return Console(TrinaConfig.load(), output=output.append, autostart_lab=False), output

    def test_home_and_long_board_fit_narrow_and_wide_terminals(self):
        from rich.cells import cell_len
        state = RunState(run_id="展示", target="http://127.0.0.1/" + "中文路径" * 35)
        state.artifacts["workspace"] = "C:/课程项目/" + "very-long-directory/" * 10
        for width in (20, 40, 64, 88, 120):
            from rich.text import Text
            for color in (False, True):
                for text in (render_home(width=width, color=color), render_board(state, width=width, color=color)):
                    with self.subTest(width=width, color=color):
                        plain = Text.from_ansi(text).plain
                        self.assertLessEqual(max(cell_len(line) for line in plain.splitlines()), width)
                        if not color:
                            self.assertNotIn("\x1b", text)
        self.assertEqual(render_home(width=40).splitlines()[-1].strip(), "v0.2.0")

    def test_slash_index_filters_commands_and_lists_options(self):
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter(running=lambda: False)

        def values(text: str) -> list[str]:
            return [item.text for item in completer.get_completions(Document(text), None)]

        root = values("/")
        self.assertIn("play", root)
        self.assertIn("model", root)
        self.assertNotIn("pause", root, "空闲时不应把仅运行态命令放在首屏")
        self.assertEqual(values("/sta"), ["status"])
        self.assertIn("--goal", values("/play --"))
        self.assertIn("verify", values("/approve v"))

    def test_running_slash_index_is_context_aware(self):
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter(running=lambda: True)
        values = [item.text for item in completer.get_completions(Document("/"), None)]
        self.assertIn("pause", values)
        self.assertIn("stop", values)
        self.assertIn("status", values)
        self.assertNotIn("play", values)

    def test_enhanced_input_can_be_built_in_a_terminal_session(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from trina.console_ui import EnhancedConsoleInput

        with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
            editor = EnhancedConsoleInput(
                status=lambda: "就绪", running=lambda: False,
                dynamic_values=lambda _command, _args: (), plain=True,
            )
        self.assertIsNotNone(editor.buffer.completer)
        self.assertTrue(editor.app.full_screen)

    def test_transcript_reflows_existing_panels_after_resize(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.formatted_text import fragment_list_to_text
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from rich.cells import cell_len
        from trina.console_ui import EnhancedConsoleInput

        size = [Size(rows=30, columns=88)]
        output = Vt100_Output(io.StringIO(), lambda: size[0], enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False,
                                          dynamic_values=lambda *_: (), plain=True)
            editor.write(render_panel("结果", sections=[("", "较长内容" * 30)]))
            wide = fragment_list_to_text(editor._transcript())
            size[0] = Size(rows=30, columns=40)
            narrow = fragment_list_to_text(editor._transcript())
        self.assertNotEqual(wide, narrow)
        self.assertLessEqual(max(cell_len(line) for line in narrow.splitlines()), 40)
        self.assertIn("结果", narrow)

    def test_partial_command_is_visible_below_input_and_enter_expands_it(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False,
                                          dynamic_values=lambda *_: (), plain=True)
            editor.buffer.text = "/h"
            editor.buffer.cursor_position = 2
            menu = "".join(text for _, text in editor._menu())
            self.assertIn("/help", menu)
            self.assertIn("/history", menu)
            pipe.send_text("/h\r")
            self.assertEqual(editor.read(), "/help")

    def test_one_enter_sends_multiline_supplement_while_running(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        text = '补充第一行\n第二行保留 "未闭合引号'
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "运行中", running=lambda: True,
                                          dynamic_values=lambda *_: (), plain=True)
            self.assertIn("Enter 发送补充", editor._hint())
            pipe.send_text("\x1b[200~" + text + "\x1b[201~\r")
            self.assertEqual(editor.read(), text)

    def test_home_uses_shell_lettering_and_only_version_below_it(self):
        text = render_home(width=88, color=True)
        self.assertIn("███████", clean_text(text))
        self.assertIn("▓", clean_text(text))
        self.assertEqual(text.splitlines()[-1].strip(), "v0.2.0")
        self.assertNotIn("睡莲紫雾", text)
        self.assertNotIn("模型", text)
        self.assertNotIn("/play", text)
        self.assertNotIn("\x1b[5m", text)

    def test_command_catalog_drives_help_and_toolbar_hint(self):
        self.assertIn("play", command_names())
        self.assertIn("clear", command_names())
        self.assertIn("--max-steps", command_hint("/play"))
        # 普通输入不占底栏；输入斜杠后才给出筛选提示。
        self.assertEqual(command_hint("hello"), "")
        self.assertIn("筛选", command_hint("/"))

    def test_supplier_names_with_spaces_are_quoted_in_command_completion(self):
        import shlex
        from prompt_toolkit.document import Document
        from trina.console_ui import ChoiceSpec
        completer = SlashCommandCompleter(dynamic_values=lambda *_: [ChoiceSpec("公司 DeepSeek")])
        completion = next(completer.get_completions(Document("/model use 公司"), None))
        self.assertEqual(shlex.split("model use " + completion.text), ["model", "use", "公司 DeepSeek"])

    def test_model_picker_search_and_enter_return_the_matching_id(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import ChoiceSpec, select_option
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("custom\r")
            self.assertEqual(select_option("providers", [ChoiceSpec("deepseek", "DeepSeek"),
                                                         ChoiceSpec("custom", "自定义")]), "custom")

    def test_boxed_model_screen_restores_terminal_and_never_prints_masked_key(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.console_ui import ChoiceSpec, select_option
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=30, columns=90), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            pipe.send_text("\r")
            self.assertEqual(select_option("Model", [ChoiceSpec("back", "返回")],
                             subtitle="模型 gpt-5.2\nAPI Key work ••••••••"), "back")
        rendered = stream.getvalue()
        self.assertIn("┌", rendered)
        self.assertIn("gpt-5.2", rendered)
        self.assertIn("\x1b[?1049h", rendered)
        self.assertIn("\x1b[?1049l", rendered)

    def test_secret_input_uses_password_display_and_escape_cancels(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.console_ui import edit_field
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            pipe.send_text("\r")
            self.assertEqual(edit_field("API Key", "test-private-key", secret=True), "test-private-key")
        self.assertNotIn("test-private-key", stream.getvalue())
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            pipe.send_text("\x03")
            with self.assertRaises(KeyboardInterrupt):
                edit_field("API Key", secret=True)

    def test_external_markup_is_literal_and_terminal_controls_are_removed(self):
        value = "[bold red]原始文本[/bold red]\x1b[2J\x1b]0;伪造标题\x07尾部"
        text = render_message(value, width=88)
        self.assertIn("[bold red]", text)
        self.assertIn("原始文本", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("伪造标题", text)
        self.assertEqual(clean_text("a\rb\x08c\u202ed"), "abcd")

    def test_color_respects_no_color_dumb_pipe_and_plain(self):
        with patch("sys.stdout.isatty", return_value=True), patch.dict("os.environ", {}, clear=True):
            self.assertTrue(use_color())
            self.assertFalse(use_color(plain=True))
            with patch.dict("os.environ", {"NO_COLOR": ""}):
                self.assertFalse(use_color())
            with patch.dict("os.environ", {"TERM": "dumb"}):
                self.assertFalse(use_color())
        with patch("sys.stdout.isatty", return_value=False):
            self.assertFalse(use_color())

    def test_missing_rich_falls_back_to_wrapped_text(self):
        original = builtins.__import__
        def without_rich(name, *args, **kwargs):
            if name == "rich" or name.startswith("rich."):
                raise ImportError("simulated missing optional dependency")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=without_rich):
            text = render_home(width=40)
            message = render_message("很长的无依赖提示" * 12, width=20, color=True)
        self.assertGreater(len(text.splitlines()), 2)
        self.assertIn("v0.2.0", text)
        self.assertNotIn("/play", text)
        self.assertNotIn("\x1b", text)
        from rich.cells import cell_len
        self.assertLessEqual(max(cell_len(line) for line in message.splitlines()), 20)
        self.assertNotIn("\x1b", message)

    def test_board_uses_real_step_counter_and_recent_tool_records(self):
        state = RunState(run_id="counts", target="127.0.0.1")
        state.counters["steps"] = 2
        state.steps = [{"kind": "tool", "tool": "first", "ok": True}, {"index": 1},
                       {"kind": "tool", "tool": "second", "ok": False}, {"index": 2}]
        state.coverage = {"executed": ["allowed-only"], "untested": ["pending", {"test_id": "other"}]}
        self.assertEqual(summarise(state)["steps"], 2)
        self.assertEqual(len(summarise(state)["last_steps"]), 2)
        text = render_board(state, width=110)
        self.assertIn("获准检查 1 项", text)
        self.assertIn("不等于执行完成", text)
        self.assertIn("pending", text)
        self.assertIn("second", text)

    def test_step_renders_phase_and_actual_duration(self):
        step = StepRecord(2, "enum", action="example", summary="参数被拒绝", denied=True, duration=1.2, llm_seconds=2.3)
        text = render_step(step)
        for expected in ("02 拒绝", "枚举", "example", "3.5s", "参数被拒绝"):
            self.assertIn(expected, text)

    def test_approval_view_redacts_password_and_does_not_approve(self):
        console, output = self.console()
        console.session.approval_mode = "all"
        console.session.approval_timeout = 0.01
        request = {"tool": "example", "level": "verify", "params": {"password": "private-pass", "target": "127.0.0.1"}}
        self.assertFalse(console.session.request_approval(request))
        text = "\n".join(output)
        self.assertIn("待审批", text)
        self.assertIn("输入 y", text)
        self.assertNotIn("private-pass", text)
        self.assertTrue(console.session.decisions[-1]["timeout"])

    def test_running_board_does_not_display_previous_task(self):
        console, output = self.console()
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        console._last_state = RunState(run_id="old", target="old.invalid")
        console._plan = SimpleNamespace(target="127.0.0.1")
        console._live_step = StepRecord(3, "enum", action="example", summary="当前结果", ok=True)
        console.handle("/board")
        text = "\n".join(output)
        self.assertIn("当前任务", text)
        self.assertIn("第 3 步", text)
        self.assertNotIn("old.invalid", text)
        self.assertNotIn("任务运行中，可用命令", text)

    def test_task_board_does_not_mix_chat_hypotheses(self):
        console, output = self.console()
        console._last_state = RunState(run_id="task", target="127.0.0.1")
        console.chat = SimpleNamespace(state=RunState(run_id="chat", target="other"),
                                       ledger=SimpleNamespace(to_dict=lambda: [{"id": "wrong-chat", "status": "open"}]))
        console.handle("/board")
        self.assertNotIn("wrong-chat", "\n".join(output))

    def test_slash_parse_failure_never_starts_chat_and_typo_has_hint(self):
        console, output = self.console()
        with patch.object(console, "_start_chat_turn") as start:
            console.handle('/run "unfinished')
            console.handle('/staus')
            start.assert_not_called()
        text = "\n".join(output)
        self.assertIn("引号未闭合", text)
        self.assertIn("/status", text)

    def test_help_can_show_one_command_with_all_options(self):
        console, output = self.console()
        console.handle("/help play")
        text = "\n".join(output)
        self.assertIn("命令 /play", text)
        self.assertIn("--target", text)
        self.assertIn("--approve", text)
        self.assertIn("拿到 flag", text)

    def test_dynamic_completion_reads_real_tools_and_ranges(self):
        console, _ = self.console()
        tools = [item.value for item in console._completion_values("tool", [])]
        ranges = [item.value for item in console._completion_values("ranges", ["status"])]
        self.assertIn("port_scan", tools)
        self.assertEqual(ranges, [])

    def test_prompt_distinguishes_approval_pause_and_stop(self):
        console, _ = self.console()
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        self.assertIn("运行中", console._prompt())
        console.session.pause_event.set()
        self.assertIn("暂停", console._prompt())
        console.session.pending = {"tool": "example"}
        self.assertIn("待审批 y/n", console._prompt())
        console.session.pending = None
        console.session.pause_event.clear()
        console.session.stop_event.set()
        self.assertIn("停止中", console._prompt())

    def test_slash_quit_uses_same_two_step_exit_as_bare_quit(self):
        console, _ = self.console()
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        self.assertTrue(console.handle("/quit"))
        self.assertFalse(console.handle("/quit"))

    def test_plain_option_is_forwarded_by_cli(self):
        from trina.cli import main
        with patch("trina.interactive.main", return_value=0) as start:
            self.assertEqual(main(["console", "--plain"]), 0)
            self.assertIn("--plain", start.call_args.args[0])

    def test_noninteractive_play_shares_result_view_and_teardown(self):
        from trina.cli import main
        from unittest.mock import Mock
        plan = SimpleNamespace(target="127.0.0.1", teardown=Mock())
        state = RunState(run_id="fixture", target="127.0.0.1", status="partial", verdict="INCONCLUSIVE")
        state.coverage = {"executed": ["allowed"], "untested": []}
        output = io.StringIO()
        with patch("trina.play.build_plan", return_value=plan), patch("trina.play.run_plan", return_value=state), contextlib.redirect_stdout(output):
            self.assertEqual(main(["play", "--target", "127.0.0.1", "--no-report"]), 1)
        plan.teardown.assert_called_once()
        self.assertIn("TRINA / WORKSPACE", output.getvalue())
        self.assertIn("获准检查 1 项", output.getvalue())
        self.assertNotIn("已测/未测检查项", output.getvalue())

    def test_synchronous_approval_does_not_claim_a_timeout(self):
        text = render_approval({"tool": "example", "level": "verify"}, None)
        self.assertIn("直接回车按拒绝处理", text)
        self.assertNotIn("秒未答复", text)

    def test_redirected_watch_prints_one_snapshot_without_clear_screen(self):
        from trina.cli import cmd_watch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sample" / "state.json"
            path.parent.mkdir()
            path.write_text(json.dumps(RunState(run_id="sample", target="127.0.0.1").to_dict()), encoding="utf-8")
            config = TrinaConfig.load().with_overrides(run__workspace=str(root))
            args = argparse.Namespace(run_id="sample", once=False, interval=0.5)
            stream = io.StringIO()
            with patch("trina.cli.build_config", return_value=config), contextlib.redirect_stdout(stream), patch("time.sleep") as sleep:
                self.assertEqual(cmd_watch(args), 0)
                sleep.assert_not_called()
            self.assertIn("TRINA / WORKSPACE", stream.getvalue())
            self.assertNotIn("\x1b", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
