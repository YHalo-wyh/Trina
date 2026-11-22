"""历史对话跨实例恢复、切换与失败保护。"""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
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
        backend.tool_inputs = []
        def complete(messages, **kwargs):
            backend.inputs.append(copy.deepcopy(messages))
            backend.tool_inputs.append({schema["function"]["name"] for schema in kwargs.get("tools", [])})
            return LLMResponse(text="已收到，保留上下文", usage=LLMUsage(input_tokens=100, output_tokens=5, tokens_reported=True))
        backend.complete_messages = complete
        return backend

    def chat(self, **kwargs):
        chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=self.backend(), **kwargs)
        self.addCleanup(chat.close)
        return chat

    def _online_chat(self, **kwargs):
        from trina.llm.openai_backend import OpenAICompatBackend

        backend = OpenAICompatBackend(LLMConfig(model="deepseek-v4.1-flash", reasoning_effort="max"))
        backend.complete_messages = Mock(return_value=LLMResponse(text="已收到"))
        chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=backend, **kwargs)
        self.addCleanup(chat.close)
        return chat, backend

    def test_online_title_runs_after_first_reply_and_preserves_newer_turns(self):
        started, release, saved = threading.Event(), threading.Event(), threading.Event()
        chat, main = self._online_chat(on_event=lambda event: saved.set() if event.kind == "chat.title" else None)

        def title_reply(messages, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("主对话等待了标题请求")
            return LLMResponse(text="<title>修正历史列表</title>")

        title_backend = SimpleNamespace(complete_messages=Mock(side_effect=title_reply))
        with patch("trina.brain.chat.make_backend", return_value=title_backend) as factory:
            try:
                chat.ask("你好")
                self.assertEqual(chat.title, "新对话")
                factory.assert_not_called()
                chat.ask("请帮我修复历史面板。再补一个回归用例")
                self.assertTrue(started.wait(2))
                self.assertEqual(chat.title, "修复历史面板")
                self.assertEqual(main.complete_messages.call_count, 2)
                chat.ask("补充一个空列表用例")
            finally:
                release.set()
            self.assertTrue(saved.wait(3))
        self.assertEqual(chat.title, "修正历史列表")
        entry = self.store.load(chat.run_id)
        self.assertEqual((entry["title"], entry["turns"]), ("修正历史列表", 3))
        self.assertEqual(entry["history"][-2]["content"], "补充一个空列表用例")
        self.assertEqual(self.store.list()[0]["title"], "修正历史列表")
        title_backend.complete_messages.assert_called_once()
        title_messages = title_backend.complete_messages.call_args.args[0]
        self.assertEqual([item["role"] for item in title_messages], ["system", "user"])
        self.assertEqual(title_messages[-1]["content"], "请帮我修复历史面板。再补一个回归用例")
        self.assertEqual(title_backend.complete_messages.call_args.kwargs, {"max_tokens": 96})
        self.assertEqual(factory.call_args.args[0].reasoning_effort, "off")
        self.assertEqual(main.config.reasoning_effort, "max")

    def test_late_title_does_not_write_to_closed_chat(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        chat, _ = self._online_chat()
        generate = chat._generate_title

        def watched(*args):
            try:
                generate(*args)
            finally:
                finished.set()

        def title_reply(*args, **kwargs):
            started.set()
            release.wait(5)
            return LLMResponse(text="<title>迟到的标题</title>")

        chat._generate_title = watched
        with patch("trina.brain.chat.make_backend",
                   return_value=SimpleNamespace(complete_messages=title_reply)):
            try:
                chat.ask("整理单元测试")
                self.assertTrue(started.wait(2))
                chat.close()
            finally:
                release.set()
            self.assertTrue(finished.wait(3))
        self.assertEqual(self.store.load(chat.run_id)["title"], "整理单元测试")
        self.assertEqual(chat.events.replay(kinds=("chat.title",)), [])

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

    def test_code_chat_restores_the_same_tools_in_native_and_legacy_records(self):
        import re
        from trina.tool_names import internal_tool_name
        from trina.tools import build_registry

        registry = build_registry()
        for protocol in ("native", "json"):
            config = self.config.with_overrides(llm__tool_protocol=protocol)
            backend = self.backend()
            original = ChatAgent(config, registry=registry, backend=backend)
            self.addCleanup(original.close)
            original.ask("修复 Python 代码并运行测试")
            initial = set(original.ctx.extra["tool_allowlist"])
            self.assertIn("workspace_read", initial)
            self.assertNotIn("worker_tasks", initial)
            original.close()
            saved = self.store.load(original.run_id)
            self.assertEqual(set(saved["tool_allowlist"]), initial)
            for legacy in (False, True):
                with self.subTest(protocol=protocol, legacy=legacy):
                    if legacy:
                        saved.pop("tool_allowlist")
                        saved["messages"][1]["content"] = re.sub(
                            r"\b(?:read|write|shell)\b", lambda match: internal_tool_name(match.group(0)),
                            saved["messages"][1]["content"],
                        )
                        self.store.save(saved)
                    replay = self.backend()
                    restored = ChatAgent.resume(config, original.run_id, registry=registry, backend=replay)
                    try:
                        restored.ask("继续核对代码")
                        self.assertEqual(restored.ctx.extra["tool_allowlist"], initial)
                        self.assertEqual(replay.tool_inputs, backend.tool_inputs)
                    finally:
                        restored.close()
            reduced = ToolRegistry([registry.get("workspace_read")])
            restored = ChatAgent.resume(config, original.run_id, registry=reduced, backend=self.backend())
            try:
                self.assertEqual(restored.ctx.extra["tool_allowlist"], {"workspace_read"})
                self.assertEqual(set(self.store.load(original.run_id)["tool_allowlist"]), initial)
            finally:
                restored.close()

    def test_model_can_use_read_name_in_native_and_json_protocols(self):
        from trina.llm.base import ToolCall
        from trina.tools import build_registry

        registry = build_registry(["workspace"])
        for protocol in ("native", "json"):
            with self.subTest(protocol=protocol):
                backend = self.backend()
                params = {"path": "sample.txt", "root": "run"}
                first = (LLMResponse(tool_calls=[ToolCall("sample", "read", json.dumps(params))]) if protocol == "native"
                         else LLMResponse(text=json.dumps({"action": "read", "params": params})))
                backend.complete_messages = Mock(side_effect=[first, LLMResponse(text="读取完成")])
                config = self.config.with_overrides(llm__tool_protocol=protocol)
                chat = ChatAgent(config, registry=registry, backend=backend)
                try:
                    (chat.workspace / "sample.txt").write_text("alias sample", encoding="utf-8")
                    self.assertEqual(chat.ask("读取代码文件并汇报结果"), "读取完成")
                    request = backend.complete_messages.call_args.args[0]
                    self.assertTrue(any("alias sample" in item["content"] for item in request))
                    if protocol == "native":
                        tools = backend.complete_messages.call_args.kwargs["tools"]
                        self.assertIn("read", {item["function"]["name"] for item in tools})
                finally:
                    chat.close()

    def test_commander_resume_keeps_an_advertised_worker_tool_optional(self):
        from trina.tools import build_registry

        registry = build_registry()
        config = self.config.with_overrides(llm__tool_protocol="native")
        backend = self.backend()
        first = Console(config, registry=registry, backend=backend, output=lambda _: None, autostart_lab=False)
        self.addCleanup(first._cleanup)
        chat = first._ensure_chat()
        chat.ask("把交接纪要整理成摘要")
        self.assertIn("worker_tasks", backend.tool_inputs[0])
        self.assertIsNone(first.subworkers.runtime)
        run_id = chat.run_id
        first._cleanup()

        backend = self.backend()
        second = Console(config, registry=registry, backend=backend, output=lambda _: None, autostart_lab=False)
        self.addCleanup(second._cleanup)
        second.handle(f"/resume {run_id}")
        second._ensure_chat().ask("继续整理")
        self.assertIn("worker_tasks", backend.tool_inputs[0])
        self.assertIsNone(second.subworkers.runtime)

    def test_legacy_text_only_session_resumes_as_normal_chat(self):
        chat = self.chat()
        chat.ask("记录会议要点")
        chat.close()
        saved = self.store.load(chat.run_id)
        saved["text_messages"], saved["messages"] = saved["history"].copy(), []
        self.store.save(saved)
        backend = self.backend()
        restored = ChatAgent.resume(self.config, chat.run_id, registry=ToolRegistry([]), backend=backend)
        self.addCleanup(restored.close)
        self.assertEqual(backend.inputs, [])
        restored.ask("继续整理")
        self.assertEqual(backend.inputs[0][2:4], saved["text_messages"])
        self.assertEqual(len(backend.inputs), 1)

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
