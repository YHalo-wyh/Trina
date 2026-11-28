"""V0.7 Agent Workspace 的响应式布局、主题和交互回归。"""
from __future__ import annotations

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.cells import cell_len

from trina.config import TrinaConfig
from trina.console_state import ConsoleContext, _cells
from trina.console_ui import EnhancedConsoleInput, command_help_sections
from trina.interactive import Console
from trina.model_picker import ModelChoice, select_model
from trina.plan_review import PlanReview, render_plan_review, show_plan_review
from trina.subworker_ui import ordered_workers, render_grid, render_workers


SIZES = ((80, 24), (100, 30), (120, 36), (160, 48))


class _Workers:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def rows(self):
        return [dict(row) for row in self._rows]

    def dialog_lines(self, task_id: str, width: int):
        return [("过程", f"{task_id} 正在核对本地结果")]


def _worker(index: int, status: str = "running") -> dict:
    now = time.time()
    return {
        "id": f"worker-{index}", "goal": f"任务 {index}", "status": status,
        "model": "chat-test", "reasoning_effort": "high", "started_at": now - index,
        "ended_at": None, "updated_at": now - 1, "activity": "运行测试", "calls": 1,
        "tools": index, "tokens": index * 100,
    }


class WorkspaceV07Tests(unittest.TestCase):
    def test_tool_expand_copy_stream_update_and_focus_at_all_terminal_sizes(self):
        from trina.events import EventBus
        from trina.event_view import EventTranscript

        for columns, rows in SIZES:
            with self.subTest(size=(columns, rows)), tempfile.TemporaryDirectory() as tmp:
                output = Vt100_Output(io.StringIO(), lambda: Size(rows=rows, columns=columns), enable_cpr=False)
                with create_pipe_input() as pipe, create_app_session(input=pipe, output=output), \
                        patch("trina.transcript.copy_to_terminal") as clipboard:
                    editor = EnhancedConsoleInput(status=lambda: "运行中", running=lambda: True,
                                                  dynamic_values=lambda *_: (), motion="none", plain=True)
                    editor.buffer.text = "未发送草稿\n第二行"
                    bus = EventBus(Path(tmp) / "events.sqlite3", "ui-test")
                    view = EventTranscript(editor.write, editor=editor)
                    bus.subscribe(view.consume)
                    bus.publish("tool.started", call_id="one", tool="terminal_exec", params={"argv": ["local-check"]})
                    bus.publish("tool.output", call_id="one", delta="\n".join(f"result-{i:02d}" for i in range(30)))
                    frames, stage = [], [0]

                    def rendered(app):
                        if app.is_done:
                            return
                        try:
                            card = editor._chunks[0].tool_card
                            self.assertIs(app.layout.current_control, editor.input_control)
                            self.assertEqual(editor.buffer.text, "未发送草稿\n第二行")
                            screen = app.renderer.last_rendered_screen
                            frame = "\n".join("".join(screen.data_buffer[y][x].char for x in range(columns))
                                              for y in range(rows))
                            if stage[0] == 0:
                                frames.append(frame)
                                self.assertNotIn("result-15", frame)
                                self.assertIn("F3", frame)
                                stage[0] = 1
                                pipe.send_text("\x1bOR")  # F3：真实 VT 按键解析。
                            elif stage[0] == 1 and card.expanded:
                                frames.append(frame)
                                self.assertIn("result-15", editor._chunk_text(editor._chunks[0], columns - 1))
                                bus.publish("tool.output", call_id="one", delta="\nlatest-result")
                                self.assertTrue(editor._chunks[0].tool_card.expanded)
                                stage[0] = 2
                                pipe.send_text("\x1bOS")
                            elif stage[0] == 2 and card.copied:
                                copied = app.clipboard.get_data().text
                                self.assertIn("result-15", copied)
                                self.assertIn("latest-result", copied)
                                clipboard.assert_called_once_with(copied, output)
                                stage[0] = 3
                                pipe.send_text("\x1bOR")
                            elif stage[0] == 3 and not card.expanded:
                                frames.append(frame)
                                self.assertNotIn("result-15", frame)
                                self.assertIn("latest-result", frame)
                                stage[0] = 4
                                app.exit()
                        except Exception as exc:
                            app.exit(exception=exc)

                    editor.app.after_render += rendered
                    editor.app.run(pre_run=lambda: editor.app.loop.call_later(
                        5, lambda: editor.app.exit(exception=TimeoutError("tool keys did not finish"))
                        if not editor.app.is_done else None))
                    self.assertEqual(stage[0], 4)
                    self.assertEqual(len(frames), 3)
                    self.assertTrue(all(len(frame.splitlines()) == rows for frame in frames))

    def test_tool_actions_target_visible_history_and_ignore_pruned_cards(self):
        from trina.events import EventBus
        from trina.event_view import EventTranscript

        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe, \
                create_app_session(input=pipe, output=DummyOutput()), patch("trina.transcript.copy_to_terminal"):
            editor = EnhancedConsoleInput(status=lambda: "就绪", running=lambda: False,
                                          dynamic_values=lambda *_: (), plain=True)
            bus = EventBus(Path(tmp) / "events.sqlite3", "history")
            view = EventTranscript(editor.write, editor=editor)
            bus.subscribe(view.consume)
            bus.publish("tool.finished", call_id="first", tool="terminal_exec", data={"stdout": "older result"})
            editor.write("historical conversation\n" * 100)
            bus.publish("tool.finished", call_id="last", tool="terminal_exec", data={"stdout": "latest result"})
            control = editor.transcript_control
            control.create_content(80, 20)
            editor._follow_latest = False
            editor._scrollback = control.max_scrollback()
            self.assertTrue(editor._tool_action(copy=True))
            self.assertIn("older result", editor.app.clipboard.get_data().text)
            self.assertNotIn("latest result", editor.app.clipboard.get_data().text)
            editor.clear_transcript()
            self.assertFalse(editor._tool_action(copy=False))

    def test_tool_cards_follow_ascii_theme_without_rewriting_tool_data(self):
        from trina.terminal import render_tool_call_fragments

        for width, _ in SIZES:
            fragments = render_tool_call_fragments("terminal_exec", {"argv": ["local-check"]},
                output="verbatim · │ ─", status="completed", exit_code=0, width=width, color=True, palette="ascii")
            rendered = "".join(text for _, text in fragments)
            self.assertTrue(rendered.startswith("+- shell |"))
            self.assertIn("verbatim · │ ─", rendered)
            self.assertNotIn("╭", rendered)
            self.assertNotIn("╯", rendered)
            self.assertNotIn("├", rendered)
            self.assertTrue(all(not style for style, _ in fragments))
            self.assertTrue(all(_cells(line) == width for line in rendered.splitlines()))

    def test_main_workspace_renders_at_required_terminal_sizes_and_keeps_input_focus(self):
        for columns, rows in SIZES:
            with self.subTest(size=(columns, rows)):
                stream = io.StringIO()
                output = Vt100_Output(stream, lambda: Size(rows=rows, columns=columns), enable_cpr=False)
                context = ConsoleContext(
                    "运行中", "Chat Test", "C:/workspace/trina", "修复终端交互",
                    context_tokens=12_000, context_window=128_000, provider="Local",
                    thinking="high", session="UI 升级", model_in_footer=False,
                )
                captured: list[str] = []
                with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
                    editor = EnhancedConsoleInput(
                        status=lambda: "运行中", running=lambda: False, dynamic_values=lambda *_: (),
                        context=lambda: context, motion="none", plain=True,
                    )
                    editor.write("历史消息\n" * 80)

                    def rendered(app):
                        if captured:
                            return
                        screen = app.renderer.last_rendered_screen
                        lines = []
                        for y in range(rows):
                            row = screen.data_buffer.get(y, {})
                            lines.append("".join(row[x].char if x in row else " " for x in range(columns)))
                        captured.append("\n".join(lines))
                        try:
                            self.assertIs(app.layout.current_control, editor.input_control)
                        finally:
                            app.exit()

                    editor.app.after_render += rendered
                    editor.app.run()
                self.assertIn("TRINA", captured[0])
                self.assertIn("UI 升级", captured[0])
                self.assertIn("Trina", captured[0])
                self.assertEqual(len(captured[0].splitlines()), rows)

    def test_multiline_input_uses_alt_enter_and_plain_enter_submits(self):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "就绪", running=lambda: False,
                                          dynamic_values=lambda *_: (), plain=True)
            pipe.send_text("第一行\x1b\r第二行\r")
            self.assertEqual(editor.read(), "第一行\n第二行")

    def test_header_footer_and_ascii_mode_fit_every_required_width(self):
        for columns, _ in SIZES:
            context = ConsoleContext(
                "运行中", "model", "C:/workspace/project", "会话任务", provider="provider",
                thinking="high", session="session", branch="main", workers="2 运行 / 1 等待",
                context_tokens=50, context_window=100, ascii_mode=True, model_in_footer=False,
            )
            header = "".join(text for _, text in context.header(columns))
            self.assertLessEqual(max(_cells(line) for line in header.splitlines()), columns)
            self.assertLessEqual(_cells("".join(text for _, text in context.footer(columns))), columns)
            rail = "".join(text for _, text in context.rail(columns))
            self.assertEqual(_cells(rail), columns)
            self.assertNotIn("◆", header)
            self.assertNotIn("─", rail)

    def test_worker_grid_uses_single_four_and_nine_worker_layouts(self):
        manager = _Workers([_worker(index) for index in range(1, 10)])
        expected = {80: ("worker-1", "worker-2"), 100: ("worker-4", "worker-5"),
                    120: ("worker-4", "worker-5"), 160: ("worker-9", "worker-10")}
        for width, height in SIZES:
            text = "".join(value for _, value in render_grid(manager, width, height, 0))
            present, absent = expected[width]
            self.assertIn(present, text)
            self.assertNotIn(absent, text)
            self.assertLessEqual(len(text.splitlines()), height)
            self.assertTrue(all(_cells(line) <= width for line in text.splitlines()))

    def test_worker_groups_are_stable_and_show_progress_and_update(self):
        rows = [_worker(1, "completed"), _worker(2, "pending"), _worker(3, "running"),
                _worker(4, "failed"), _worker(5, "running")]
        self.assertEqual([row["id"] for row in ordered_workers(rows)],
                         ["worker-3", "worker-5", "worker-2", "worker-1", "worker-4"])
        text = "".join(value for _, value in render_workers(rows, 100, 0, count=10))
        for group in ("RUNNING", "QUEUED", "COMPLETED", "FAILED"):
            self.assertIn(group, text)
        self.assertIn("阶段", text)
        self.assertIn("刚刚", text)

    def test_model_picker_adjusts_thinking_without_extra_catalog_request(self):
        choice = ModelChoice("local", "Local", "chat-test", current=True,
                             levels=("default", "low", "high"), reasoning_effort="default")
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("]]\r")
            selected = select_model([choice], plain=True)
        self.assertEqual(selected.reasoning_effort, "high")

    def test_plan_workspace_renders_edits_and_confirms_without_model_call(self):
        review = PlanReview("升级终端", criteria=("四种尺寸可用", "输入焦点可恢复"))
        edited = review.edit_task("升级终端工作台").add_note("保留原有快捷键")
        self.assertIn("审阅意见", edited.draft())
        for width, _ in SIZES:
            rendered = render_plan_review(edited, width)
            self.assertLessEqual(max(cell_len(line) for line in rendered.splitlines()), width)
            self.assertIn("验收条件", rendered)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("c")
            self.assertEqual(show_plan_review(edited, plain=True), "confirm")

    def test_theme_footer_help_and_queue_states_are_real_configuration(self):
        for theme in ("dark", "light", "high_contrast", "ascii"):
            config = TrinaConfig._from_mapping({"ui": {"output_palette": theme}})
            config.validate()
        custom = TrinaConfig._from_mapping({"ui": {
            "status_line_preset": "custom", "status_line_custom": "tokens,context,git,workers",
        }})
        custom.validate()
        self.assertIn("常用工作流", dict(command_help_sections()))

        output: list[str] = []
        console = Console(TrinaConfig(), output=output.append, autostart_lab=False)
        self.addCleanup(console._cleanup)
        console._enqueue_draft("稍后发送")
        console._cmd_queue(["drop", "1"])
        console._cmd_queue(["list"])
        self.assertIn("CANCELLED", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
