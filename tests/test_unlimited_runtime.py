"""无限任务的真实循环、显式覆盖、租约释放与入口默认值回归。"""
from __future__ import annotations

import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from trina.brain.agent import PentestAgent
from trina.brain.chat import ChatAgent
from trina.brain.runtime import ActionOutcome, AgentRuntime
from trina.capabilities.broker import CapabilityFabric, CapabilityLeaseError
from trina.capabilities.catalog import CapabilityCatalog
from trina.capabilities.runtime import CapabilityRuntime
from trina.cli import build_parser
from trina.config import ConfigError, TrinaConfig
from trina.coordination import CoordinationRuntime
from trina.intent_planner import FactIntentPlanner
from trina.llm.base import LLMBackend, LLMResponse, ToolCall
from trina.policy import AuthorizationController, PolicyRegistry
from trina.swarm import TaskSpec, validate_tasks
from trina.tools.registry import Tool, ToolRegistry, ToolResult, ToolSpec


class Probe(Tool):
    spec = ToolSpec(name="unlimited_probe", summary="无限循环回归探针", category="recon",
                    action_level="detect", params={})

    def run(self, ctx):
        return ToolResult(summary="实际探针完成")


class SequenceBackend(LLMBackend):
    supports_native_tools = True

    def __init__(self, config, count=45):
        super().__init__(config)
        self.count, self.calls = count, 0

    def complete(self, *args, **kwargs):
        return self.complete_messages([], **kwargs)

    def complete_messages(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.count:
            call = ToolCall(str(self.calls), "unlimited_probe", "{}")
        else:
            call = ToolCall(str(self.calls), "finish", json.dumps({"summary": "已完成全部探针"}))
        return LLMResponse(tool_calls=[call])


class UnlimitedRuntimeTests(unittest.TestCase):
    def config(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        return TrinaConfig().with_overrides(run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"), report__output_dir=str(root / "reports"))

    def test_unlimited_batch_crosses_old_action_and_round_limits_and_pairs_calls(self):
        responses = [LLMResponse(tool_calls=[ToolCall(str(i), "probe", "{}") for i in range(65)])]
        responses += [LLMResponse(text="again") for _ in range(45)] + [None]
        messages = []
        runtime = AgentRuntime(max_steps=0, messages=messages, request=lambda *_: responses.pop(0),
            execute=lambda *_args, **_kwargs: ActionOutcome("ok"), decode_text=lambda _: None,
            say=lambda *_args: ActionOutcome(), native=lambda: True)
        runtime.run()
        self.assertEqual(runtime.steps, 65)
        self.assertGreater(runtime.rounds, 40)
        self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 65)

    def test_chat_continues_past_twelve_without_user_continuation(self):
        config = self.config()
        backend = SequenceBackend(config.llm, count=15)
        chat = ChatAgent(config, backend=backend, registry=ToolRegistry([Probe()]))
        self.addCleanup(chat.close)
        self.assertEqual(chat.ask("完成全部探针"), "已完成全部探针")
        self.assertEqual(backend.calls, 16)
        self.assertEqual(chat.state.counters["tool_calls"], 15)

    def test_explicit_zero_overrides_finite_task_config_and_has_no_deadline(self):
        config = self.config().with_overrides(llm__max_steps=1, llm__max_seconds=1)
        backend = SequenceBackend(config.llm)
        output = []
        agent = PentestAgent(config, backend=backend, registry=ToolRegistry([Probe()]), logger=output.append)
        state = agent.run("127.0.0.1", goal="完成全部探针", max_steps=0, max_seconds=0)
        self.assertEqual(state.counters["tool_calls"], 45)
        self.assertIsNone(agent._ctx.deadline)
        self.assertTrue(any("/不限]" in line for line in output))

    def test_unlimited_leases_still_revoke_and_do_not_expire(self):
        registry = ToolRegistry([Probe()])
        fabric = CapabilityFabric(CapabilityCatalog.from_registry(registry), CapabilityRuntime(registry))
        lease = fabric.lease("worker", "task", "probe", ttl=0)
        lease.check()
        self.assertEqual(lease.expires_at, 0)
        fabric.release(lease.id)
        with self.assertRaises(CapabilityLeaseError):
            lease.check()
        runtime = CoordinationRuntime([TaskSpec("task", "127.0.0.1", "检查")])
        runtime.claim("task", "worker", ttl=0)
        self.assertEqual(runtime.expire_leases(now=time.time() + 10**8), [])

    def test_defaults_and_successor_tasks_preserve_unlimited_budgets(self):
        config = TrinaConfig.load()
        self.assertEqual((config.llm.max_steps, config.llm.max_seconds,
                          config.subworker.max_steps, config.subworker.max_seconds), (0, 0, 0, 0))
        source = TaskSpec("base", "127.0.0.1", "分析")
        validate_tasks([source])
        proposals = FactIntentPlanner().propose(source, [{"id": "f", "kind": "asset",
                                                        "payload": {"ports": {"80": {}}}}])
        self.assertEqual((proposals[0].max_steps, proposals[0].max_seconds), (0, 0))
        parser = build_parser()
        self.assertEqual(parser.parse_args(["swarm", "--target", "127.0.0.1"]).max_steps, 0)
        self.assertEqual(parser.parse_args(["schedule", "add", "--target", "127.0.0.1", "--at", "+1m"]).max_steps, 0)
        controller = AuthorizationController(PolicyRegistry(), mode="free")
        self.assertFalse(any(controller.note_evidence(False) for _ in range(100)))
        for section in ("llm", "subworker"):
            for field in ("max_steps", "max_seconds"):
                with self.assertRaises(ConfigError):
                    config.with_overrides(**{f"{section}__{field}": -1})

    def test_bench_supports_unlimited_and_independently_finite_deadlines(self):
        from trina.bench.runner import BenchOptions, BenchmarkRunner
        options = BenchOptions()
        options.validate()
        runner = object.__new__(BenchmarkRunner)
        runner.options = options
        runner._outcome_lock = __import__("threading").RLock()
        runner._outcomes, runner._task_count = {}, 1
        self.assertIsNone(runner._attempt_budget(math.inf, math.inf, 0))
        self.assertGreater(runner._attempt_budget(math.inf, time.monotonic() + 30, 0), 0)
        self.assertGreater(runner._attempt_budget(time.monotonic() + 30, math.inf, 0), 0)
        self.assertEqual(runner._attempt_budget(time.monotonic() - 1, math.inf, 0), 0)


if __name__ == "__main__":
    unittest.main()
