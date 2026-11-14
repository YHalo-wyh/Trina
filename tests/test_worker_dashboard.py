"""真实委派工具、独立消息控制、四宫格视图与动态协议隔离。"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.config import TrinaConfig
from trina.console_state import _cells
from trina.events import RuntimeEvent
from trina.interactive import Console
from trina.llm.replay_backend import ReplayBackend
from trina.state import RunState
from trina.subworkers import Subworkers
from trina.subworker_ui import render_grid
from trina.swarm import TaskSpec
from trina.tools import build_registry


class WorkerDashboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"), report__output_dir=str(self.root / "reports"))

    def manager(self, count=4):
        manager = Subworkers(lambda _: None)
        for index in range(count):
            name = f"worker-{index + 1}"
            manager._rows[name] = {"id": name, "goal": "验证分支", "status": "running", "model": "deepseek-chat", "reasoning_effort": "high"}
        return manager

    def test_grid_layout_pages_resize_zoom_and_reuses_unchanged_dialog(self):
        manager = self.manager(7)
        for name in manager._rows:
            manager._record(name, "reply", "回复", "真实输出 " + "中文 mixed " * 60)
        first = manager.dialog_lines("worker-1", 60)
        self.assertIs(first, manager.dialog_lines("worker-1", 60))
        for width, height in ((1, 1), (20, 12), (88, 24), (144, 30), (180, 40)):
            text = "".join(text for _, text in render_grid(manager, width, height, 0))
            self.assertTrue(all(_cells(line) <= width for line in text.splitlines()))
            self.assertLessEqual(len(text.splitlines()), height)
        page = "".join(t for _, t in render_grid(manager, 144, 30, 5))
        self.assertIn("worker-6", page)
        self.assertNotIn("worker-1", page)
        zoom = "".join(t for _, t in render_grid(manager, 144, 30, 1, zoom=True))
        self.assertIn("worker-2", zoom)
        self.assertNotIn("worker-1", zoom)
        manager._record("worker-2", "new", "输出", "更新")
        self.assertIs(first, manager.dialog_lines("worker-1", 60))

    def test_live_projection_ignores_raw_model_protocol_and_tracks_final_output(self):
        manager = self.manager(1)
        def event(kind, payload, seq):
            return RuntimeEvent(seq=seq, stream_id="test", run_id="r", task_id="worker-1", worker_id="w", kind=kind, time=time.time(), source="test", call_id="call", payload=payload)
        manager._progress(event("model.text_delta", {"delta": "<|DSML|invoke>"}, 1))
        manager._progress(event("react.action", {"thought": "核验实际路径"}, 2))
        manager._progress(event("tool.started", {"tool": "terminal_exec", "params": {"argv": ["python", "check.py"]}}, 3))
        manager._progress(event("tool.output", {"delta": "first\n"}, 4))
        manager._progress(event("tool.output", {"delta": "second"}, 5))
        manager._progress(event("assistant.message", {"text": "最终结果", "message_id": "reply"}, 6))
        text = "\n".join(t for _, t in manager.dialog_lines("worker-1", 60))
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertIn("最终结果", text)
        self.assertNotIn("DSML", text)
        for i in range(100):
            manager._record("worker-1", str(i), "输出", "x" * 10000)
        self.assertLessEqual(len(manager._dialogs["worker-1"]), 80)
        self.assertLessEqual(len(manager.dialog_lines("worker-1", 60)), 600)
        stable_block = manager._block_cache[("worker-1", 60)]["21"][1]
        manager._record("worker-1", "99", "输出", "changed")
        manager.dialog_lines("worker-1", 60)
        self.assertIs(stable_block, manager._block_cache[("worker-1", 60)]["21"][1])

    def test_registered_delegation_executes_real_child_terminal_and_reads_result(self):
        parent = ReplayBackend(self.config.llm)
        parent.script = [{"response": json.dumps({"action": "worker_tasks", "params": {"action": "spawn", "task_id": "code", "target": "localhost", "goal": "写 proof.txt 并验证"}})},
                         {"response": json.dumps({"action": "worker_tasks", "params": {"action": "wait", "task_id": "code", "wait_seconds": 5}})},
                         {"response": "已核对独立子任务结果。"}]
        def child_factory(llm):
            child = ReplayBackend(llm)
            code = "from pathlib import Path; Path('proof.txt').write_text('child'); print('child-output')"
            child.script = [{"response": json.dumps({"action": "terminal_exec", "params": {"argv": [sys.executable, "-c", code], "cwd": "run"}})},
                            {"response": json.dumps({"final": {"summary": "子任务已实际执行"}})}]
            return child
        console = Console(self.config, output=lambda _: None, backend=parent, registry=build_registry())
        self.addCleanup(console._cleanup)
        console._backend_factory = child_factory
        chat = console._ensure_chat()
        chat.ctx.extra["disable_title_generation"] = True
        chat.ask("把独立编码检查分给 Worker 并核对结果")
        console.subworkers.thread.join(8)
        result = console.subworkers.task_result("code")
        self.assertIsNotNone(result)
        self.assertNotIn(result["status"], {"pending", "running"})
        artifact = Path(result["result"]["artifacts"]["workspace"]) / "proof.txt"
        self.assertEqual(artifact.read_text(), "child")
        self.assertNotIn("child-output", json.dumps(chat.state.to_dict(), ensure_ascii=False))
        dialog = "\n".join(t for _, t in console.subworkers.dialog_lines("code", 80))
        self.assertIn("child-output", dialog)
        self.assertIn("子任务已实际执行", dialog)
        self.assertTrue(chat.audit.verify()[0])
        restored = Subworkers(lambda _: None)
        self.addCleanup(restored.close)
        restored.start(self.config, checkpoint=str(console.subworkers.runtime.checkpoint), backend_factory=child_factory)
        restored.thread.join(5)
        restored_dialog = "\n".join(t for _, t in restored.dialog_lines("code", 80))
        self.assertIn("child-output", restored_dialog)
        self.assertEqual(restored.usage.usage.calls, 0, "历史重放不能重新调用或统计模型")
        self.assertEqual(restored.rows()[0]["tools"], 1)

    def test_targeted_cancel_and_message_do_not_stop_sibling(self):
        entered = threading.Barrier(3, timeout=5)
        release = threading.Event()
        manager = Subworkers(lambda _: None)
        def execute(worker, task, run_id, hints, registry):
            entered.wait()
            release.wait(5)
            stopped = manager.runtime._task_stops[task.id].is_set()
            return RunState(run_id=run_id, target=task.target, status="stopped" if stopped else "success")
        manager.start(self.config, [TaskSpec("a", "localhost", "A"), TaskSpec("b", "localhost", "B")], executor=execute, backend_factory=lambda _: object())
        self.addCleanup(lambda: (release.set(), manager.close()))
        entered.wait()
        manager.task_control("b", "message", "先验证输出文件")
        manager.task_control("a", "cancel")
        self.assertFalse(manager.runtime.stop_event.is_set())
        self.assertEqual(manager.runtime._task_steers["b"].get_nowait(), "先验证输出文件")
        release.set()
        manager.thread.join(8)
        self.assertEqual(manager.task_result("a")["status"], "cancelled")
        self.assertEqual(manager.task_result("b")["status"], "completed")

    def test_ui_hides_hint_and_normal_weight_role_and_keeps_grid_keyboard(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.console_ui import EnhancedConsoleInput
        from trina.subworker_ui import show_subworkers
        output = Vt100_Output(io.StringIO(), lambda: Size(rows=30, columns=144), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: ())
            editor.buffer.text = "/"
            editor.buffer.cursor_position = 1
            self.assertEqual(editor._hint(), "")
            self.assertTrue(editor._items())
            self.assertNotIn("Tab 填入", "".join(t for _, t in editor._menu()))
            self.assertFalse(editor.app.style.get_attrs_for_style_str("class:context.role").bold)
            pipe.send_text("\tzf")
            self.assertEqual(show_subworkers(self.manager(), self.config), ("message", "worker-2"))

    def test_worker_tool_without_host_refuses_recursion_and_old_results_survive_new_run(self):
        from trina.tools.coordination import WorkerTasks
        from types import SimpleNamespace
        self.assertFalse(WorkerTasks().run(SimpleNamespace(extra={}), "spawn").ok)
        from trina.brain.chat import ChatAgent
        standalone = ChatAgent(self.config, registry=build_registry(), backend=ReplayBackend(self.config.llm))
        self.addCleanup(standalone.close)
        standalone._initialize_messages()
        self.assertNotIn("worker_tasks", standalone._messages[1]["content"], "无委派宿主不得消耗 schema/合同预算")
        manager = Subworkers(lambda _: None)
        self.addCleanup(manager.close)
        for name in ("first", "second"):
            manager.start(self.config, [TaskSpec(name, "localhost", name)],
                          executor=lambda w, t, r, h, g: RunState(run_id=r, target=t.target, status="success"), backend_factory=lambda _: object())
            manager.thread.join(5)
        self.assertEqual(manager.task_result("first")["status"], "completed")
        self.assertEqual(manager.task_result("second")["status"], "completed")
