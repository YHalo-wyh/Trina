"""会话任务清单、模型工具、恢复和固定任务卡回归。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMResponse
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.todo import TodoList
from trina.tools import build_registry
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext, ToolRegistry


class TodoListTests(unittest.TestCase):
    def test_replace_update_and_restore_preserve_stable_ids(self):
        todo = TodoList()
        snapshot = todo.replace([
            {"text": "审计现有仓库", "status": "in_progress", "phase": "准备"},
            {"text": "运行回归测试"},
            {"text": "整理文档", "status": "blocked", "note": "等待接口"},
        ])
        self.assertEqual([row["id"] for row in snapshot["tasks"]], ["T1", "T2", "T3"])
        self.assertEqual(snapshot["counts"]["in_progress"], 1)
        todo.update("T1", status="completed")
        todo.update("T2", status="in_progress", note="正在运行")
        restored = TodoList()
        restored.restore(todo.snapshot())
        self.assertEqual(restored.snapshot(), todo.snapshot())
        first_view = restored.view()
        self.assertIs(restored.view(), first_view)
        added = restored.add("提交结果")
        self.assertEqual(added["id"], "T4")
        self.assertIsNot(restored.view(), first_view)

    def test_duplicate_text_in_same_phase_is_rejected(self):
        todo = TodoList()
        with self.assertRaisesRegex(ValueError, "重复任务"):
            todo.replace([{"text": "测试", "phase": "A"}, {"text": "测试", "phase": "A"}])


class TodoToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
                                                   audit__dir=str(self.root / "logs"))
        audit = AuditLog.open(self.config.audit_dir, run_id="todo", resume=False)
        guard = ScopeGuard(self.config.scope, audit=audit)
        self.registry = build_registry(["todo"])
        self.ctx = ToolContext(config=self.config, guard=guard, audit=audit,
                               state=RunState(run_id="todo", target=""), http=HttpClient(guard),
                               workspace=self.root / "run", tools=self.registry)

    def test_registered_tool_emits_complete_snapshot(self):
        events = []
        subscription = self.ctx.events.subscribe(events.append)
        self.addCleanup(subscription.close)
        result = self.registry.execute("todo", {"action": "replace", "items": [
            {"text": "读取代码", "status": "in_progress"}, {"text": "运行测试"},
        ]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["tasks"][0]["id"], "T1")
        self.assertTrue(any(event.kind == "todo.updated" for event in events))
        update = self.registry.execute("todo", {"action": "update", "task_id": "T1",
                                                 "status": "completed"}, self.ctx)
        self.assertTrue(update.ok, update.error)
        self.assertEqual(update.data["task"]["status"], "completed")


class TodoChatAndUiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
                                                   audit__dir=str(self.root / "logs"), llm__backend="replay")

    @staticmethod
    def _backend():
        class Backend:
            inputs = []

            def complete_messages(self, messages, **kwargs):
                self.inputs.append(copy.deepcopy(messages))
                return LLMResponse(text="已完成")

        return Backend()

    def test_chat_resume_restores_todo_without_model_request(self):
        chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=self._backend())
        chat.todo.replace([{"text": "检查实现", "status": "in_progress"}, {"text": "运行测试"}])
        chat.ask("开始处理")
        run_id = chat.run_id
        chat.close()
        backend = self._backend()
        restored = ChatAgent.resume(self.config, run_id, registry=ToolRegistry([]), backend=backend)
        self.addCleanup(restored.close)
        self.assertEqual(restored.todo.snapshot()["tasks"][0]["text"], "检查实现")
        self.assertEqual(backend.inputs, [])

    def test_fixed_task_card_prioritizes_active_and_collapses_overflow(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput

        todo = TodoList()
        todo.replace([{"text": "当前任务", "status": "in_progress"},
                      *({"text": f"待处理 {index}"} for index in range(1, 9))])
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "就绪", running=lambda: True,
                                          dynamic_values=lambda *_: (), plain=True, tasks=todo.snapshot)
            rendered = "".join(fragment[1] for fragment in editor._tasks())
            self.assertIn("1 进行中 · 8 待处理", rendered)
            self.assertIn("当前任务", rendered)
            self.assertIn("/tasks", rendered)
            self.assertLess(editor._tasks_height(), 10)

    def test_tasks_command_is_local_and_new_chat_clears_projection(self):
        from trina.interactive import Console

        output: list[str] = []
        backend = self._backend()
        console = Console(self.config, registry=build_registry(), backend=backend,
                          output=output.append, autostart_lab=False, plain=True)
        self.addCleanup(console._cleanup)
        chat = console._ensure_chat()
        chat.todo.replace([{"text": "核对任务清单", "status": "in_progress"}])
        console.handle("/tasks")
        self.assertIn("核对任务清单", "\n".join(output))
        self.assertEqual(backend.inputs, [])
        console.handle("/new")
        self.assertEqual(console._todo_snapshot, {})


if __name__ == "__main__":
    unittest.main()
