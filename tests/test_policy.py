"""授权策略测试：注册表、控制器裁决、预算与停止、撤权、结论状态与复核。

这些行为是 P3「计划器提出、独立控制器批准」的落点：
一旦失效，模型就能自行扩大范围，或者把"没跑"当成"没问题"。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trina.brain.agent import PentestAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm.replay_backend import ReplayBackend
from trina.policy import AuthorizationController, Budget, PolicyRegistry
from trina.review import review_state
from trina.tools import build_registry


class TestPolicyRegistry(unittest.TestCase):
    def test_loads_targets_and_tests_from_ranges(self):
        registry = PolicyRegistry.load()
        self.assertIn("lab-http", registry.targets)
        self.assertIn("lab-ssh", registry.targets)
        self.assertIn("sqli_probe", registry.tests)
        test = registry.tests["sqli_probe"]
        self.assertEqual(test.tool, "sqli_test")
        self.assertEqual(test.target_id, "lab-http")
        self.assertEqual(test.level, "verify")

    def test_target_matching(self):
        registry = PolicyRegistry.load()
        self.assertIsNotNone(registry.match_target("http://127.0.0.1:9099"))
        self.assertIsNotNone(registry.match_target("http://127.0.0.1:9099/sqli?id=1"))
        self.assertIsNotNone(registry.match_target("ssh://127.0.0.1:2222"))
        self.assertIsNone(registry.match_target("http://127.0.0.1:8081"))

    def test_prompt_block_lists_registered_tests(self):
        registry = PolicyRegistry.load()
        block = registry.prompt_block("lab-http")
        self.assertIn("sqli_probe", block)
        self.assertIn("flag_read", block)


class TestController(unittest.TestCase):
    def setUp(self):
        self.registry = PolicyRegistry.load()

    def _controller(self, mode="strict", **budget):
        return AuthorizationController(
            self.registry,
            mode=mode,
            budget=Budget(**{"max_checks": 40, "logic_seconds": 900, "stop_on_no_new_evidence": 4, **budget}),
        )

    def test_strict_requires_registered_test_id(self):
        controller = self._controller()
        denied = controller.check({"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.kind, "test")
        allowed = controller.check(
            {
                "tool": "sqli_test",
                "level": "verify",
                "target": "http://127.0.0.1:9099/sqli",
                "test_id": "sqli_probe",
            }
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(controller.executed, ["sqli_probe"])

    def test_strict_rejects_unregistered_target(self):
        controller = self._controller()
        denied = controller.check(
            {"tool": "port_scan", "level": "detect", "target": "192.168.9.9", "test_id": "port_quick"}
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.kind, "target")

    def test_test_id_tool_mismatch_is_denied(self):
        controller = self._controller()
        denied = controller.check(
            {"tool": "lfi_test", "level": "verify", "target": "http://127.0.0.1:9099/lfi", "test_id": "sqli_probe"}
        )
        self.assertFalse(denied.allowed)
        self.assertIn("绑定的是", denied.reason)

    def test_guided_resolves_registered_tool(self):
        controller = self._controller(mode="guided")
        decision = controller.check({"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"})
        self.assertTrue(decision.allowed)
        self.assertEqual(controller.executed, ["sqli_probe"], "guided 模式应按工具反查注册项以便统计覆盖")
        controller.note_evidence(False)
        self.assertIn("lfi_test", [item.get("tool") for item in controller.coverage()["untested"]])

    def test_bare_host_inherits_run_target(self):
        """动作写裸主机时应继承本次运行的目标注册（同主机才继承，且继承不放宽撤权）。"""
        controller = self._controller(mode="guided")
        controller.default_target_id = "lab-http"
        decision = controller.check({"tool": "port_scan", "level": "detect", "target": "127.0.0.1"})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.target_id, "lab-http")
        self.assertEqual(controller.executed, ["port_quick"], "继承后应能反查到注册检查项")

        controller.revoke("lab-http")
        denied = controller.check({"tool": "port_scan", "level": "detect", "target": "127.0.0.1"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.kind, "revoked", "撤权必须覆盖裸主机写法")

        controller.grant("lab-http")
        other = controller.check({"tool": "port_scan", "level": "detect", "target": "192.168.9.9"})
        self.assertTrue(other.allowed, "不同主机不继承注册，但 guided 模式仍交给护栏判定")
        self.assertIn("port_scan", controller.coverage()["unregistered"])

    def test_guided_marks_unregistered_tool(self):
        controller = self._controller(mode="guided")
        self.assertTrue(controller.check({"tool": "tls_info", "level": "detect", "target": "http://127.0.0.1:9099"}).allowed)
        self.assertIn("tls_info", controller.coverage()["unregistered"])

    def test_revocation_blocks_further_checks(self):
        controller = self._controller(mode="guided")
        controller.revoke("lab-http")
        denied = controller.check({"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.kind, "revoked")
        self.assertIn("撤销", denied.reason)
        controller.grant("lab-http")
        self.assertTrue(controller.check({"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"}).allowed)

    def test_budget_stops_checks(self):
        controller = self._controller(mode="guided", max_checks=1)
        request = {"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"}
        self.assertTrue(controller.check(request).allowed)
        denied = controller.check(request)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.kind, "budget")
        self.assertIn("上限", controller.stop_reason())

    def test_no_new_evidence_triggers_stop(self):
        controller = self._controller(mode="guided", stop_on_no_new_evidence=2)
        self.assertFalse(controller.note_evidence(False))
        self.assertTrue(controller.note_evidence(False))
        self.assertIn("无新增证据", controller.stop_reason())

    def test_coverage_reports_untested_reasons(self):
        controller = self._controller(mode="guided")
        controller.check({"tool": "sqli_test", "level": "verify", "target": "http://127.0.0.1:9099/sqli"})
        coverage = controller.coverage()
        self.assertEqual(coverage["executed"], ["sqli_probe"])
        self.assertTrue(coverage["untested"])
        self.assertTrue(all(item["reason"] for item in coverage["untested"]))


class TestVerdictAndReview(unittest.TestCase):
    """结论三分类 + 评价 Agent 的只读复核。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = TrinaConfig.load().with_overrides(
            run__workspace=str(Path(self.tmp.name) / "runs"),
            audit__dir=str(Path(self.tmp.name) / "logs"),
            policy__mode="guided",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script: list[dict]) -> object:
        path = Path(self.tmp.name) / "replay.json"
        path.write_text(json.dumps({"steps": script}, ensure_ascii=False), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(path)))
        agent = PentestAgent(self.cfg, registry=build_registry(), backend=backend)
        return agent.run("http://127.0.0.1:9099", max_steps=4)

    def test_not_confirmed_when_checks_ran_without_findings(self):
        state = self._run(
            [
                {"response": '{"thought": "扫端口", "action": "port_scan", "params": {"target": "127.0.0.1", "ports": "1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "无发现"}}'},
            ]
        )
        self.assertEqual(state.verdict, "NOT_CONFIRMED")
        self.assertTrue(state.coverage.get("executed"))

    def test_inconclusive_when_nothing_executed(self):
        state = self._run([{"response": '{"thought": "直接结束", "final": {"summary": "不测了"}}'}])
        self.assertEqual(state.verdict, "INCONCLUSIVE")

    def test_inconclusive_when_tool_fails(self):
        """工具失败/连不上时，结论必须是 INCONCLUSIVE，不能写成 NOT_CONFIRMED。"""
        state = self._run(
            [
                {
                    "response": '{"thought": "指纹识别", "action": "http_fingerprint", "params": {"url": "http://127.0.0.1:9/"}}'
                },
                {"response": '{"thought": "结束", "final": {"summary": "目标不可达"}}'},
            ]
        )
        self.assertGreaterEqual(state.counters.get("failures", 0), 1)
        self.assertEqual(state.verdict, "INCONCLUSIVE")

    def test_review_flags_claim_without_evidence(self):
        state = {
            "run_id": "x",
            "target": "http://127.0.0.1:9099",
            "verdict": "CONFIRMED",
            "status": "success",
            "findings": [{"vuln_class": "sqli", "url": "http://x/sqli", "status": "verified", "evidence": []}],
            "coverage": {"executed": ["sqli_probe"], "untested": [{"test_id": "lfi_probe", "tool": "lfi_test", "reason": "预算用尽"}]},
            "boundary_events": [{"tool": "rce_exec", "kind": "action-level", "result": "blocked", "reason": "超等级"}],
        }
        note = review_state(state)
        self.assertTrue(note.claims_without_evidence, "verified 但无证据必须被复核标记")
        self.assertEqual(len(note.coverage_gaps), 1)
        markdown = note.to_markdown()
        self.assertIn("缺证据", markdown)
        self.assertIn("边界事件", markdown)
        self.assertIn("建议补测", markdown)

    def test_review_conflict_when_confirmed_without_verified(self):
        note = review_state(
            {"run_id": "y", "target": "t", "verdict": "CONFIRMED", "findings": [], "coverage": {"executed": ["x"]}}
        )
        self.assertTrue(note.verdict_conflicts)


if __name__ == "__main__":
    unittest.main()
