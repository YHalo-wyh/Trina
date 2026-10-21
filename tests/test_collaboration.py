"""专职 Agent 与主控的边界回归：建议不是事实、权限或隐式执行。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.brain.agent import PentestAgent
from trina.brain.chat import ChatAgent
from trina.brain.scheduler import ScheduleDecision
from trina.config import ConfigError, LLMConfig, TrinaConfig
from trina.llm import LLMBackend, LLMResponse
from trina.llm.replay_backend import ReplayBackend
from trina.policy import AuthorizationController, Budget, PolicyRegistry, RegisteredTarget, RegisteredTest
from trina.scope import ScopeGuard
from trina.state import Credential, RunState
from trina.tools import build_registry
from trina.tools.registry import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec
from trina.tools.specialists import ConsultSpecialist


TARGET = "http://127.0.0.1:9099"


class ScriptBackend(LLMBackend):
    """记录提示快照的确定性后端，不请求网络。"""

    def __init__(self, responses: list[dict | str]):
        super().__init__(LLMConfig(backend="replay"))
        self.responses = iter(responses)
        self.inputs: list[tuple[str, str, int | None]] = []

    def complete(self, system, user, *, temperature=None, max_tokens=None):
        self.inputs.append((system, user, max_tokens))
        response = next(self.responses)
        text = json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response
        return self._tally(LLMResponse(text=text))


class Probe(Tool):
    spec = ToolSpec(name="safe_probe", summary="离线侦察", category="recon", action_level="detect",
                    params={"target": ParamSpec("str", "目标")})

    def __init__(self):
        self.calls = 0

    def run(self, ctx, target):
        self.calls += 1
        return ToolResult(summary="发现开放端口", facts={"ports": [{"host": "127.0.0.1", "port": 80}]})


def consult(role: str = "recon") -> dict:
    return {"action": "consult_specialist", "params": {"target": TARGET, "role": role, "task": "检查下一步缺口"}}


def probe(target: str = TARGET) -> dict:
    return {"action": "safe_probe", "params": {"target": target}}


class TestCollaboration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = TrinaConfig.load().with_overrides(
            run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"),
            policy__mode="free", run__specialists=True,
        )
        self.tool = Probe()
        self.registry = ToolRegistry([self.tool, ConsultSpecialist()])

    def tearDown(self):
        self.tmp.cleanup()

    def context(self, backend: LLMBackend, *, run_id: str = "specialist") -> ToolContext:
        state = RunState(run_id=run_id, target=TARGET, goal="验证目标的实际攻击面")
        audit = AuditLog.open(self.root / "logs", run_id=run_id)
        guard = ScopeGuard(self.cfg.scope, audit=audit)
        ctx = ToolContext(config=self.cfg, guard=guard, audit=audit, state=state, http=None,
                          workspace=self.root, tools=self.registry)
        ctx.extra["llm_backend"] = backend
        return ctx

    def execute(self, ctx: ToolContext, **params):
        arguments = consult()["params"] | params
        return self.registry.execute("consult_specialist", arguments, ctx)

    def test_consultation_is_registered_and_can_be_hidden(self):
        registry = build_registry(["specialists"])
        self.assertIn("consult_specialist", registry.names())
        self.assertNotIn("consult_specialist", registry.prompt_block(exclude=("consult_specialist",)))
        self.assertEqual(registry.get("consult_specialist").spec.action_level, "detect")

    def test_advice_never_executes_or_mutates_facts(self):
        backend = ScriptBackend([probe()])
        ctx = self.context(backend)
        before = ctx.state.knowledge_digest()
        result = self.execute(ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["proposal"]["action"], "safe_probe")
        self.assertEqual(self.tool.calls, 0)
        self.assertEqual(ctx.state.knowledge_digest(), before)
        self.assertFalse(result.facts or result.evidence or ctx.state.flags)
        self.assertIn(self.cfg.scope_prompt_block(), backend.inputs[0][0])
        self.assertEqual(backend.inputs[0][2], self.cfg.run.specialist_max_tokens)
        self.assertTrue(ctx.audit.verify()[0])

    def test_disabled_feature_does_not_call_model(self):
        self.cfg.run.specialists = False
        backend = ScriptBackend([])
        result = self.execute(self.context(backend))
        self.assertFalse(result.ok)
        self.assertEqual(backend.inputs, [])

    def test_scope_denial_precedes_specialist_model_call(self):
        backend = ScriptBackend([])
        result = self.execute(self.context(backend), target="https://8.8.8.8")
        self.assertTrue(result.denied)
        self.assertEqual(backend.inputs, [])

    def test_unknown_role_and_oversized_task_are_rejected(self):
        backend = ScriptBackend([])
        ctx = self.context(backend)
        for params in ({"role": "admin"}, {"task": "x" * 1201}, {"task": "  "}):
            with self.subTest(params=list(params)):
                self.assertFalse(self.execute(ctx, **params).ok)
        self.assertEqual(backend.inputs, [])

    def test_invalid_response_consumes_budget(self):
        self.cfg.run.specialist_max_calls = 1
        backend = ScriptBackend(["无结构建议"])
        ctx = self.context(backend)
        self.assertFalse(self.execute(ctx).ok)
        second = self.execute(ctx)
        self.assertEqual(second.category, "budget")
        self.assertEqual(len(backend.inputs), 1)

    def test_recursion_unregistered_and_cross_role_tools_are_rejected(self):
        for index, proposal in enumerate((consult(), {"action": "shell", "params": {}},
                                          {"action": ["safe_probe"], "params": {}},
                                          {"action": "safe_probe", "params": "bad"})):
            with self.subTest(index=index):
                result = self.execute(self.context(ScriptBackend([proposal]), run_id=f"invalid-{index}"))
                self.assertFalse(result.ok)
                self.assertEqual(self.tool.calls, 0)

    def test_review_cannot_propose_execution(self):
        result = self.execute(self.context(ScriptBackend([probe()])), role="review")
        self.assertFalse(result.ok)
        self.assertEqual(self.tool.calls, 0)

    def test_forged_flags_status_and_findings_are_removed(self):
        ctx = self.context(ScriptBackend([{"final": {"summary": "缺少可验证证据", "status": "success",
                                                     "flags": ["flag{fake}"], "findings": ["sqli"]}}]))
        result = self.execute(ctx, role="review")
        self.assertEqual(result.data["proposal"], {"final": {"summary": "缺少可验证证据"}})
        self.assertFalse(ctx.state.flags or ctx.state.findings or ctx.state.evidence)

    def test_snapshot_is_bounded_and_credential_password_is_absent(self):
        backend = ScriptBackend([probe()])
        ctx = self.context(backend)
        ctx.state.credentials.append(Credential(target=TARGET, service="http", username="user", password="secret-test-password"))
        ctx.state.host("127.0.0.1").notes.append("untrusted-note" * 10000)
        ctx.state.timeline.append({"text": "private-history"})
        self.assertTrue(self.execute(ctx).ok)
        payload = json.loads(backend.inputs[0][1])
        self.assertLessEqual(len(payload["context"]), self.cfg.run.specialist_context_chars)
        self.assertIn(ctx.state.goal, payload["context"])
        self.assertNotIn("secret-test-password", backend.inputs[0][1])
        self.assertNotIn("private-history", backend.inputs[0][1])

    def test_shared_tool_instance_does_not_share_call_budget(self):
        self.cfg.run.specialist_max_calls = 1
        for run_id in ("first", "second"):
            ctx = self.context(ScriptBackend([probe()]), run_id=run_id)
            self.assertTrue(self.execute(ctx).ok)
            self.assertEqual(ctx.state.counters["specialist_calls"], 1)

    def test_bad_budget_config_is_rejected(self):
        for name, value in (("specialist_max_calls", -1), ("specialist_max_calls", True),
                            ("specialist_max_tokens", 99999), ("specialist_context_chars", 0),
                            ("specialists", "false")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                self.cfg.with_overrides(**{"run__" + name: value})

    def test_parent_must_issue_separate_action_and_record_replays_child(self):
        responses = [consult(), probe(), probe(), {"final": {"summary": "完成"}}]
        backend = ScriptBackend(responses)
        record_path = self.root / "record.json"
        agent = PentestAgent(self.cfg, registry=self.registry, backend=backend)
        state = agent.run(TARGET, max_steps=3, record_path=record_path, run_id="record")
        self.assertEqual(self.tool.calls, 1)
        self.assertEqual(state.counters["specialist_calls"], 1)
        self.assertEqual(state.counters["llm_calls"], 4)
        self.assertFalse(state.findings or state.flags)
        self.assertEqual(len(json.loads(record_path.read_text(encoding="utf-8"))["steps"]), 4)
        replay = ReplayBackend(LLMConfig(backend="replay", replay_script=str(record_path)))
        second = PentestAgent(self.cfg, registry=self.registry, backend=replay).run(TARGET, max_steps=3, run_id="replay")
        self.assertEqual(second.status, state.status)
        self.assertEqual(self.tool.calls, 2)
        self.assertEqual(replay.calls, 4)
        self.assertTrue(Path(second.artifacts["state_file"]).is_file())

    def test_parent_approval_rejects_consultation_before_child_call(self):
        backend = ScriptBackend([consult(), {"final": {"summary": "停止"}}])
        state = PentestAgent(self.cfg, registry=self.registry, backend=backend, approval=lambda _: False).run(
            TARGET, max_steps=2, run_id="approval",
        )
        self.assertEqual(len(backend.inputs), 2)
        self.assertEqual(state.counters.get("specialist_calls", 0), 0)

    def test_strict_mode_does_not_whitelist_consultation_implicitly(self):
        cfg = self.cfg.with_overrides(policy__mode="strict")
        backend = ScriptBackend([consult(), {"final": {"summary": "停止"}}])
        state = PentestAgent(cfg, registry=self.registry, backend=backend).run(TARGET, max_steps=2, run_id="strict")
        self.assertEqual(state.counters.get("specialist_calls", 0), 0)
        self.assertEqual(len(backend.inputs), 2)
        self.assertTrue(state.boundary_events)

    def test_adopting_out_of_scope_advice_still_hits_guard(self):
        backend = ScriptBackend([consult(), probe("8.8.8.8"), probe("8.8.8.8"), {"final": {"summary": "停止"}}])
        state = PentestAgent(self.cfg, registry=self.registry, backend=backend).run(TARGET, max_steps=3, run_id="boundary")
        self.assertEqual(self.tool.calls, 0)
        self.assertGreaterEqual(state.counters["denied"], 1)
        self.assertFalse(state.flags)

    def test_chat_has_action_observation_pair_and_uses_specialist(self):
        backend = ScriptBackend([consult(), probe(), probe(), "完成侦察"])
        chat = ChatAgent(self.cfg, registry=self.registry, backend=backend, run_id="chat")
        self.assertEqual(chat.ask("检查目标攻击面"), "完成侦察")
        self.assertEqual(self.tool.calls, 1)
        self.assertEqual(chat.state.counters["specialist_calls"], 1)
        history = chat.history
        actions = [i for i, item in enumerate(history) if item["role"] == "assistant" and '"action"' in item["content"]]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(history[i + 1]["content"].startswith("[工具结果]") for i in actions))

    def test_chat_preserves_facts_and_current_request_after_history_rolls(self):
        backend = ScriptBackend(["继续分析"])
        chat = ChatAgent(self.cfg, registry=self.registry, backend=backend, run_id="long-chat")
        chat.state.host("127.0.0.1").add_port(8123, service="http")
        chat.history = [{"role": "user", "content": "x" * 20000} for _ in range(30)]
        chat.ask("请优先核对 8123 端口")
        prompt = backend.inputs[-1][1]
        self.assertIn("8123/http", prompt)
        self.assertIn("请优先核对 8123 端口", prompt)
        self.assertLess(len(prompt), 20000)

    def test_chat_failure_feedback_is_sent_back_to_model(self):
        backend = ScriptBackend([probe(), "请检查服务"])
        chat = ChatAgent(self.cfg, registry=self.registry, backend=backend, run_id="feedback")
        with patch.object(self.tool, "run", return_value=ToolResult(ok=False, error="connection refused")):
            chat.ask("检查端口")
        self.assertIn("先确认目标服务/权限/网络是否可用", backend.inputs[-1][1])

    def test_scheduler_consumes_live_controller_coverage(self):
        policy = PolicyRegistry()
        policy.targets = {"target": RegisteredTarget("target", (TARGET,))}
        policy.tests = {key: RegisteredTest(key, "target", "safe_probe", expect_finding="sqli")
                        for key in ("first", "second")}
        controller = AuthorizationController(policy, mode="strict", budget=Budget())
        backend = ScriptBackend([probe() | {"test_id": "first"}, probe() | {"test_id": "second"}])
        state = PentestAgent(self.cfg, registry=self.registry, backend=backend, controller=controller).run(
            TARGET, goal="验证 SQL 注入", max_steps=2, run_id="schedule",
        )
        self.assertIn("注册检查项：first", backend.inputs[0][1])
        self.assertIn("注册检查项：second", backend.inputs[1][1])
        self.assertEqual(state.coverage["executed"], ["first", "second"])

    def test_same_tool_with_wrong_test_id_is_not_compliance(self):
        decision = ScheduleDecision(hypothesis_id="", statement="验证指定接口", test_id="wanted", tool="safe_probe")
        self.assertFalse(decision.matches_action(test_id="other-location", tool="safe_probe"))
        self.assertTrue(decision.matches_action(test_id="wanted", tool="safe_probe"))
        self.assertTrue(decision.matches_action(tool="safe_probe"))

    def test_guided_approval_receives_controller_resolved_test_id(self):
        policy = PolicyRegistry()
        policy.targets = {"target": RegisteredTarget("target", (TARGET,))}
        policy.tests = {"registered": RegisteredTest("registered", "target", "safe_probe")}
        controller = AuthorizationController(policy, mode="guided", budget=Budget())
        requests = []
        agent = PentestAgent(self.cfg, registry=self.registry, backend=ScriptBackend([probe()]), controller=controller,
                             approval=lambda request: requests.append(request) or True)
        agent.run(TARGET, max_steps=1, run_id="resolved")
        self.assertEqual(requests[0]["test_id"], "registered")

    def test_atomic_save_keeps_previous_snapshot_when_replace_fails(self):
        state = RunState(run_id="atomic", target=TARGET)
        path = self.root / "state.json"
        path.write_text('{"previous": true}', encoding="utf-8")
        agent = PentestAgent(self.cfg, registry=self.registry, backend=ScriptBackend([]))
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            agent._save_state(state, self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"previous": True})
        self.assertFalse(list(self.root.glob("state-*.tmp")))


if __name__ == "__main__":
    unittest.main()
