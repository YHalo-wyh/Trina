"""证据驱动决策回归：阻止模型自评、疑似发现和重复输出伪造验证进展。"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.brain.agent import PentestAgent
from trina.brain.failure import LoopGuard
from trina.brain.goal import GoalRequirements
from trina.config import LLMConfig, TrinaConfig
from trina.evidence_graph import build
from trina.hypotheses import HypothesisLedger
from trina.lab.http_target import LabState, handle_lfi
from trina.llm.replay_backend import ReplayBackend
from trina.policy import AuthorizationController, Budget, PolicyRegistry, RegisteredTarget, RegisteredTest
from trina.state import RunState
from trina.tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec


URL = "http://127.0.0.1/sqli"


def finding(status: str = "verified", *, evidence: bool = True, url: str = URL, vuln_class: str = "sqli") -> dict:
    return {"title": "测试发现", "vuln_class": vuln_class, "url": url, "parameter": "id",
            "status": status, "evidence": [{"kind": "http", "note": "验证响应", "ts": 1}] if evidence else []}


class SequenceTool(Tool):
    """按预定顺序产出事实，真实经过注册表、控制器、审计与状态合并。"""

    spec = ToolSpec(name="sequence_probe", summary="离线证据验证", action_level="verify",
                    params={"url": ParamSpec("str", "目标 URL")})

    def __init__(self, outputs: list[dict]):
        self.outputs = iter(outputs)

    def run(self, ctx, url):
        return ToolResult(summary="检查完成", facts=next(self.outputs))


class TestEvidenceDecisions(unittest.TestCase):
    def test_structured_subject_matches_exact_location(self):
        ledger = HypothesisLedger()
        h = ledger.add("注入", vuln_class="sqli", target=URL, parameter="id")
        from trina.state import Finding

        def candidate(**overrides):
            data = finding(**overrides)
            return Finding(id="x", **data)

        self.assertTrue(h.matches_finding(candidate(url=URL + "?id=1")))
        for data in (candidate(url=URL + "-other"), candidate(url="http://localhost/sqli"),
                     candidate(url="https://127.0.0.1/sqli"), candidate(vuln_class="lfi")):
            self.assertFalse(h.matches_finding(data))
        unbound = ledger.add("未指定验证对象")
        self.assertFalse(unbound.matches_finding(candidate()))

    def test_reference_does_not_overwrite_hypothesis(self):
        ledger = HypothesisLedger()
        h = ledger.add("SQL 注入", vuln_class="sqli", target=URL)
        self.assertIs(ledger.from_action({"hypothesis_id": h.id, "hypothesis": {"statement": "替换文字"}}), h)
        self.assertEqual(h.statement, "SQL 注入")
        self.assertIsNone(ledger.from_action({"hypothesis_id": "H-999"}))
        self.assertEqual(len(ledger.items), 1)

    def test_same_statement_at_different_locations_keeps_separate_hypotheses(self):
        ledger = HypothesisLedger()
        a = ledger.add("疑似 SQL 注入", vuln_class="sqli", target=URL)
        b = ledger.add("疑似 SQL 注入", vuln_class="sqli", target=URL + "-other")
        self.assertNotEqual(a.id, b.id)

    def test_confirmation_cannot_be_erased_by_later_timeout(self):
        ledger = HypothesisLedger()
        h = ledger.add("注入")
        ledger.record_outcome(h.id, new_information=False, confirmed=True)
        self.assertNotEqual(h.status, "confirmed")
        ledger.record_outcome(h.id, new_information=True, confirmed=True, evidence=[{"finding_id": "f"}])
        ledger.record_outcome(h.id, new_information=False, blocked=True, note="后续超时")
        self.assertEqual(h.status, "confirmed")

    def test_knowledge_ignores_duplicate_facts_and_timestamps_but_sees_upgrade(self):
        state = RunState("s", URL)
        state.absorb({"findings": [finding("detected")]})
        before = state.knowledge_digest()
        duplicate = finding("detected")
        duplicate["evidence"][0]["ts"] = 999
        state.absorb({"findings": [duplicate]})
        self.assertEqual(state.knowledge_digest(), before)
        state.absorb({"findings": [finding("verified")]})
        self.assertNotEqual(state.knowledge_digest(), before)

    def test_loop_fingerprint_does_not_truncate_distinct_inputs_or_results(self):
        guard = LoopGuard()
        result = ToolResult(raw="x" * 5000 + "a")
        self.assertEqual(guard.observe("probe", {"payload": "x" * 400 + "a"}, result), "")
        self.assertEqual(guard.observe("probe", {"payload": "x" * 400 + "b"}, result), "")
        self.assertEqual(guard.observe("probe", {"payload": "x" * 400 + "a"}, ToolResult(raw="x" * 5000 + "b")), "")
        a = ToolResult(data={"ts": 1, "nested": {"duration": 2, "value": 3}})
        b = ToolResult(data={"ts": 9, "nested": {"duration": 7, "value": 3}})
        self.assertEqual(guard.result_digest(a), guard.result_digest(b))

    def test_goal_requires_verified_evidence(self):
        goal = GoalRequirements.parse("确认 SQL 注入")
        for data in (finding("detected"), finding("verified", evidence=False)):
            state = RunState("s", URL)
            state.absorb({"findings": [data]})
            self.assertFalse(goal.satisfied(state))

    def test_lab_passwd_fixture_is_consistent_between_encodings(self):
        state = LabState()
        code, plain = handle_lfi(state, "../../etc/passwd")
        encoded_code, encoded = handle_lfi(state, "php://filter/convert.base64-encode/resource=/etc/passwd")
        self.assertEqual((code, encoded_code), (200, 200))
        self.assertIn("root:x:0:0", plain)
        self.assertEqual(base64.b64decode(encoded).decode(), plain)


class TestEvidenceRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = TrinaConfig.load()
        self.cfg.run.workspace = str(Path(self.tmp.name) / "runs")
        self.cfg.audit.dir = str(Path(self.tmp.name) / "logs")
        self.cfg.policy.stop_on_no_new_evidence = 2

    def agent(self, actions: list[dict], outputs: list[dict]) -> PentestAgent:
        script = Path(self.tmp.name) / "script.json"
        script.write_text(json.dumps({"steps": [{"response": json.dumps(a)} for a in actions]}), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(script)))
        policy = PolicyRegistry()
        policy.targets["test-http"] = RegisteredTarget("test-http", ("http://127.0.0.1:80",))
        policy.tests["sequence"] = RegisteredTest("sequence", "test-http", "sequence_probe", "verify")
        controller = AuthorizationController(policy, mode="guided", budget=Budget(stop_on_no_new_evidence=2))
        return PentestAgent(self.cfg, registry=ToolRegistry([SequenceTool(outputs)]), backend=backend, controller=controller)

    def action(self, *, reference: bool = False) -> dict:
        action = {"action": "sequence_probe", "params": {"url": URL}}
        if reference:
            action["hypothesis_id"] = "H-001"
        else:
            action["hypothesis"] = {"statement": "SQL 注入待验证", "vuln_class": "sqli", "target": URL, "parameter": "id"}
        return action

    def test_detected_upgrade_confirms_same_id_and_flushes_live_ledger(self):
        agent = self.agent([self.action(), self.action(reference=True), {"final": {"summary": "完成"}}],
                           [{"findings": [finding("detected")]}, {"findings": [finding()]}])
        snapshots = []
        original = agent._save_state

        def capture(state, workspace):
            original(state, workspace)
            snapshots.append(json.loads((workspace / "state.json").read_text(encoding="utf-8")))

        agent._save_state = capture
        state = agent.run(URL)
        self.assertEqual(len(state.findings), 1)
        self.assertEqual(state.hypotheses[0]["status"], "confirmed")
        self.assertEqual(state.hypotheses[0]["attempts"], 2)
        self.assertEqual(snapshots[0]["hypotheses"][0]["status"], "verifying")
        self.assertEqual(snapshots[1]["hypotheses"][0]["status"], "confirmed")
        self.assertEqual(state.verdict, "CONFIRMED")
        audit = AuditLog(Path(state.artifacts["audit_path"]), state.run_id)
        self.assertTrue(audit.verify()[0])

    def test_unrelated_or_unevidenced_findings_do_not_confirm(self):
        for data in (finding("detected"), finding(evidence=False), finding(url=URL + "-other"), finding(vuln_class="lfi")):
            with self.subTest(data=data):
                agent = self.agent([self.action(), {"final": {"summary": "结束"}}], [{"findings": [data]}])
                state = agent.run(URL)
                self.assertNotEqual(state.hypotheses[0]["status"], "confirmed")
                graph = build(state, hypotheses=state.hypotheses)
                self.assertNotIn("验证为", [edge.label for edge in graph.edges])

    def test_repeated_facts_trigger_stop_contract(self):
        agent = self.agent([self.action()] + [self.action(reference=True)] * 5,
                           [{"findings": [finding("detected")]}] * 6)
        state = agent.run(URL, max_steps=6)
        self.assertEqual(state.counters["tool_calls"], 3)
        self.assertIn("无新增", state.coverage["stop_reason"])
        self.assertNotEqual(state.hypotheses[0]["status"], "refuted")

    def test_model_final_cannot_create_flag_or_success(self):
        agent = self.agent([{"final": {"status": "success", "flags": ["flag{invented}"], "summary": "自称完成"}}], [])
        state = agent.run(URL)
        self.assertEqual(state.flags, [])
        self.assertNotEqual(state.status, "success")
        self.assertEqual(state.verdict, "INCONCLUSIVE")

    def test_chat_uses_same_evidence_rules(self):
        from trina.brain.chat import ChatAgent

        task = self.agent([], [{"findings": [finding("detected")]}, {"findings": [finding()]}])
        chat = ChatAgent(self.cfg, registry=task.registry, backend=task.backend, controller=task.controller)
        chat._run_action(self.action(), 1)
        self.assertEqual(chat.ledger.get("H-001").status, "verifying")
        chat._run_action(self.action(reference=True), 2)
        self.assertEqual(chat.state.hypotheses[0]["status"], "confirmed")
        self.assertEqual(len(chat.state.findings), 1)


if __name__ == "__main__":
    unittest.main()
