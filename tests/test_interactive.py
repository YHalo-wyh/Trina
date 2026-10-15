"""人机协同测试：审批闸门、实时介入、暂停/停止、交互控制台、一条命令开打。

这些是「人工可介入」这条产品线的对外可感知行为：
没有它们，交互式 CLI 就只是把自动流程包一层壳子。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from trina.brain.agent import PentestAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm.replay_backend import ReplayBackend
from trina.play import build_plan
from trina.session import Session, level_needs_approval
from trina.tools import build_registry
from trina.tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec


class _EchoTool(Tool):
    spec = ToolSpec(
        name="echo_probe",
        summary="无害探针",
        category="recon",
        action_level="verify",
        params={"target": ParamSpec("str", "目标", required=True)},
    )

    def run(self, ctx, target):
        return ToolResult(ok=True, summary=f"probe {target}", facts={"ports": [{"host": target, "port": 80}]})


class TestApprovalModes(unittest.TestCase):
    def test_level_thresholds(self):
        self.assertFalse(level_needs_approval("off", "exploit"))
        self.assertTrue(level_needs_approval("exploit", "exploit"))
        self.assertFalse(level_needs_approval("exploit", "verify"))
        self.assertTrue(level_needs_approval("verify", "verify"))
        self.assertFalse(level_needs_approval("verify", "detect"))
        self.assertTrue(level_needs_approval("all", "detect"))
        self.assertTrue(level_needs_approval("exploit", "unknown-level"))

    def test_answer_releases_waiter(self):
        session = Session(approval_mode="all", approval_timeout=5)
        result: list[bool] = []

        def _waiter():
            result.append(session.request_approval({"tool": "x", "level": "detect", "params": {}}))

        thread = threading.Thread(target=_waiter)
        thread.start()
        for _ in range(50):  # 等 waiter 挂起
            if session.pending:
                break
            time.sleep(0.02)
        self.assertIsNotNone(session.pending)
        self.assertTrue(session.answer(False))
        thread.join(timeout=5)
        self.assertEqual(result, [False])
        self.assertEqual(session.decisions[-1]["tool"], "x")

    def test_timeout_is_fail_closed(self):
        session = Session(approval_mode="all", approval_timeout=0.2)
        allowed = session.request_approval({"tool": "danger", "level": "exploit", "params": {}})
        self.assertFalse(allowed, "无人值守时必须按拒绝处理")
        self.assertTrue(session.decisions[-1]["timeout"])

    def test_off_mode_never_waits(self):
        session = Session(approval_mode="off")
        self.assertTrue(session.request_approval({"tool": "x", "level": "exploit", "params": {}}))
        self.assertEqual(session.decisions, [])


class TestAgentHumanHooks(unittest.TestCase):
    """Agent 侧：审批拒绝 / 人工提示 / 停止信号必须真的生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = TrinaConfig.load().with_overrides(
            run__workspace=str(Path(self.tmp.name) / "runs"),
            audit__dir=str(Path(self.tmp.name) / "logs"),
            policy__mode="free",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, script: list[dict], **kwargs) -> PentestAgent:
        script_path = Path(self.tmp.name) / "replay.json"
        script_path.write_text(json.dumps({"steps": script}, ensure_ascii=False), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(script_path)))
        return PentestAgent(self.cfg, registry=ToolRegistry([_EchoTool()]), backend=backend, **kwargs)

    def test_human_rejection_skips_action(self):
        seen: list[dict] = []
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "被拒后收尾"}}'},
            ],
            approval=lambda request: seen.append(request) or False,
        )
        state = agent.run("127.0.0.1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool"], "echo_probe")
        self.assertIn("human", [item["kind"] for item in state.timeline])
        actions = [step.get("action") for step in state.steps]
        self.assertIn("echo_probe", actions)
        self.assertTrue(any(step.get("denied") for step in state.steps), "被人工拒绝的动作要记为 denied")
        # 拒绝后没有产生事实
        self.assertFalse(state.hosts.get("127.0.0.1") and state.hosts["127.0.0.1"].ports)

    def test_approval_allows_action(self):
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "已批准"}}'},
            ],
            approval=lambda request: True,
        )
        state = agent.run("127.0.0.1")
        self.assertIn(80, state.hosts["127.0.0.1"].ports)

    def test_steering_queue_reaches_agent(self):
        import queue

        steers: "queue.Queue[str]" = queue.Queue()
        steers.put("先看 /upload 的真实路径")
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "ok"}}'},
            ],
            steer_queue=steers,
        )
        state = agent.run("127.0.0.1")
        human_notes = [item["text"] for item in state.timeline if item["kind"] == "human"]
        self.assertTrue(any("先看 /upload" in note for note in human_notes), human_notes)

    def test_stop_event_halts_loop(self):
        stop = threading.Event()
        stop.set()
        agent = self._agent(
            [{"response": '{"thought": "不该执行", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'}],
            stop_event=stop,
        )
        state = agent.run("127.0.0.1", max_steps=3)
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.counters.get("llm_calls", 0), 0, "停止信号应在调用模型前生效")


