"""统一 AgentRuntime 的批量调用、依赖、预算和标准消息配对。"""
from __future__ import annotations

import unittest

from trina.brain.runtime import ActionOutcome, AgentRuntime
from trina.config import TrinaConfig
from trina.llm import LLMResponse
from trina.llm.base import ToolCall


class AgentRuntimeTests(unittest.TestCase):
    @staticmethod
    def response(*calls: tuple[str, str, str]) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(call_id, name, arguments) for call_id, name, arguments in calls])

    def test_default_path_is_goal_driven_layered_and_periodic(self):
        config = TrinaConfig()
        self.assertEqual(config.run.phases, [])
        self.assertFalse(config.run.hypothesis_scheduler)
        self.assertEqual(config.run.flush_state_every, 5)
        self.assertEqual(config.policy.stop_on_no_new_evidence, 0)
        self.assertTrue(config.llm.prompt_layers)

    def test_batch_executes_all_calls_in_one_decision_and_pairs_every_id(self):
        messages: list[dict] = []
        requests = [
            self.response(("c1", "read", '{"path":"a"}'), ("c2", "read", '{"path":"b"}')),
            self.response(("c3", "finish", '{"status":"success","summary":"done"}')),
        ]
        executed: list[str] = []

        def execute(action, index, response, **kwargs):
            del index, response, kwargs
            if "final" in action:
                return ActionOutcome(observation="done", answer="done", stop=True)
            executed.append(action["params"]["path"])
            return ActionOutcome(observation="ok")

        runtime = AgentRuntime(max_steps=4, messages=messages, request=lambda *_: requests.pop(0),
                               execute=execute, decode_text=lambda _: None, native=lambda: True)
        outcome = runtime.run()
        self.assertEqual(executed, ["a", "b"])
        self.assertEqual(runtime.rounds, 2)
        self.assertEqual(runtime.steps, 3)
        self.assertEqual(outcome.answer, "done")
        self.assertEqual([row["tool_call_id"] for row in messages if row["role"] == "tool"], ["c1", "c2", "c3"])

    def test_failed_dependency_is_skipped_without_consuming_action_budget(self):
        messages: list[dict] = []
        response = self.response(
            ("c1", "probe", "{}"),
            ("c2", "exploit", '{"_depends_on":[1]}'),
            ("c3", "read", "{}"),
        )
        executed: list[str] = []

        def execute(action, *_args, **_kwargs):
            executed.append(action["action"])
            return ActionOutcome(observation="failed", ok=False) if action["action"] == "probe" else ActionOutcome("ok")

        requests = [response, None]
        runtime = AgentRuntime(max_steps=3, messages=messages, request=lambda *_: requests.pop(0),
                               execute=execute, decode_text=lambda _: None, native=lambda: True)
        runtime.run()
        self.assertEqual(executed, ["probe", "read"])
        self.assertEqual(runtime.steps, 2)
        results = [row["content"] for row in messages if row["role"] == "tool"]
        self.assertIn("依赖调用失败", results[1])

    def test_stop_cancels_remaining_calls_but_keeps_protocol_complete(self):
        messages: list[dict] = []
        response = self.response(("c1", "one", "{}"), ("c2", "two", "{}"))
        calls = 0

        def execute(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return ActionOutcome("stop", stop=True)

        runtime = AgentRuntime(max_steps=3, messages=messages, request=lambda *_: response,
                               execute=execute, decode_text=lambda _: None, native=lambda: True)
        runtime.run()
        self.assertEqual(calls, 1)
        self.assertEqual([row["tool_call_id"] for row in messages if row["role"] == "tool"], ["c1", "c2"])
        self.assertIn("未执行", messages[-1]["content"])

    def test_finish_mixed_with_tool_rejects_entire_batch(self):
        messages: list[dict] = []
        requests = [self.response(
            ("c1", "read", "{}"),
            ("c2", "finish", '{"status":"success","summary":"guess"}'),
        ), None]
        executed: list[dict] = []
        runtime = AgentRuntime(max_steps=3, messages=messages, request=lambda *_: requests.pop(0),
                               execute=lambda action, *_args, **_kwargs: (
                                   executed.append(action) or ActionOutcome(str(action.get("error") or ""), ok=False)
                               ), decode_text=lambda _: None, native=lambda: True)
        runtime.run()
        self.assertTrue(all("error" in action for action in executed))
        self.assertEqual([row["tool_call_id"] for row in messages if row["role"] == "tool"], ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
