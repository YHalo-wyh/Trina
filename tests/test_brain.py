"""大脑测试：动作协议解析、阶段机推进、以及用回放后端的完整 ReAct 闭环。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.brain.agent import PentestAgent
from trina.brain.planner import Planner
from trina.brain.prompts import build_system_prompt, parse_action
from trina.config import PAIConfig, LLMConfig
from trina.llm.replay_backend import ReplayBackend
from trina.scope import ScopeGuard
from trina.state import Finding, RunState
from trina.tools.registry import ParamSpec, Tool, ToolResult, ToolSpec


# ---------------------------------------------------------------------------
# 动作协议解析
# ---------------------------------------------------------------------------


class TestParseAction(unittest.TestCase):
    def test_plain_json(self):
        action = parse_action('{"thought": "扫端口", "action": "port_scan", "params": {"target": "127.0.0.1"}}')
        self.assertEqual(action["action"], "port_scan")
        self.assertEqual(action["params"]["target"], "127.0.0.1")

    def test_fenced_json(self):
        text = '```json\n{"thought": "t", "action": "poc_scan", "params": {}}\n```'
        self.assertEqual(parse_action(text)["action"], "poc_scan")

    def test_json_after_prose(self):
        text = '我先想想。\n{"thought": "收尾", "final": {"status": "success", "summary": "done"}}\n结束。'
        action = parse_action(text)
        self.assertIn("final", action)
        self.assertEqual(action["final"]["status"], "success")

    def test_last_valid_object_wins(self):
        text = '{"thought":"草稿","action":"a"}\n{"thought":"最终","action":"b","params":{}}'
        self.assertEqual(parse_action(text)["action"], "b")

    def test_braces_inside_strings(self):
        text = '{"thought": "payload 里有 } 和 {", "action": "sqli_test", "params": {"url": "http://x/?a={\\"b\\":1}"}}'
        self.assertEqual(parse_action(text)["action"], "sqli_test")

    def test_garbage(self):
        action = parse_action("完全不是 JSON")
        self.assertIn("error", action)
        self.assertIn("raw", action)

    def test_missing_fields(self):
        self.assertIn("error", parse_action('{"foo": 1}'))

    def test_empty(self):
        self.assertIn("error", parse_action(""))


# ---------------------------------------------------------------------------
# 阶段机
# ---------------------------------------------------------------------------


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.state = RunState(run_id="p", target="127.0.0.1")

    def test_recon_to_enum_requires_ports(self):
        planner = Planner(max_steps_per_phase=5)
        phase, note = planner.tick(self.state, new_facts=False)
        self.assertEqual(phase, "recon")
        self.assertIsNone(note)
        self.state.host("127.0.0.1").add_port(80, service="http")
        phase, note = planner.tick(self.state, new_facts=True)
        self.assertEqual(phase, "enum")
        self.assertIn("recon", note)

    def test_enum_requires_surface_not_just_hosts(self):
        planner = Planner(max_steps_per_phase=5)
        planner.phase = "enum"
        self.state.host("127.0.0.1").add_port(22, service="ssh")
        phase, note = planner.tick(self.state, new_facts=False)
        self.assertEqual(phase, "enum", "只有主机与端口不算枚举完成")
        self.state.host("127.0.0.1").technologies.append("ssh")
        phase, _ = planner.tick(self.state, new_facts=True)
        self.assertEqual(phase, "vuln")

    def test_vuln_needs_verified_or_three_steps(self):
        planner = Planner(max_steps_per_phase=6)
        planner.phase = "vuln"
        self.state.findings.append(Finding(id="TRINA-001", title="t", vuln_class="git_leak", status="detected"))
        phase, _ = planner.tick(self.state, new_facts=True)
        self.assertEqual(phase, "vuln", "仅有未验证线索时不应立刻前移")
        self.state.findings[0].status = "verified"
        phase, _ = planner.tick(self.state, new_facts=True)
        self.assertEqual(phase, "exploit")

    def test_budget_forces_advance(self):
        planner = Planner(max_steps_per_phase=2)
        planner.phase = "enum"
        planner.tick(self.state, new_facts=False)
        phase, note = planner.tick(self.state, new_facts=False)
        self.assertEqual(phase, "vuln")
        self.assertIn("预算", note)

    def test_force_advance(self):
        planner = Planner(max_steps_per_phase=9)
        planner.phase = "recon"
        planner.force_advance()
        phase, note = planner.tick(self.state, new_facts=False)
        self.assertEqual(phase, "enum")
        self.assertIn("停滞", note)

    def test_should_finish_after_flags_in_post(self):
        planner = Planner()
        planner.phase = "post"
        self.state.flags.append({"value": "flag{x}"})
        self.assertTrue(planner.should_finish(self.state))
        planner.phase = "recon"
        self.assertFalse(planner.should_finish(self.state))

    def test_directive_mentions_known_ports(self):
        planner = Planner()
        planner.phase = "enum"
        self.state.host("127.0.0.1").add_port(80, service="http")
        directive = planner.directive(self.state)
        self.assertIn("80/http", directive)


# ---------------------------------------------------------------------------
# 目标要求
# ---------------------------------------------------------------------------


class TestGoalRequirements(unittest.TestCase):
    def test_parse_extracts_class_and_path(self):
        from trina.brain.goal import GoalRequirements

        req = GoalRequirements.parse("确认 http://127.0.0.1/sqli 的 SQL 注入并给出证据")
        self.assertIn("sqli", req.classes)
        self.assertIn("/sqli", req.paths)

    def test_nested_and_dotfile_paths(self):
        from trina.brain.goal import GoalRequirements

        req = GoalRequirements.parse("读取 /.git/HEAD 与 /admin/config.php")
        self.assertIn("/.git/head", req.paths)
        self.assertIn("/admin/config.php", req.paths)

    def test_flag_goal_has_no_hard_requirement(self):
        from trina.brain.goal import GoalRequirements

        self.assertTrue(GoalRequirements.parse("请拿到 flag").empty)

    def test_credentials_extracted_from_goal(self):
        from trina.brain.goal import GoalRequirements

        labeled = GoalRequirements.parse("对 127.0.0.1:2222 的 SSH 服务验证弱口令（用户名 trina、口令 trina123）")
        self.assertIn(("trina", "trina123"), labeled.credentials)
        self.assertTrue(any("trina/trina123" in hint for hint in labeled.credential_hints()))
        self.assertIn(("admin", "admin123"), GoalRequirements.parse("验证弱口令 admin/admin123").credentials)
        # 路径里的 "a/b" 不应被当成凭据
        self.assertEqual(GoalRequirements.parse("读取 /admin/config.php").credentials, [])

    def test_port_requirements_extracted_and_ignored_when_unrelated(self):
        from trina.brain.goal import GoalRequirements

        req = GoalRequirements.parse('枚举 127.0.0.1 的端口，必须覆盖 9099(HTTP) 与 2222(SSH)，例如 ports="2222,9099"')
        self.assertEqual(sorted(req.ports), [2222, 9099])
        # 年份/时长/任务编号不是端口
        for text in ("在 2026 年完成，限时 1800 秒", "复现 TSecBench 题 web-0001"):
            self.assertEqual(GoalRequirements.parse(text).ports, [], text)

    def test_port_requirement_blocks_until_scanned(self):
        from trina.brain.goal import GoalRequirements

        state = RunState(run_id="p", target="127.0.0.1")
        req = GoalRequirements.parse("必须覆盖 9099 与 2222 端口")
        self.assertEqual(sorted(req.ports), [2222, 9099])
        self.assertEqual(len(req.unsatisfied(state)), 2)
        state.host("127.0.0.1").add_port(9099, service="http")
        self.assertEqual(len(req.unsatisfied(state)), 1)
        state.host("127.0.0.1").add_port(2222, service="ssh")
        self.assertEqual(req.unsatisfied(state), [])

    def test_class_requirement_needs_evidenced_finding(self):
        from trina.brain.goal import GoalRequirements

        state = RunState(run_id="g", target="127.0.0.1")
        req = GoalRequirements.parse("确认 /sqli 的 SQL 注入")
        self.assertTrue(req.unsatisfied(state))
        # 只有「发现」但没有证据 → 仍不算达成
        state.add_finding({"vuln_class": "sqli", "url": "http://127.0.0.1/sqli", "parameter": "id", "status": "detected"})
        self.assertTrue(req.unsatisfied(state))
        # 已验证 + 有证据 → 达成
        state.add_finding(
            {
                "vuln_class": "sqli",
                "url": "http://127.0.0.1/sqli",
                "parameter": "id",
                "status": "verified",
                "evidence": [{"kind": "http", "target": "http://127.0.0.1/sqli?id=1'"}],
            }
        )
        self.assertFalse(req.unsatisfied(state))

    def test_path_only_requirement_is_hard(self):
        from trina.brain.goal import GoalRequirements

        state = RunState(run_id="g", target="127.0.0.1")
        req = GoalRequirements.parse("读取 /admin/config.php 的内容")
        self.assertFalse(req.classes, "该目标文本不含漏洞类关键词，应只有路径要求")
        self.assertIn("/admin/config.php", req.paths)
        self.assertTrue(req.unsatisfied(state), "无类要求时路径要求是硬条件")
        state.add_finding({"vuln_class": "lfi", "url": "http://127.0.0.1/admin/config.php", "evidence": [{"kind": "http"}]})
        self.assertEqual(req.unsatisfied(state), [])

    def test_path_requirement_is_soft_when_class_present(self):
        from trina.brain.goal import GoalRequirements

        state = RunState(run_id="g", target="127.0.0.1")
        req = GoalRequirements.parse("确认 /sqli 的 SQL 注入，并读取 /robots.txt")
        state.add_finding(
            {"vuln_class": "sqli", "url": "http://127.0.0.1/sqli", "parameter": "id", "status": "verified", "evidence": [{"kind": "http"}]}
        )
        self.assertEqual(req.unsatisfied(state), [])
        self.assertTrue(any("/robots.txt" in hint for hint in req.soft_hints(state)))


# ---------------------------------------------------------------------------
# ReAct 闭环（回放后端，无网络）
# ---------------------------------------------------------------------------


class _FakeTool(Tool):
    spec = ToolSpec(
        name="fake_scan",
        summary="假扫描器",
        category="recon",
        action_level="detect",
        params={"target": ParamSpec("str", "目标", required=True)},
    )

    def run(self, ctx, target):
        return ToolResult(
            ok=True,
            summary=f"扫到 80 端口 @ {target}",
            facts={"ports": [{"host": target, "port": 80, "service": "http"}]},
        )


class _LeakTool(Tool):
    spec = ToolSpec(
        name="fake_leak",
        summary="假 flag 读取",
        category="exploit",
        action_level="verify",
        params={"target": ParamSpec("str", "目标", required=True)},
    )

    def run(self, ctx, target):
        return ToolResult(ok=True, summary="拿到 flag", facts={"flags": [{"value": "flag{test_ok}", "source": "fake"}]})


class _SqliTool(Tool):
    """产出「已验证的 SQL 注入」事实，用于验证目标达成判定。"""

    spec = ToolSpec(
        name="fake_sqli",
        summary="假注入验证",
        category="exploit",
        action_level="verify",
        params={"url": ParamSpec("str", "目标 URL", required=True)},
    )

    def run(self, ctx, url):
        return ToolResult(
            ok=True,
            summary=f"SQL 注入已验证 @ {url}",
            facts={
                "findings": [
                    {
                        "title": "SQL 注入",
                        "vuln_class": "sqli",
                        "url": url,
                        "parameter": "id",
                        "severity": "high",
                        "status": "verified",
                        "evidence": [{"kind": "http", "target": url, "note": "报错回显"}],
                    }
                ]
            },
        )


class TestReActLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = PAIConfig.load()
        self.cfg.run.workspace = str(Path(self.tmp.name) / "runs")
        self.cfg.audit.dir = str(Path(self.tmp.name) / "logs")
        self.cfg.llm.max_steps = 8

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, script: list[dict], tools=None) -> PentestAgent:
        from trina.tools.registry import ToolRegistry

        script_path = Path(self.tmp.name) / "replay.yaml"
        script_path.write_text(json.dumps({"steps": script}, ensure_ascii=False), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(script_path)))
        return PentestAgent(self.cfg, registry=ToolRegistry(tools or [_FakeTool(), _LeakTool(), _SqliTool()]), backend=backend)

    def test_full_loop_reaches_success(self):
        agent = self._agent(
            [
                {"response": '{"thought": "先扫端口", "action": "fake_scan", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "拿 flag", "action": "fake_leak", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "完成", "final": {"status": "success", "summary": "拿到 flag"}}'},
            ]
        )
        state = agent.run("127.0.0.1", goal="拿 flag")
        self.assertEqual(state.status, "success")
        self.assertEqual(len(state.flags), 1)
        self.assertIn(80, state.hosts["127.0.0.1"].ports)
        self.assertEqual(state.counters["llm_calls"], 3)
        self.assertEqual(state.counters["tool_calls"], 2)
        self.assertTrue(Path(state.artifacts["state_file"]).exists())
        # 审计链必须完整
        from trina.audit import AuditLog

        log = AuditLog(Path(state.artifacts["audit_path"]), state.run_id)
        ok, detail = log.verify()
        self.assertTrue(ok, detail)

    def test_out_of_scope_is_blocked_without_llm(self):
        agent = self._agent([{"response": '{"thought": "x", "final": {}}'}])
        state = agent.run("8.8.8.8")
        self.assertEqual(state.status, "blocked")
        self.assertEqual(state.counters["llm_calls"], 0)

    def test_denied_action_gets_hints_and_loop_continues(self):
        # fake_leak 需要 verify 级，这里把上限压到 detect，动作应被拒绝但循环继续
        self.cfg.scope.exploit_level = "detect"
        agent = self._agent(
            [
                {"response": '{"thought": "越级尝试", "action": "fake_leak", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "改走合规路径", "final": {"status": "partial", "summary": "被拒后结束"}}'},
            ]
        )
        state = agent.run("127.0.0.1")
        self.assertGreaterEqual(state.counters["denied"], 1)
        self.assertEqual(state.status, "partial")

    def test_invalid_json_is_recovered(self):
        agent = self._agent(
            [
                {"response": "我不会输出 JSON"},
                {"response": '{"thought": "改正", "action": "fake_scan", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "完成", "final": {"status": "partial", "summary": "ok"}}'},
            ]
        )
        state = agent.run("127.0.0.1")
        self.assertGreaterEqual(state.counters["errors"], 1)
        self.assertIn(80, state.hosts["127.0.0.1"].ports)

    def test_stall_detection_injects_correction(self):
        repeat = '{"thought": "重复", "action": "fake_scan", "params": {"target": "127.0.0.1"}}'
        agent = self._agent([{"response": repeat} for _ in range(8)])
        state = agent.run("127.0.0.1", max_steps=6)
        hints = [item["text"] for item in state.timeline if item["kind"] == "hint"]
        self.assertTrue(any("必须改变策略" in hint for hint in hints), hints)

    def test_flag_shortcut_does_not_satisfy_task_goal(self):
        """目标要求「验证 /sqli 的注入」时，仅拿到 flag 不得判定成功或提前收敛。"""
        agent = self._agent(
            [
                {"response": '{"thought": "直接读 flag", "action": "fake_leak", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "有 flag 了，收工", "final": {"status": "success", "summary": "拿到 flag"}}'},
            ]
        )
        state = agent.run("127.0.0.1", goal="确认 http://127.0.0.1/sqli 的 SQL 注入并给出证据")
        self.assertNotEqual(state.status, "success", "目标未达成时不得判成功")
        self.assertGreaterEqual(state.counters["denied"], 0)
        actions = [step.get("action") for step in state.steps]
        self.assertIn("final_rejected", actions, "提前结束请求应被拒绝一次")
        notes = " ".join(item["text"] for item in state.timeline if item["kind"] == "goal")
        self.assertIn("sqli", notes)

    def test_goal_satisfied_yields_success(self):
        agent = self._agent(
            [
                {"response": '{"thought": "验证注入", "action": "fake_sqli", "params": {"url": "http://127.0.0.1/sqli?id=1"}}'},
                {"response": '{"thought": "完成", "final": {"status": "success", "summary": "注入已验证"}}'},
            ]
        )
        state = agent.run("127.0.0.1", goal="确认 http://127.0.0.1/sqli 的 SQL 注入并给出证据")
        self.assertEqual(state.status, "success")
        self.assertEqual([f.vuln_class for f in state.findings], ["sqli"])

    def test_no_goal_keeps_flag_shortcut_success(self):
        """无明确任务要求时，拿到 flag 仍然是成功（保持原有语义）。"""
        agent = self._agent(
            [
                {"response": '{"thought": "读 flag", "action": "fake_leak", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "完成", "final": {"status": "success", "summary": "拿到 flag"}}'},
            ]
        )
        state = agent.run("127.0.0.1", goal="拿到 flag")
        self.assertEqual(state.status, "success")
        self.assertEqual(len(state.flags), 1)

    def test_mid_run_pivot_to_out_of_scope_is_denied(self):
        """预检只覆盖起始目标：模型中途换目标也必须被逐动作拦下并留痕。"""
        agent = self._agent(
            [
                {"response": '{"thought": "顺手扫隔壁", "action": "fake_scan", "params": {"target": "8.8.8.8"}}'},
                {"response": '{"thought": "收回", "final": {"status": "partial", "summary": "已改回授权范围"}}'},
            ]
        )
        state = agent.run("127.0.0.1")
        self.assertGreaterEqual(state.counters["denied"], 1)
        self.assertNotIn("8.8.8.8", state.hosts)
        from trina.audit import AuditLog

        log = AuditLog(Path(state.artifacts["audit_path"]), state.run_id)
        events = [record.get("event") for record in log.read_all()]
        self.assertIn("guard.deny", events)
        self.assertIn("tool.denied", events)

    def test_record_round_trip_is_valid_json(self):
        """录制物是评测回归的输入，必须是合法 JSON 且能被回放后端直接加载。"""
        agent = self._agent(
            [
                {"response": '{"thought": "扫端口", "action": "fake_scan", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "完成", "final": {"status": "partial", "summary": "ok"}}'},
            ]
        )
        record = Path(self.tmp.name) / "rec.json"
        agent.run("127.0.0.1", record_path=record)
        data = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(len(data["steps"]), 2)
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(record)))
        self.assertEqual(len(backend.script), 2)

    def test_system_prompt_contains_scope_and_tools(self):
        registry = type("R", (), {"prompt_block": lambda self=None: "### fake_scan"})()
        prompt = build_system_prompt(self.cfg.scope_prompt_block(), registry.prompt_block())
        self.assertIn("授权网段", prompt)
        self.assertIn("fake_scan", prompt)
        self.assertIn("禁止破坏性", prompt)


if __name__ == "__main__":
    unittest.main()
