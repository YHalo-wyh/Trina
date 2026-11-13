"""缓存参数、增量状态、继承检查点和供应商实际用量的行为验证。"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.brain.agent import PentestAgent
from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.conversation import ContextBudgetError, ConversationContext, state_projection
from trina.llm import LLMResponse, LLMUsage
from trina.llm.cache import CachePolicy
from trina.llm.openai_backend import OpenAICompatBackend, _parse_usage
from trina.llm.replay_backend import ReplayBackend
from trina.providers import ProfileStore, ProviderProfile
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools.registry import ToolContext, Tool, ToolRegistry, ToolResult, ToolSpec


class CacheRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
                                                  audit__dir=str(self.root / "logs"))
        self.audit = AuditLog.open(self.root, run_id="cache-runtime")
        self.ctx = ToolContext(config=self.config, guard=ScopeGuard(self.config.scope), audit=self.audit,
                               state=RunState(run_id="cache-test", target="127.0.0.1"), http=None,
                               workspace=self.root, tools=ToolRegistry([]))
        self.addCleanup(self.ctx.extra["background_jobs"].close)

    @staticmethod
    def base():
        return [{"role": "system", "content": "stable core"},
                {"role": "user", "content": "stable contract", "cache_scope": "stable"}]

    def long_history(self):
        messages = self.base()
        for index in range(20):
            messages.extend([{"role": "user", "content": f"question {index}"},
                             {"role": "assistant", "content": "observation " * 500}])
        return messages

    def test_state_delta_contains_only_changed_values_and_periodic_full_checkpoint(self):
        self.config.llm.state_checkpoint_interval = 3
        context = ConversationContext(self.ctx)
        state = {"facts": {"version": "1"}, "hypotheses": {"H03": {"status": "pending"}}}
        first = json.loads(context.state_update(state).splitlines()[-1])
        second = json.loads(context.state_update(state).splitlines()[-1])
        state["hypotheses"]["H03"]["status"] = "confirmed"
        del state["facts"]["version"]
        third = json.loads(context.state_update(state).splitlines()[-1])
        fourth = json.loads(context.state_update(state).splitlines()[-1])
        self.assertEqual(first["state"]["hypotheses"]["H03"]["status"], "pending")
        self.assertEqual(second["set"], {})
        self.assertEqual(third["set"], {"/hypotheses/H03/status": "confirmed"})
        self.assertEqual(third["remove"], ["/facts/version"])
        self.assertEqual(fourth["state"], state)

    def test_compaction_archives_original_and_preserves_constraints_facts_and_tool_pairs(self):
        context = ConversationContext(self.ctx)
        context.remember_user("仅修改测试文件，原规则保持")
        self.ctx.state.goal = "验证任务目标"
        projection = state_projection(self.ctx.state)
        context.state_update(projection)
        messages = self.base()
        for index in range(6):
            messages.extend([{"role": "assistant", "content": "", "reasoning_content": "original reasoning",
                              "tool_calls": [{"id": str(index), "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": str(index), "content": "result " * 1000},
                             {"role": "user", "content": f"continue {index}"}])
        original = copy.deepcopy(messages)
        context.prepare(messages, force=True)
        self.assertEqual(messages[:2], original[:2])
        self.assertLess(len(messages), len(original))
        checkpoint = json.loads(messages[2]["content"].splitlines()[-1])
        self.assertEqual(checkpoint["instructions"], ["仅修改测试文件，原规则保持"])
        self.assertEqual(checkpoint["state"], projection)
        raw = json.loads(Path(context.stats["last_archive"]).read_text(encoding="utf-8"))
        self.assertEqual(raw["messages"], original)
        self.assertEqual(raw["next_messages"], messages)
        declared = {call["id"] for row in messages for call in row.get("tool_calls", [])}
        observed = {row["tool_call_id"] for row in messages if row["role"] == "tool"}
        self.assertTrue(declared, "最近工具链必须保留，不能通过全部删除规避配对检查")
        self.assertEqual(declared, observed)
        self.assertTrue(self.audit.verify()[0])

    def test_archive_failure_keeps_original_messages_and_epoch(self):
        context = ConversationContext(self.ctx)
        context.state_update(state_projection(self.ctx.state))
        messages = self.long_history()
        original = copy.deepcopy(messages)
        with patch("trina.conversation.Path.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(ContextBudgetError):
                context.prepare(messages, force=True)
        self.assertEqual(messages, original)
        self.assertEqual(context.epoch, 0)

    def test_new_context_in_same_directory_does_not_overwrite_existing_archive(self):
        first = ConversationContext(self.ctx)
        first.state_update(state_projection(self.ctx.state))
        first.prepare(self.long_history(), force=True)
        original = Path(first.stats["last_archive"]).read_bytes()
        second = ConversationContext(self.ctx)
        second.state_update(state_projection(self.ctx.state))
        second.prepare(self.long_history(), force=True)
        self.assertNotEqual(first.stats["last_archive"], second.stats["last_archive"])
        self.assertEqual(Path(first.stats["last_archive"]).read_bytes(), original)

    def test_failed_requests_without_assistant_reply_can_still_compact(self):
        self.config.llm.context_window = 32768  # 固定压力预算，不依赖模型默认窗口。
        context = ConversationContext(self.ctx)
        context.remember_user("原始目标")
        context.state_update(state_projection(self.ctx.state))
        messages = self.base() + [{"role": "user", "content": f"runtime {index}: " + "x" * 3000} for index in range(20)]
        latest = copy.deepcopy(messages[-1])
        context.prepare(messages)
        self.assertGreater(context.epoch, 0)
        self.assertEqual(messages[-1], latest)

    def test_impossible_budget_keeps_latest_request_and_does_not_silently_drop_instructions(self):
        context = ConversationContext(self.ctx)
        context.remember_user("不可遗忘的原始约束 " * 10000)
        context.state_update(state_projection(self.ctx.state))
        self.config.llm.context_window = 8192
        messages = self.long_history()
        original = copy.deepcopy(messages)
        with self.assertRaises(ContextBudgetError):
            context.prepare(messages)
        self.assertEqual(messages, original)
        self.assertEqual(context.epoch, 0)

    def test_diagnostics_distinguish_growth_rewrite_and_settings_without_inventing_miss_cause(self):
        context = ConversationContext(self.ctx)
        messages = self.base()
        self.assertEqual(context.before_request(messages)["reason"], "cold")
        messages.append({"role": "user", "content": "new suffix"})
        appended = context.before_request(messages)
        usage = LLMUsage(input_tokens=1000, cache_read_tokens=770, input_reported=True, cache_read_reported=True)
        context.after_response(appended, LLMResponse(text="ok", usage=usage))
        self.assertEqual(appended["reason"], "append")
        self.assertEqual(appended["uncached_input_tokens"], 230)
        self.assertEqual(appended["appended_visible_bytes"], appended["suffix_visible_bytes"])
        self.assertNotIn("churn_tokens", appended)
        messages[0]["content"] = "changed core"
        changed = context.before_request(messages)
        self.assertEqual(changed["reason"], "prefix_changed")
        self.assertIsNone(changed["appended_visible_bytes"])
        self.config.llm.reasoning_effort = "high"
        self.assertEqual(context.before_request(messages)["reason"], "request_settings_changed")
        context.compacted = True
        self.assertEqual(context.before_request(messages)["reason"], "compaction")

    def test_chat_automatically_compacts_and_keeps_worker_contexts_isolated(self):
        self.config.llm.context_window, self.config.llm.max_tokens = 10000, 1024
        backend = ReplayBackend(LLMConfig(backend="replay"))
        requests = []
        def complete(messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            return LLMResponse(text="response " * 500)
        backend.complete_messages = complete
        chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=backend)
        other = ChatAgent(self.config, registry=ToolRegistry([]), backend=backend, run_id="other-worker")
        self.addCleanup(chat.close)
        self.addCleanup(other.close)
        for index in range(15):
            chat.ask(f"仅处理题目 A，补充 {index}")
        self.assertGreater(chat.context.epoch, 0)
        self.assertEqual(other.context.epoch, 0)
        self.assertEqual(other.context.instructions, [])
        self.assertTrue(any(item["reason"] == "compaction" for item in chat.context.events))
        self.assertEqual(requests[-1][0], requests[0][0])
        self.assertIn("Agent 状态栏", requests[-1][-1]["content"])
        self.assertEqual(chat.context.instructions[0], "仅处理题目 A，补充 0")

    def test_task_agent_uses_same_compaction_and_persists_diagnostics(self):
        self.config.llm.context_window, self.config.llm.max_tokens = 10000, 1024
        self.config.policy.stop_on_no_new_evidence = 30
        class Observe(Tool):
            spec = ToolSpec(name="observe", summary="离线检查", action_level="detect")
            def run(self, ctx):
                return ToolResult(summary="actual observation " * 200)
        backend = ReplayBackend(LLMConfig(backend="replay"))
        backend.complete_messages = lambda messages, **kwargs: LLMResponse(text='{"action":"observe","params":{}}')
        state = PentestAgent(self.config, registry=ToolRegistry([Observe()]), backend=backend).run(
            "127.0.0.1", goal="验证 SQL 注入", max_steps=8)
        self.assertGreater(state.context_stats["compactions"], 0)
        self.assertTrue(list(self.config.workspace_dir.rglob("epoch-*.json")))
        saved = json.loads(Path(state.artifacts["state_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["context_stats"], state.context_stats)

    def test_layered_and_legacy_prompts_keep_same_fixed_core_and_contract_boundaries(self):
        cores = []
        for layered in (False, True):
            self.config.llm.prompt_layers = layered
            backend = ReplayBackend(LLMConfig(backend="replay"))
            captured = []
            backend.complete_messages = lambda messages, **kwargs: captured.append(copy.deepcopy(messages)) or LLMResponse(text="ok")
            chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=backend, run_id=f"layer-{layered}")
            self.addCleanup(chat.close)
            chat.ask("first")
            chat.ask("second")
            self.assertEqual(captured[1][:len(captured[0])], captured[0])
            self.assertEqual(captured[0][1]["cache_scope"], "stable")
            cores.append(captured[0][0]["content"])
        self.assertIn("# Engineering", cores[0])
        self.assertIn("# Engineering", cores[1])

    def test_worker_cache_identity_depends_on_core_and_tool_set_not_task_or_dynamic_state(self):
        cfg = LLMConfig(base_url="https://api.openai.com/v1", model="gpt-6.1-sol")
        a = CachePolicy.for_request(cfg, self.base())
        b = CachePolicy.for_request(cfg, self.base() + [{"role": "user", "content": "different task"}])
        self.assertEqual(a.stable_prefix_id, b.stable_prefix_id)
        c = CachePolicy.for_request(cfg, self.base(), tools=[{"name": "different-tool"}])
        self.assertNotEqual(a.stable_prefix_id, c.stable_prefix_id)

    def test_environment_address_is_used_for_official_cache_policy(self):
        with patch.dict(os.environ, {"TRINA_LLM_BASE_URL": "https://api.openai.com/v1"}):
            backend = OpenAICompatBackend(LLMConfig(base_url="", model="gpt-6.1-sol"), key_override="")
        with patch.object(backend, "_complete_stream", return_value=LLMResponse(text="ok")) as send:
            backend.complete_messages(self.base())
        self.assertIn("prompt_cache_options", send.call_args.args[1])

    def test_context_and_compact_commands_use_current_chat_without_model_calls(self):
        from trina.interactive import Console
        self.config.llm.backend = "replay"
        with patch.dict(os.environ, {"TRINA_PROVIDERS_FILE": str(self.root / "providers.json")}):
            output = []
            console = Console(self.config, input_fn=lambda _: "", output=output.append)
            backend = ReplayBackend(LLMConfig(backend="replay"))
            backend.complete_messages = lambda messages, **kwargs: LLMResponse(text="response " * 300)
            chat = ChatAgent(self.config, registry=ToolRegistry([]), backend=backend)
            self.addCleanup(chat.close)
            console.chat = chat
            for index in range(6):
                chat.ask(f"request {index}")
            calls = chat.state.counters["llm_calls"]
            console.handle("/compact")
            console.handle("/context")
        self.assertGreater(chat.context.epoch, 0)
        self.assertEqual(chat.state.counters["llm_calls"], calls)
        self.assertIn("上下文与缓存", "\n".join(output))

    def test_provider_payloads_use_native_fields_and_do_not_mutate_internal_messages(self):
        cases = [("chat", "https://api.openai.com/v1", "gpt-6.1-sol", "30m"),
                 ("responses", "https://api.openai.com/v1", "gpt-6.1-sol", "12h"),
                 ("anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-6", "1h")]
        for api, url, model, ttl in cases:
            with self.subTest(api=api):
                cfg = LLMConfig(api=api, base_url=url, model=model, cache_ttl=ttl, cache_mode="explicit")
                backend = OpenAICompatBackend(cfg, key_override="")
                messages = self.base()
                messages[0]["trace_private"] = "must-not-leak"
                original = copy.deepcopy(messages)
                _, payload = backend._build_payload(messages, temperature=None, max_tokens=None, stream=True)
                policy = CachePolicy.for_request(cfg, messages)
                payload = policy.apply(payload, messages, api)
                self.assertNotIn("cache_scope", json.dumps(payload))
                self.assertNotIn("trace_private", json.dumps(payload))
                self.assertEqual(messages, original)
                if api == "anthropic":
                    self.assertEqual(payload["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
                    self.assertNotIn("prompt_cache_key", payload)
                else:
                    self.assertEqual(payload["prompt_cache_options"], {"mode": "explicit", "ttl": ttl})
                    rows = payload["input"] if api == "responses" else payload["messages"]
                    self.assertEqual(rows[1]["content"][0]["prompt_cache_breakpoint"], {"mode": "explicit"})

    def test_passive_proxy_deepseek_and_older_openai_have_separate_policies(self):
        for url in ("https://relay.example/v1", "https://api.deepseek.com/v1"):
            cfg = LLMConfig(base_url=url)
            policy = CachePolicy.for_request(cfg, self.base())
            self.assertEqual(policy.apply({"messages": self.base()}, self.base(), "chat"), {"messages": self.base()})
        cfg = LLMConfig(base_url="https://api.openai.com/v1", model="gpt-4.1", cache_ttl="24h")
        payload = CachePolicy.for_request(cfg, self.base()).apply({}, self.base(), "chat")
        self.assertEqual(payload["prompt_cache_retention"], "24h")
        self.assertNotIn("prompt_cache_options", payload)
        cfg.cache_ttl, cfg.model = "12h", "gpt-6.1-sol"
        with self.assertRaises(ValueError):
            CachePolicy.for_request(cfg, self.base()).apply({}, self.base(), "chat")

    def test_real_http_proxy_rejection_negotiates_once_without_repeating_generation(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                if "prompt_cache_options" in payload:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"Unsupported parameter prompt_cache_options"}}')
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = LLMConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1", cache_features=["openai_options"])
            backend = OpenAICompatBackend(cfg, key_override="")
            events = []
            response = backend.complete_messages(self.base(), on_event=events.append)
            again = backend.complete_messages(self.base())
            self.assertTrue(response.ok, response.error)
            self.assertTrue(again.ok)
            self.assertEqual(len(requests), 3)
            self.assertNotIn("prompt_cache_options", requests[-1])
            self.assertFalse(any(event.type == "error" for event in events))
            self.assertEqual(response.cache_diagnostic["source"], "server_rejected_fallback")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_auth_error_is_not_treated_as_cache_capability_rejection(self):
        backend = OpenAICompatBackend(LLMConfig(base_url="https://relay.example/v1", cache_features=["openai_options"]), key_override="")
        with patch.object(backend, "_complete_stream", return_value=LLMResponse(ok=False, error="HTTP 401: invalid key")) as send:
            response = backend.complete_messages(self.base())
        self.assertFalse(response.ok)
        self.assertEqual(send.call_count, 1)

    def test_profile_cache_capabilities_follow_connection_and_survive_reload(self):
        store = ProfileStore(self.root / "profiles.json")
        store.profiles = {"a": ProviderProfile("a", "custom", "https://a.example/v1", "m", cache_features=["openai_options"], cache_ttl="30m"),
                          "b": ProviderProfile("b", "custom", "https://b.example/v1", "m")}
        store.active = "a"
        store.save()
        reloaded = ProfileStore(store.path)
        reloaded.apply(self.config.llm, "a")
        self.assertEqual(self.config.llm.cache_features, ["openai_options"])
        reloaded.apply(self.config.llm, "b")
        self.assertEqual(self.config.llm.cache_features, [])
        self.assertEqual(self.config.llm.cache_ttl, "")

    def test_deepseek_hit_and_miss_can_supply_total_without_double_counting(self):
        usage = _parse_usage({"prompt_cache_hit_tokens": 770, "prompt_cache_miss_tokens": 230, "completion_tokens": 10}, "chat", "deepseek-chat")
        self.assertEqual(usage.input_tokens, 1000)
        self.assertTrue(usage.input_reported)
        self.assertAlmostEqual(usage.to_dict()["cache_hit_rate"], .77)
        raw = usage.to_dict()
        restored = RunState.from_dict(self.ctx.state.to_dict())
        self.assertEqual(restored.context_stats, {})
        self.assertEqual(raw["total_tokens"], 1010)


if __name__ == "__main__":
    unittest.main()
