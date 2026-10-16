"""洞察层测试：假设台账、失败分类与循环守卫、证据关系图、工作台面板、新内部指标。

这些是「让 Agent 说得清为什么这么做、并且能从失败里换路」的能力，失效的话：
决策退化成撞墙循环、结论无法反向追溯、指标看不出成本效率。
"""

from __future__ import annotations

import unittest

from trina.board import render_board, summarise
from trina.brain.failure import LoopGuard, advice_for, alternatives_for, classify_failure
from trina.evidence_graph import build as build_graph
from trina.eval.metrics import ScenarioResult, compute_metrics
from trina.hypotheses import HypothesisLedger
from trina.state import Finding, RunState
from trina.tools import build_registry
from trina.tools.registry import ToolResult


class TestHypothesisLedger(unittest.TestCase):
    def test_priority_formula(self):
        ledger = HypothesisLedger()
        item = ledger.add("怀疑 /sqli 存在错误型注入", confidence=0.8, value=0.8, info_gain=0.8, cost=0.2, risk=0.2)
        expected = (0.8 * 0.8 * 0.8) / (0.2 + 1.0 * 0.2 + 0.05)
        self.assertAlmostEqual(item.priority(), round(expected, 4), places=4)

    def test_level_words_are_coarsened(self):
        ledger = HypothesisLedger()
        item = ledger.add("假设 A", confidence="high", cost="low", risk="very low")
        self.assertEqual(item.confidence, 0.8)
        self.assertEqual(item.cost, 0.4)
        self.assertEqual(item.risk, 0.2)

    def test_duplicate_is_reused(self):
        ledger = HypothesisLedger()
        first = ledger.add("怀疑存在目录穿越，可读取系统文件")
        second = ledger.add("怀疑存在目录穿越，可读取系统文件（细化）")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(ledger.items), 1)

    def test_outcome_confirms_and_refutes(self):
        ledger = HypothesisLedger()
        confirmed = ledger.add("怀疑存在 SQL 注入")
        result = ledger.record_outcome(confirmed.id, new_information=True, confirmed=True, evidence=[{"finding_id": "PAI-001"}])
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.priority(), 0.0, "已确认的假设不应再占用优先级")

        refuted = ledger.add("怀疑存在文件包含")
        for _ in range(3):
            ledger.record_outcome(refuted.id, new_information=False)
        self.assertEqual(refuted.status, "refuted")
        self.assertTrue(any("已否定" in note or "无新信息" in note for note in refuted.notes))

    def test_blocked_hypothesis_is_marked(self):
        ledger = HypothesisLedger()
        item = ledger.add("怀疑可以读取 /etc/shadow")
        ledger.record_outcome(item.id, new_information=False, blocked=True)
        self.assertEqual(item.status, "blocked")
        self.assertIn("人工", item.next_step)

    def test_finalize_marks_unresolved_without_faking(self):
        ledger = HypothesisLedger()
        pending = ledger.add("怀疑存在目录爆破可发现的管理后台")
        done = ledger.add("怀疑存在 SQL 注入")
        ledger.record_outcome(done.id, new_information=True, confirmed=True)
        unfinished = ledger.finalize()
        self.assertEqual([item.id for item in unfinished], [pending.id])
        self.assertEqual(pending.status, "open")
        self.assertIn("下一轮", pending.next_step)
        self.assertEqual(done.status, "confirmed", "已确认的假设不应被收尾改动")

    def test_prompt_block_sorted_by_priority(self):
        ledger = HypothesisLedger()
        ledger.add("低价值假设，成本高", confidence=0.4, value=0.4, info_gain=0.4, cost=0.8, risk=0.6)
        ledger.add("高价值假设，成本低", confidence=0.9, value=0.8, info_gain=0.8, cost=0.2, risk=0.2)
        block = ledger.prompt_block()
        self.assertLess(block.index("高价值假设"), block.index("低价值假设"))

    def test_from_action_accepts_string_and_dict(self):
        ledger = HypothesisLedger()
        self.assertIsNotNone(ledger.from_action({"hypothesis": "纯文本假设"}))
        item = ledger.from_action({"hypothesis": {"statement": "结构化假设", "confidence": "high", "next_step": "验证 id 参数"}})
        self.assertIsNotNone(item)
        self.assertEqual(item.confidence, 0.8)
        self.assertEqual(item.next_step, "验证 id 参数")
        self.assertIsNone(ledger.from_action({}))