class TestPlayPlan(unittest.TestCase):
    def test_plan_with_explicit_target(self):
        plan = build_plan(TrinaConfig.load(), target="127.0.0.1:9099", goal="g", autostart_lab=False, log=lambda _m: None)
        self.assertEqual(plan.target, "127.0.0.1:9099")
        self.assertFalse(plan.started_fixtures)

    def test_plan_autostarts_lab_and_reuses_existing(self):
        first = build_plan(TrinaConfig.load(), autostart_lab=True, log=lambda _m: None)
        try:
            self.assertTrue(first.started_fixtures, "空环境下应自动拉起 lab")
            self.assertIn("127.0.0.1:9099", first.target)
            second = build_plan(TrinaConfig.load(), autostart_lab=True, log=lambda _m: None)
            self.assertFalse(second.started_fixtures, "已有靶机时应复用而不是重复拉起")
        finally:
            first.teardown()

    def test_plan_requires_something_to_attack(self):
        with self.assertRaises(RuntimeError):
            build_plan(TrinaConfig.load(), autostart_lab=False, log=lambda _m: None)


class TestConsole(unittest.TestCase):
    """控制台命令分发（不需要真 LLM：只验证解析、状态与错误处理）。"""

    def _console(self, lines: list[str], **kwargs):
        from trina.interactive import Console

        output: list[str] = []
        inputs = iter(lines)

        def _fake_input(_prompt: str) -> str:
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError from None

        console = Console(
            TrinaConfig.load().with_overrides(policy__mode="free"),
            input_fn=_fake_input,
            output=output.append,
            autostart_lab=False,
            **kwargs,
        )
        return console, output

    def test_status_and_help_and_quit(self):
        console, output = self._console(["help", "status", "approve verify", "quit"])
        self.assertEqual(console.run(), 0)
        blob = "\n".join(output)
        self.assertIn("可用命令", blob)
        self.assertIn("审批模式已切换为 verify", blob)
        self.assertEqual(console.session.approval_mode, "verify")

    def test_unknown_command_is_reported(self):
        console, output = self._console(["nope", "quit"])
        console.run()
        self.assertTrue(any("未知命令" in line for line in output))

    def test_revoke_and_grant(self):
        console, output = self._console(["revoke lab-http", "grant lab-http", "quit"])
        console.run()
        blob = "\n".join(output)
        self.assertIn("已撤销 lab-http", blob)
        self.assertIn("已恢复 lab-http", blob)

    def test_run_without_target_explains_usage(self):
        console, output = self._console(["run", "quit"])
        console.run()
        self.assertTrue(any("用法: run <target>" in line for line in output))

    def test_running_state_rejects_unrelated_commands(self):
        console, output = self._console([])
        console.session.thread = threading.Thread(target=lambda: time.sleep(0.5))
        console.session.thread.start()
        console.handle("tools")
        self.assertTrue(any("任务运行中" in line for line in output))
        console.session.thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
