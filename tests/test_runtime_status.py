"""末尾状态栏与真实后台程序的生命周期验证。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMResponse
from trina.llm.replay_backend import ReplayBackend
from trina.runtime_status import render_agent_status, status_snapshot
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.registry import ToolContext


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))
        self.audit = AuditLog.open(self.root, run_id="background")
        self.registry = build_registry(["terminal"])
        self.ctx = ToolContext(config=self.config, guard=ScopeGuard(self.config.scope, audit=self.audit), audit=self.audit,
                               state=RunState(run_id="test", target="127.0.0.1"), http=None, workspace=self.root, tools=self.registry)
        self.jobs = self.ctx.extra["background_jobs"]
        self.addCleanup(self.jobs.close)

    def start(self, code, timeout=5):
        result = self.registry.execute("terminal_start", {"argv": [sys.executable, "-u", "-c", code], "timeout": timeout}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.data["code"], "启动成功不应伪造进程退出码")
        return result.data["job_id"]

    def test_background_progress_completion_and_nonzero_exit_are_real(self):
        job_id = self.start("import time; print('started', flush=True); time.sleep(.3); print('finished')")
        first = status_snapshot(self.ctx)
        self.assertEqual(first["background_jobs"][0]["status"], "running")
        self.assertIsNone(first["background_jobs"][0]["exit_code"])
        done = self.registry.execute("terminal_status", {"job_id": job_id, "wait_seconds": 2}, self.ctx)
        self.assertTrue(done.ok, done.error)
        self.assertEqual(done.data["jobs"][0]["exit_code"], 0)
        self.assertIn("finished", done.data["jobs"][0]["stdout_tail"])
        failed_id = self.start("import sys; print('bad input', file=sys.stderr); sys.exit(7)")
        failed = self.registry.execute("terminal_status", {"job_id": failed_id, "wait_seconds": 2}, self.ctx)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.data["jobs"][0]["exit_code"], 7)
        self.assertEqual(failed.data["jobs"][0]["status"], "failed")
        self.assertTrue(self.audit.verify()[0])

    def test_timeout_cancel_and_session_isolation(self):
        timed = self.start("import time; time.sleep(10)", timeout=.1)
        self.jobs.wait(timed, 2)
        self.assertEqual(self.jobs.snapshot(timed)[0]["status"], "timed_out")
        other = ToolContext(config=self.config, guard=self.ctx.guard, audit=self.audit, state=RunState(run_id="other", target=""),
                            http=None, workspace=self.root / "other")
        self.addCleanup(other.extra["background_jobs"].close)
        with self.assertRaises(ValueError):
            other.extra["background_jobs"].snapshot(timed)
        running = self.start("import time; time.sleep(10)")
        self.jobs.close()
        self.assertEqual(self.jobs.snapshot(running)[0]["status"], "cancelled")
        self.assertIsNotNone(self.jobs.snapshot(running)[0]["exit_code"])

    def test_statebar_is_tail_message_data_and_does_not_rewrite_prefix(self):
        backend = ReplayBackend(LLMConfig(backend="replay"))
        messages = []
        def complete(history, **kwargs):
            messages.append([dict(item) for item in history])
            return LLMResponse(text="收到")
        backend.complete_messages = complete
        chat = ChatAgent(self.config, registry=self.registry, backend=backend)
        self.addCleanup(chat.close)
        chat.ask("first")
        chat.ask("second")
        self.assertEqual(messages[1][:len(messages[0])], messages[0])
        self.assertIn("Agent 状态栏", messages[-1][-1]["content"])
        payload = json.loads(messages[-1][-1]["content"].splitlines()[-1])
        self.assertEqual(payload["progress"]["turn"], 2)
        self.assertEqual(payload["tools"]["calls"], 0)
        self.assertNotIn("Agent 状态栏", messages[-1][0]["content"])

    def test_snapshot_is_bounded_redacted_and_round_trips(self):
        render_agent_status(self.ctx, step=2, max_steps=6, observation="password=secret-123\n" + "x" * 5000)
        self.assertLess(len(json.dumps(self.ctx.state.runtime_status)), 2500)
        restored = RunState.from_dict(self.ctx.state.to_dict())
        self.assertEqual(restored.runtime_status["progress"]["step"], 2)
        text = render_agent_status(self.ctx, observation="password=secret-123\nnormal output")
        self.assertNotIn("secret-123", text)

    def test_background_child_processes_are_cancelled_with_parent(self):
        heartbeat = self.root / "heartbeat.txt"
        child = "import time,pathlib; p=pathlib.Path(" + repr(str(heartbeat)) + "); i=0\nwhile True:\n i+=1; p.write_text(str(i)); time.sleep(.05)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c'," + repr(child) + "]); time.sleep(20)"
        job = self.start(parent, timeout=20)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not heartbeat.exists() or len(heartbeat.read_text()) == 0):
            time.sleep(.02)
        self.assertTrue(heartbeat.exists(), "子进程必须真实启动")
        self.jobs.stop(job)
        before = heartbeat.stat().st_mtime_ns
        time.sleep(.2)
        self.assertEqual(heartbeat.stat().st_mtime_ns, before, "取消父作业后子进程不得继续运行")

    def test_background_environment_does_not_inherit_model_keys(self):
        with patch.dict(os.environ, {"TRINA_LLM_API_KEY": "test-private-key"}):
            job = self.start("import os; print('inherited' if 'TRINA_LLM_API_KEY' in os.environ else 'absent')")
            self.jobs.wait(job, 2)
        self.assertEqual(self.jobs.snapshot(job)[0]["stdout_tail"].strip(), "absent")

    def test_restricted_mode_rejects_start_before_any_process_launch(self):
        self.config.scope.trust_mode = "restricted"
        self.config.scope.exploit_level = "verify"
        result = self.registry.execute("terminal_start", {"argv": [sys.executable, "-c", "print('not run')"]}, self.ctx)
        self.assertTrue(result.denied)
        self.assertEqual(self.jobs.snapshot(), [])

    def test_timed_out_start_call_cancels_only_its_own_background_job(self):
        from trina.tools.external import run_external
        from trina.tools.registry import Tool, ToolRegistry, ToolSpec
        previous = self.start("import time; time.sleep(20)", timeout=20)
        class SlowStart(Tool):
            spec = ToolSpec(name="slow_start", summary="验证超时回收", action_level="detect")
            def run(self, ctx):
                run_external([sys.executable, "-c", "import time; time.sleep(20)"], ctx,
                             tool="slow_start", background=True, timeout=20)
                time.sleep(.5)
                return None
        result = ToolRegistry([SlowStart()]).execute("slow_start", {}, self.ctx, timeout=.2)
        self.assertFalse(result.ok)
        rows = self.jobs.snapshot()
        own = next(row for row in rows if row["tool"] == "slow_start")
        self.assertEqual(own["status"], "cancelled")
        self.assertIsNotNone(own["exit_code"])
        self.assertEqual(self.jobs.snapshot(previous)[0]["status"], "running")

    def test_failed_start_result_does_not_leave_unreported_process(self):
        from trina.tools.external import run_external
        from trina.tools.registry import Tool, ToolRegistry, ToolResult, ToolSpec
        class FailedStart(Tool):
            spec = ToolSpec(name="failed_start", summary="验证失败启动回收", action_level="detect")
            def run(self, ctx, invalid=False):
                run_external([sys.executable, "-c", "import time; time.sleep(20)"], ctx,
                             tool="failed_start", background=True, timeout=20)
                return None if invalid else ToolResult(ok=False, error="未交付作业标识")
        tool = FailedStart()
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                tool.run = lambda ctx, flag=invalid: FailedStart.run(tool, ctx, invalid=flag)
                result = ToolRegistry([tool]).execute("failed_start", {}, self.ctx)
                self.assertFalse(result.ok)
                self.assertTrue(all(row["status"] == "cancelled" for row in self.jobs.snapshot()))
                self.assertTrue(all(row["exit_code"] is not None for row in self.jobs.snapshot()))

    def test_chat_reads_running_job_at_request_tail_then_observes_completion(self):
        backend = ReplayBackend(LLMConfig(backend="replay"))
        snapshots = []
        def complete(history, **kwargs):
            snapshots.append(json.loads(history[-1]["content"].splitlines()[-1]))
            if len(snapshots) == 1:
                return LLMResponse(text=json.dumps({"action": "terminal_start", "params": {
                    "argv": [sys.executable, "-u", "-c", "import time; print('work started', flush=True); time.sleep(.8); print('done')"]}}))
            return LLMResponse(text="后台执行中")
        backend.complete_messages = complete
        chat = ChatAgent(self.config, registry=self.registry, backend=backend)
        self.addCleanup(chat.close)
        self.assertEqual(chat.ask("启动测试程序"), "后台执行中")
        running = snapshots[-1]["background_jobs"]
        self.assertEqual(running[0]["status"], "running")
        self.assertEqual(snapshots[-1]["tools"]["calls"], 1)
        chat.ctx.extra["background_jobs"].wait(running[0]["id"], 2)
        chat.ask("核对执行情况")
        self.assertEqual(snapshots[-1]["background_jobs"][0]["status"], "completed")
        self.assertEqual(snapshots[-1]["background_jobs"][0]["exit_code"], 0)
        self.assertIn("done", snapshots[-1]["background_jobs"][0]["stdout_tail"])


if __name__ == "__main__":
    unittest.main()