class TestFailureAndLoopGuard(unittest.TestCase):
    def _result(self, **kwargs):
        return ToolResult(**kwargs)

    def test_classify_failure_kinds(self):
        self.assertEqual(classify_failure(self._result(ok=True, summary="done")), "ok")
        self.assertEqual(classify_failure(self._result(ok=False, denied=True, error="越界")), "boundary_denied")
        self.assertEqual(classify_failure(self._result(ok=False, error="参数非法: 未知参数 ['x']")), "tool_error")
        self.assertEqual(classify_failure(self._result(ok=False, error="ConnectionError: connection refused")), "environment_error")
        self.assertEqual(classify_failure(self._result(ok=False, error="逻辑预算已用尽")), "budget_exhausted")
        self.assertEqual(classify_failure(self._result(ok=False, error="未发现任何线索", summary="未发现")), "hypothesis_error")

    def test_advice_covers_all_kinds(self):
        for kind in ("tool_error", "environment_error", "budget_exhausted", "boundary_denied", "hypothesis_error"):
            self.assertTrue(advice_for(kind), kind)

    def test_loop_guard_escalation_ladder(self):
        guard = LoopGuard()
        params = {"url": "http://127.0.0.1:9099/sqli", "param": "id"}
        result = self._result(ok=False, error="未发现", summary="未发现")
        self.assertEqual(guard.observe("sqli_test", params, result), "", "第一次不算重复")
        second = guard.observe("sqli_test", params, result)
        self.assertIn("换参数", second)
        third = guard.observe("sqli_test", params, result)
        self.assertIn("换工具", third)
        fourth = guard.observe("sqli_test", params, result)
        self.assertIn("人工", fourth)
        self.assertTrue(guard.needs_human())
        self.assertAlmostEqual(guard.redundant_rate, 3 / 4, places=4)

    def test_loop_guard_ignores_different_results(self):
        guard = LoopGuard()
        params = {"target": "127.0.0.1"}
        guard.observe("port_scan", params, self._result(ok=True, summary="发现 1 个端口", raw="80"))
        advice = guard.observe("port_scan", params, self._result(ok=True, summary="发现 2 个端口", raw="80,443"))
        self.assertEqual(advice, "", "结果不同就不算无新信息")

    def test_alternatives_for_prefers_same_category_and_lower_level(self):
        registry = build_registry()
        options = alternatives_for(registry, "poc_scan", tried={"poc_scan"})
        self.assertTrue(options)
        category = registry.get("poc_scan").spec.category
        for name in options:
            spec = registry.get(name).spec
            self.assertEqual(spec.category, category, f"{name} 应属同类能力")
            self.assertLessEqual(spec.action_level, registry.get("poc_scan").spec.action_level)
        self.assertNotIn("poc_scan", options)

    def test_alternatives_empty_for_unknown_tool(self):
        self.assertEqual(alternatives_for(build_registry(), "no_such_tool"), [])


