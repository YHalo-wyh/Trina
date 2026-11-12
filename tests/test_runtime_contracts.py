"""从旧专职咨询测试迁移的通用执行合同，移除功能后仍保留失败回归。"""
from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.brain.agent import PentestAgent
from trina.brain.chat import ChatAgent
from trina.brain.scheduler import ScheduleDecision
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMBackend, LLMResponse
from trina.policy import AuthorizationController, Budget, PolicyRegistry, RegisteredTarget, RegisteredTest
from trina.state import RunState
from trina.tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec


TARGET = "http://127.0.0.1:9099"


class ScriptBackend(LLMBackend):
    def __init__(self, script):
        super().__init__(LLMConfig(backend="replay"))
        self.script = iter(script)
        self.inputs = []
        self.after_response = None

    def complete(self, system, user, **kwargs):
        return self.complete_messages([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def complete_messages(self, messages, **kwargs):
        self.inputs.append([dict(item) for item in messages])
        value = next(self.script)
        result = self._tally(LLMResponse(text=json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value))
        if self.after_response:
            self.after_response()
        return result


class Probe(Tool):
    spec = ToolSpec(name="safe_probe", summary="离线侦察", category="recon", action_level="detect",
                    params={"target": ParamSpec("str", "目标")})
    calls = 0

    def run(self, ctx, target):
        self.calls += 1
        return ToolResult(summary="发现开放端口", facts={"ports": [{"host": "127.0.0.1", "port": 80}]})


def probe():
    return {"action": "safe_probe", "params": {"target": TARGET}}


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))
        self.tool = Probe()
        self.registry = ToolRegistry([self.tool])

    def test_atomic_save_keeps_previous_snapshot_when_replace_fails(self):
        path = self.root / "state.json"
        path.write_text('{"previous": true}', encoding="utf-8")
        agent = PentestAgent(self.cfg, registry=self.registry, backend=ScriptBackend([]))
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            agent._save_state(RunState(run_id="atomic", target=TARGET), self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"previous": True})
        self.assertFalse(list(self.root.glob("state-*.tmp")))

    def test_chat_failure_feedback_is_sent_back_to_model(self):
        backend = ScriptBackend([probe(), "请检查服务"])
        chat = ChatAgent(self.cfg, registry=self.registry, backend=backend)
        with patch.object(self.tool, "run", return_value=ToolResult(ok=False, error="connection refused")):
            chat.ask("检查端口")
        self.assertIn("先确认目标服务/权限/网络是否可用", json.dumps(backend.inputs[-1], ensure_ascii=False))

    def test_scheduler_consumes_live_controller_coverage(self):
        policy = PolicyRegistry()
        policy.targets = {"target": RegisteredTarget("target", (TARGET,))}
        policy.tests = {key: RegisteredTest(key, "target", "safe_probe", expect_finding="sqli") for key in ("first", "second")}
        controller = AuthorizationController(policy, mode="strict", budget=Budget())
        backend = ScriptBackend([probe() | {"test_id": "first"}, probe() | {"test_id": "second"}])
        state = PentestAgent(self.cfg, registry=self.registry, backend=backend, controller=controller).run(
            TARGET, goal="验证 SQL 注入", max_steps=2)
        self.assertIn('"test_id": "first"', backend.inputs[0][-1]["content"])
        self.assertIn('"test_id": "second"', backend.inputs[1][-1]["content"])
        self.assertEqual(state.coverage["executed"], ["first", "second"])

    def test_same_tool_with_wrong_test_id_is_not_compliance(self):
        decision = ScheduleDecision(hypothesis_id="", statement="验证指定接口", test_id="wanted", tool="safe_probe")
        self.assertFalse(decision.matches_action(test_id="other-location", tool="safe_probe"))
        self.assertTrue(decision.matches_action(test_id="wanted", tool="safe_probe"))

    def test_inflight_supplement_supersedes_candidate_for_chat_and_task(self):
        for mode in ("chat", "task"):
            with self.subTest(mode=mode):
                steers = queue.Queue()
                backend = ScriptBackend([probe(), "已更新" if mode == "chat" else {"final": {"summary": "已更新"}}])
                backend.after_response = lambda: steers.put("先不要执行探测") if len(backend.inputs) == 1 else None
                agent_type = ChatAgent if mode == "chat" else PentestAgent
                agent = agent_type(self.cfg, registry=self.registry, backend=backend, steer_queue=steers)
                agent.ask("检查目标") if mode == "chat" else agent.run(TARGET, max_steps=2)
                self.assertEqual(self.tool.calls, 0)
                self.assertIn("先不要执行探测", json.dumps(backend.inputs[1], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
