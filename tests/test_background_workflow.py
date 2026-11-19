"""真实后台命令与独立工作交错推进，终态证据必须可核验。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMResponse
from trina.llm.base import ToolCall
from trina.llm.replay_backend import ReplayBackend
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.casebook import resolve_evidence
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext


class BackgroundWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"))
        self.registry = build_registry()
        guard = ScopeGuard(self.config.scope)
        self.ctx = ToolContext(config=self.config, guard=guard, audit=AuditLog.open(self.config.audit_dir, "background"),
                               state=RunState("background", "", "analysis"), http=HttpClient(guard),
                               workspace=root / "work", tools=self.registry)
        self.addCleanup(self.ctx.extra["background_jobs"].close)

    def execute(self, tool, **params):
        return self.registry.execute(tool, params, self.ctx)

    def start(self, code, timeout=10):
        result = self.execute("terminal_start", argv=[sys.executable, "-c", code], timeout=timeout)
        self.assertTrue(result.ok, result.to_observation())
        return result.data["job_id"]

    def test_model_advances_independent_tool_before_background_result(self):
        backend = ReplayBackend(LLMConfig(backend="replay"))
        chat = ChatAgent(self.config, registry=self.registry, backend=backend)
        self.addCleanup(chat.close)
        chat.ctx.extra["disable_title_generation"] = True
        code = "from pathlib import Path; import time\nwhile not Path('release.txt').exists(): time.sleep(.02)\nprint('completed after independent work')"
        calls = []
        def response(_messages, **_kwargs):
            index = len(calls)
            calls.append(index)
            if index == 0:
                name, params = "terminal_start", {"argv": [sys.executable, "-c", code], "timeout": 10}
            elif index == 1:
                rows = chat.ctx.extra["background_jobs"].snapshot()
                self.assertEqual(rows[0]["status"], "running")
                self.assertFalse(rows[0]["finished"])
                self.assertFalse(chat.state.evidence)
                name, params = "workspace_write", {"path": "release.txt", "content": "independent work", "root": "run"}
            elif index == 2:
                self.assertTrue((chat.ctx.workspace / "release.txt").exists())
                job_id = chat.ctx.extra["background_jobs"].snapshot()[0]["id"]
                name, params = "terminal_status", {"job_id": job_id, "wait_seconds": 5}
            else:
                evidence = chat.state.evidence[-1]
                self.assertTrue(evidence["successful"])
                self.assertEqual(evidence["exit_code"], 0)
                self.assertIn("completed after independent work", Path(evidence["path"]).read_text(encoding="utf-8"))
                return LLMResponse(text="独立工作与后台命令均已完成。")
            return LLMResponse(tool_calls=[ToolCall(f"call-{index}", name, json.dumps(params))])
        backend.complete_messages = response
        chat.ask("启动分析，等待期间独立生成文件，然后检查命令结果")
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(resolve_evidence(chat.ctx, [chat.state.evidence[-1]["id"]])), 1)

    def test_only_finished_job_has_stable_evidence_and_tampering_is_rejected(self):
        job_id = self.start("from pathlib import Path; import time\nwhile not Path('release').exists(): time.sleep(.02)\nprint('real output')")
        running = self.execute("terminal_status", job_id=job_id)
        self.assertNotIn("evidence_id", running.data)
        (self.ctx.workspace / "release").write_text("go", encoding="utf-8")
        done = self.execute("terminal_status", job_id=job_id, wait_seconds=5)
        self.assertTrue(done.ok, done.to_observation())
        evidence_id = done.data["evidence_id"]
        evidence = resolve_evidence(self.ctx, [evidence_id])[0]
        self.assertTrue(evidence["output_is_tail"])
        count = len(self.ctx.state.evidence)
        self.assertEqual(self.execute("terminal_status", job_id=job_id).data["evidence_id"], evidence_id)
        self.assertEqual(len(self.ctx.state.evidence), count)
        Path(evidence["path"]).write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "被修改"):
            resolve_evidence(self.ctx, [evidence_id])

    def test_failed_timeout_and_cancelled_jobs_cannot_confirm_findings(self):
        for mode in ("failed", "timed_out", "cancelled"):
            with self.subTest(mode=mode):
                job_id = self.start("raise SystemExit(3)" if mode == "failed" else "import time; time.sleep(30)",
                                    timeout=.15 if mode == "timed_out" else 10)
                if mode == "cancelled":
                    self.ctx.extra["background_jobs"].close()
                result = self.execute("terminal_status", job_id=job_id, wait_seconds=5)
                self.assertFalse(result.ok)
                self.assertEqual(result.data["jobs"][0]["status"], mode)
                with self.assertRaisesRegex(ValueError, "执行失败"):
                    resolve_evidence(self.ctx, [result.data["evidence_id"]])


if __name__ == "__main__":
    unittest.main()