class TestEvidenceGraph(unittest.TestCase):
    def _state(self) -> RunState:
        state = RunState(run_id="g", target="http://127.0.0.1:9099")
        host = state.host("127.0.0.1")
        host.add_port(9099, service="http", version="Apache")
        host.web_paths.append("http://127.0.0.1:9099/sqli")
        state.absorb(
            {
                "findings": [
                    {
                        "title": "SQL 注入",
                        "vuln_class": "sqli",
                        "url": "http://127.0.0.1:9099/sqli",
                        "parameter": "id",
                        "severity": "high",
                        "status": "verified",
                        "tool": "sqli_test",
                        "evidence": [{"kind": "http", "note": "报错回显"}],
                    }
                ],
                "flags": [{"value": "flag{trina_lab_x}", "source": "http://127.0.0.1:9099/flag"}],
            }
        )
        return state

    def test_build_nodes_and_edges(self):
        state = self._state()
        graph = build_graph(state, hypotheses=[{"id": "H-001", "statement": "怀疑注入", "status": "confirmed",
                                                "evidence": [{"finding_id": "PAI-001"}]}])
        kinds = graph.node_kinds()
        self.assertGreaterEqual(kinds.get("host", 0), 1)
        self.assertGreaterEqual(kinds.get("port", 0), 1)
        self.assertEqual(kinds.get("finding"), 1)
        self.assertEqual(kinds.get("evidence"), 1)
        self.assertEqual(kinds.get("hypothesis"), 1)
        labels = [edge.label for edge in graph.edges]
        self.assertIn("证据", labels)
        self.assertIn("验证为", labels)
        self.assertIn("节点", graph.summary())

    def test_mermaid_is_renderable(self):
        graph = build_graph(self._state())
        text = graph.to_mermaid()
        self.assertTrue(text.startswith("```mermaid"))
        self.assertIn("flowchart", text)
        self.assertIn("sqli", text)
        self.assertTrue(text.strip().endswith("```"))

    def test_empty_state_still_renders(self):
        graph = build_graph(RunState(run_id="e", target="127.0.0.1"))
        self.assertIn("mermaid", graph.to_mermaid())


