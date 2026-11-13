"""历史对话跨实例恢复、切换与失败保护。"""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trina.brain.chat import ChatAgent
from trina.chat_store import ChatStore
from trina.config import LLMConfig, TrinaConfig
from trina.interactive import Console
from trina.llm import LLMResponse, LLMUsage
from trina.llm.replay_backend import ReplayBackend
from trina.tools.registry import Tool, ToolRegistry, ToolResult, ToolSpec


class ChatResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
                                                  audit__dir=str(self.root / "logs"), llm__backend="replay")
        env = patch.dict("os.environ", {"TRINA_PROVIDERS_FILE": str(self.root / "providers.json"),
                                        "TRINA_TASKS_FILE": str(self.root / "tasks.json")})
        env.start()
        self.addCleanup(env.stop)
        self.store = ChatStore(self.config.workspace_dir)

    def backend(self):
        backend = ReplayBackend(LLMConfig(backend="replay"))
        backend.inputs = []
        def complete(messages, **kwargs):
            backend.inputs.append(copy.deepcopy(messages))
            return LLMResponse(text="已收到，保留上下文", usage=LLMUsage(input_tokens=100, output_tokens=5, tokens_reported=True))
        backend.complete_messages = complete
        return backend

    def chat(self, **kwargs):
        chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=self.backend(), **kwargs)
        self.addCleanup(chat.close)
        return chat

    def test_saved_chat_restores_messages_facts_ledger_usage_and_delta_baseline(self):
        chat = self.chat()
        chat.state.host("127.0.0.1").add_port(80, service="http")
        chat.ledger.add("验证已发现的接口", target="127.0.0.1")
        chat.ask("只处理测试目录中的数据")
        chat.close()
        old = self.store.load(chat.run_id)
        backend = self.backend()
        restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=backend)
        self.addCleanup(restored.close)
        self.assertEqual(restored._messages, old["messages"])
        self.assertEqual(restored.context.snapshot, old["context"]["snapshot"])
        self.assertEqual(restored.state.hosts["127.0.0.1"].ports[80]["service"], "http")
        self.assertEqual(len(restored.ledger.items), 1)
        self.assertEqual(restored.usage.input_tokens, 100)
        self.assertEqual(restored.usage_gaps, chat.usage_gaps)
        self.assertEqual(backend.inputs, [], "恢复不得发出模型请求")
        restored.ask("继续核对")
        self.assertEqual(backend.inputs[0][:len(old["messages"])], old["messages"])
        self.assertEqual(restored.turns, 2)
        self.assertEqual(restored.usage.input_tokens, 200)
        self.assertEqual(restored.context.events[-1]["reason"], "append")
        self.assertTrue(restored.audit.verify()[0])

    def test_recent_list_is_project_scoped_and_does_not_store_provider_configuration(self):
        chat = self.chat()
        chat.ask("第一条对话标题")
        rows = self.store.list()
        self.assertEqual(rows[0]["title"], "第一条对话标题")
        self.assertEqual(rows[0]["turns"], 1)
        other = ChatStore(self.config.workspace_dir, self.root / "another-project")
        self.assertEqual(other.list(), [])
        with self.assertRaises(ValueError):
            other.load(chat.run_id)
        self.assertNotIn("api_key", json.dumps(self.store.load(chat.run_id)))
        with self.assertRaises(ValueError):
            self.store.load("../outside")

    def test_active_chat_cannot_be_opened_twice_and_lock_releases_on_close(self):
        chat = self.chat()
        chat.ask("保持会话占用")
        with self.assertRaisesRegex(ValueError, "另一个 Trina"):
            ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=self.backend())
        chat.close()
        restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=self.backend())
        restored.close()

    def test_same_second_new_chats_get_distinct_ids_and_restore_uses_current_guard(self):
        with patch("trina.brain.chat.time.strftime", return_value="fixed-time"):
            first, second = self.chat(), self.chat()
        self.assertNotEqual(first.run_id, second.run_id)
        first.ask("恢复时仍按当前配置执行")
        first.close()
        self.config.scope.trust_mode = "restricted"
        restored = ChatAgent.resume(self.config, first.run_id, registry=ToolRegistry([]), backend=self.backend())
        self.addCleanup(restored.close)
        self.assertFalse(restored.guard.check_target("203.0.113.10").allowed)
        self.assertTrue(restored.guard.check_target("127.0.0.1").allowed)

    def test_resume_reads_latest_snapshot_after_exclusive_lock(self):
        chat = self.chat()
        chat.ask("旧标题")
        chat.close()
        acquire = ChatStore.acquire
        def save_before_acquire(store, run_id):
            data = store.load(run_id)
            data["title"] = "另一实例关闭时的最后保存"
            store.save(data)
            return acquire(store, run_id)
        with patch.object(ChatStore, "acquire", new=save_before_acquire):
            restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=self.backend())
        self.addCleanup(restored.close)
        self.assertEqual(restored.title, "另一实例关闭时的最后保存")

    def test_corrupt_audit_refuses_resume_without_overwriting_history(self):
        chat = self.chat()
        chat.ask("校验审计链")
        chat.close()
        before = self.store.load(chat.run_id)
        path = self.config.audit_dir / f"{chat.run_id}.jsonl"
        records = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(records[0])
        row["event"] = "tampered"
        records[0] = json.dumps(row)
        path.write_text("\n".join(records) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "哈希链损坏"):
            ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=self.backend())
        self.assertEqual(self.store.load(chat.run_id), before)
        lock = self.store.acquire(chat.run_id)
        lock.close()

    def test_restore_does_not_replay_executed_tool_or_revive_background_job(self):
        calls = []
        class Probe(Tool):
            spec = ToolSpec(name="probe", summary="真实离线调用", action_level="detect")
            def run(self, ctx):
                calls.append("executed")
                return ToolResult(summary="执行结束")
        backend = self.backend()
        script = iter(['{"action":"probe","params":{}}', "执行结束"])
        backend.complete_messages = lambda *args, **kwargs: LLMResponse(text=next(script))
        chat = ChatAgent(self.config, registry=ToolRegistry([Probe()]), backend=backend)
        chat.ask("执行一次离线工具")
        chat.close()
        data = self.store.load(chat.run_id)
        data["in_flight"] = True
        data["state"]["runtime_status"] = {"background_jobs": [{"id": "old-job", "status": "running"}]}
        self.store.save(data)
        restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([Probe()]), backend=self.backend())
        self.addCleanup(restored.close)
        self.assertEqual(calls, ["executed"])
        self.assertEqual(restored.ctx.extra["background_jobs"].snapshot(), [])
        self.assertIn("不自动重放", restored._messages[-1]["content"])
        self.assertEqual(restored.controller.checks_used, 1)
        restored.ask("描述结果")
        self.assertEqual(calls, ["executed"])

    def test_failed_sqlite_transaction_keeps_previous_snapshot(self):
        chat = self.chat()
        chat.ask("先保存这条消息")
        before = self.store.load(chat.run_id)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_save BEFORE UPDATE ON chats BEGIN SELECT RAISE(ABORT,'test disk failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                chat.ask("不应覆盖旧快照")
            self.assertEqual(self.store.load(chat.run_id), before)
        finally:
            with self.store.connection() as db:
                db.execute("DROP TRIGGER fail_save")

    def test_console_switches_by_number_and_restores_usage_without_model_call(self):
        first = self.chat()
        first.ask("历史会话 A")
        first.close()
        backend = self.backend()
        output = []
        console = Console(self.config, backend=backend, registry=ToolRegistry([]), input_fn=lambda _: "", output=output.append)
        current = self.chat()
        current.ask("当前会话 B")
        console.chat = current
        target_number = next(i for i, row in enumerate(self.store.list(), 1) if row["id"] == first.run_id)
        console.handle(f"/resume {target_number}")
        self.addCleanup(console.chat.close)
        self.assertEqual(console.chat.run_id, first.run_id)
        self.assertTrue(current._closed)
        self.assertEqual(console.view.usage.input_tokens, 100)
        self.assertEqual(backend.inputs, [])
        self.assertIn("历史会话 A", "\n".join(output))
        self.assertEqual(console._completion_values("resume", [])[0].value, first.run_id)

    def test_corrupt_target_keeps_current_chat_and_running_resume_does_not_unpause(self):
        target = self.chat()
        target.ask("目标对话")
        target.close()
        current = self.chat()
        current.ask("当前对话")
        output = []
        console = Console(self.config, backend=self.backend(), registry=ToolRegistry([]), output=output.append)
        console.chat = current
        with self.store.connection() as db:
            db.execute("UPDATE chats SET payload='{}' WHERE id=?", (target.run_id,))
        console.handle(f"/resume {target.run_id}")
        self.assertIs(console.chat, current)
        self.assertFalse(current._closed)
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        console.session.pause_event.set()
        console.handle("/resume")
        self.assertTrue(console.session.pause_event.is_set())
        console.handle("/unpause")
        self.assertFalse(console.session.pause_event.is_set())

    def test_selector_is_searchable_and_cancel_keeps_current_chat(self):
        target = self.chat()
        target.ask("按标题搜索的历史对话")
        target.close()
        current = self.chat()
        current.ask("当前内容")
        console = Console(self.config, backend=self.backend(), registry=ToolRegistry([]), output=lambda _: None)
        console._custom_output = False
        console.chat = current
        with patch("trina.interactive.enhanced_input_available", return_value=True), patch("trina.console_ui.select_option", return_value=None) as picker:
            console.handle("/resume")
            self.assertIn("搜索", picker.call_args.kwargs["subtitle"])
            self.assertTrue(any(item.value == target.run_id for item in picker.call_args.args[1]))
        self.assertIs(console.chat, current)

    def test_list_last_and_prefix_switch_without_rendering_tool_json_as_answer(self):
        target = self.chat(run_id="chat-target")
        target.ask("查看旧工具记录")
        target.history.insert(1, {"role": "assistant", "content": '{"action":"probe","params":{}}'})
        target.close()
        current = self.chat(run_id="chat-current")
        current.ask("当前内容")
        output = []
        console = Console(self.config, backend=self.backend(), registry=ToolRegistry([]), output=output.append)
        console.chat = current
        console.handle("/resume list")
        self.assertIs(console.chat, current)
        console.handle("/resume chat-")
        self.assertIs(console.chat, current, "不唯一的前缀不能切换")
        console.handle("/resume last")
        self.addCleanup(lambda: console.chat.close())
        self.assertEqual(console.chat.run_id, target.run_id)
        self.assertIn("执行记录", "\n".join(output))
        self.assertNotIn('"action"', "\n".join(output))
        console.handle("/resume chat-cur")
        self.assertEqual(console.chat.run_id, current.run_id)

    def test_compacted_chat_resumes_revision_and_preserves_original_instructions(self):
        chat = self.chat()
        for i in range(8):
            chat.ask(f"测试约束 {i}")
        chat.context.prepare(chat._messages, force=True)
        self.assertGreater(chat.context.epoch, 0)
        chat.close()
        restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=self.backend())
        self.addCleanup(restored.close)
        self.assertEqual(restored.context.instructions, chat.context.instructions)
        self.assertEqual(restored.context.epoch, chat.context.epoch)
        revision = restored.context.revision
        restored.ask("继续")
        self.assertEqual(restored.context.revision, revision + 1)


if __name__ == "__main__":
    unittest.main()