class TestBoard(unittest.TestCase):
    def _state_dict(self):
        return {
            "run_id": "20260101-000000-abc",
            "target": "http://127.0.0.1:9099",
            "status": "success",
            "verdict": "CONFIRMED",
            "phase": "post",
            "started_at": 1000.0,
            "ended_at": 1060.0,
            "findings": [
                {"severity": "high", "status": "verified"},
                {"severity": "info", "status": "detected"},
            ],
            "coverage": {"executed": ["sqli_probe"], "untested": [{"test_id": "lfi_probe"}], "stop_reason": ""},
            "loop": {"redundant_action_rate": 0.25},
            "hypotheses": [
                {"id": "H-001", "statement": "怀疑注入", "status": "open", "confidence": 0.7, "priority": 1.2},
                {"id": "H-002", "statement": "已确认的", "status": "confirmed", "confidence": 0.9, "priority": 0.0},
            ],
            "counters": {"steps": 6, "denied": 1, "errors": 0},
            "ledger": {"calls": 5, "input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.003},
            "steps": [{"kind": "tool", "tool": "sqli_test", "ok": True, "summary": "SQL 注入已验证"}],
            "flags": [{"value": "flag{x}"}],
            "credentials": [],
            "hosts": {"127.0.0.1": {}},
            "artifacts": {"workspace": "/tmp/runs/x"},
        }

    def test_summarise_fields(self):
        data = summarise(self._state_dict())
        self.assertEqual(data["findings_total"], 2)
        self.assertEqual(data["findings_verified"], 1)
        self.assertEqual(data["hypotheses_open"], 1)
        self.assertEqual(data["redundant_rate"], 0.25)
        self.assertEqual(data["duration"], "1m00s")

    def test_render_board_sections(self):
        text = render_board(self._state_dict(), hypotheses=self._state_dict()["hypotheses"])
        for expected in ("TRINA / WORKSPACE", "研究成果", "假设台账", "覆盖", "sqli_test"):
            self.assertIn(expected, text)


class TestStateRoundTrip(unittest.TestCase):
    def test_to_dict_from_dict_round_trip(self):
        import json

        state = RunState(run_id="rt", target="http://127.0.0.1:9099", goal="验证")
        host = state.host("127.0.0.1")
        host.add_port(9099, service="http", version="Apache")
        host.alive = True
        host.web_paths.append("http://127.0.0.1:9099/sqli")
        state.absorb(
            {
                "findings": [{"title": "SQL 注入", "vuln_class": "sqli", "severity": "high", "status": "verified"}],
                "credentials": [{"target": "127.0.0.1", "service": "ssh", "username": "root", "password": "toor"}],
                "flags": [{"value": "flag{x}", "source": "http://127.0.0.1:9099/flag"}],
            }
        )
        state.verdict = "CONFIRMED"
        state.counters["denied"] = 2
        state.hypotheses = [{"id": "H-001", "statement": "怀疑注入", "status": "confirmed", "priority": 0.0}]
        state.ended_at = state.started_at + 12.5

        payload = json.loads(json.dumps(state.to_dict(), ensure_ascii=False))
        restored = RunState.from_dict(payload)

        self.assertEqual(restored.run_id, "rt")
        self.assertEqual(restored.verdict, "CONFIRMED")
        self.assertEqual(restored.counters["denied"], 2)
        self.assertEqual(list(restored.hosts["127.0.0.1"].ports.keys()), [9099], "端口键要还原成 int")
        self.assertEqual(restored.hosts["127.0.0.1"].ports[9099]["service"], "http")
        self.assertEqual(restored.hosts["127.0.0.1"].alive, True)
        self.assertEqual(len(restored.findings), 1)
        self.assertEqual(restored.findings[0].vuln_class, "sqli")
        self.assertEqual(restored.findings[0].status, "verified")
        self.assertEqual(restored.credentials[0].username, "root")
        self.assertEqual(restored.flags[0]["value"], "flag{x}")
        self.assertEqual(restored.hypotheses[0]["id"], "H-001")
        self.assertAlmostEqual(restored.ended_at, state.ended_at, places=3)
        self.assertEqual(restored.to_dict()["findings"][0]["title"], "SQL 注入")

    def test_from_dict_rejects_error_payload(self):
        with self.assertRaises(ValueError):
            RunState.from_dict({"error": "读取失败"})


class TestInternalMetrics(unittest.TestCase):
    def test_cost_redundancy_and_recovery(self):
        results = [
            ScenarioResult(scenario_id="a", kind="web", status="success", duration=10, cost_usd=0.01,
                           first_evidence_seconds=2.0, redundant_rate=0.1, failures=2),
            ScenarioResult(scenario_id="b", kind="web", status="failed", duration=30, cost_usd=0.03,
                           first_evidence_seconds=None, redundant_rate=0.5, failures=3),
            ScenarioResult(scenario_id="c", kind="web", status="success", duration=20, cost_usd=0.02,
                           first_evidence_seconds=6.0, redundant_rate=0.0, failures=0),
        ]
        metrics = compute_metrics(results)
        self.assertAlmostEqual(metrics["cost_usd_total"], 0.06, places=6)
        self.assertAlmostEqual(metrics["cost_per_success"], 0.03, places=6)  # 0.06 / 2 次成功
        self.assertAlmostEqual(metrics["time_to_evidence_avg"], 4.0, places=3)  # (2+6)/2
        self.assertAlmostEqual(metrics["redundant_action_rate"], 0.2, places=4)  # (0.1+0.5+0)/3
        self.assertAlmostEqual(metrics["recovery_rate"], 1 / 2, places=4)  # 失败过且最终成功：a 与 b → 1/2

    def test_metrics_without_cost_data(self):
        metrics = compute_metrics([ScenarioResult(scenario_id="x", kind="web", status="partial", duration=1)])
        self.assertEqual(metrics["cost_per_success"], 0.0)
        self.assertIsNone(metrics["time_to_evidence_avg"])
        self.assertEqual(metrics["recovery_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
