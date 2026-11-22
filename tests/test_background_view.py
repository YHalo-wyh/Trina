"""后台视图验收：真实本地进程、合成 Worker，等待不触发模型。"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from trina.background_view import BackgroundRow, BackgroundWait
from trina.config import TrinaConfig
from trina.console_ui import EnhancedConsoleInput
from trina.interactive import Console
from trina.llm.replay_backend import ReplayBackend
from trina.tools.registry import ToolRegistry
from trina.tools.terminal import TerminalStart


class BackgroundWaitTests(unittest.TestCase):
    def setUp(self):
        self.shell = BackgroundRow("terminal:demo", "chat-a", "running", 0)
        self.worker = BackgroundRow("worker:test", "swarm-a", "pending", 0)
        self.rows = [self.shell, self.worker]
        self.reads = queue.Queue()
        self.results = queue.Queue()
        self.wait = BackgroundWait(self.read, lambda reason, rows: self.results.put((reason, rows)))
        self.addCleanup(self.wait.close)

    def read(self):
        rows = self.rows
        self.reads.put_nowait(rows)
        return rows

    def finish(self, row, status):
        self.rows = [BackgroundRow(item.id, item.owner, status, 1) if item.key == row.key else item
                     for item in self.rows]
        self.wait.changed()

    def test_any_wakes_on_one_completion_without_periodic_reads(self):
        self.wait.start("any", self.rows, 5)
        self.reads.get(timeout=2)
        with self.assertRaises(queue.Empty):
            self.reads.get(timeout=.12)
        self.finish(self.worker, "failed")
        reason, rows = self.results.get(timeout=2)
        self.assertIn("结束", reason)
        self.assertEqual([row.status for row in rows], ["running", "failed"])
        self.wait.changed()
        self.assertTrue(self.results.empty())

    def test_all_waits_for_final_task_and_ignores_duplicate_notifications(self):
        self.wait.start("all", self.rows, 5)
        self.reads.get(timeout=2)
        self.finish(self.shell, "completed")
        self.reads.get(timeout=2)
        self.wait.changed()
        self.assertTrue(self.results.empty())
        self.finish(self.worker, "cancelled")
        reason, rows = self.results.get(timeout=2)
        self.assertEqual(reason, "所选任务均已结束")
        self.assertEqual([row.status for row in rows], ["completed", "cancelled"])
        self.wait.changed()
        self.assertTrue(self.results.empty())

    def test_timeout_returns_state_and_cancel_can_be_followed_by_new_wait(self):
        self.wait.start("all", self.rows, 5)
        self.reads.get(timeout=2)
        self.assertTrue(self.wait.cancel())
        self.assertFalse(self.wait.cancel())
        self.wait.start("all", self.rows, 0)
        reason, rows = self.results.get(timeout=2)
        self.assertEqual(reason, "等待超时")
        self.assertEqual(rows, self.rows)

    def test_reused_ids_in_new_task_group_are_never_consumed(self):
        self.wait.start("any", [self.worker], 5)
        self.reads.get(timeout=2)
        self.rows = [BackgroundRow(self.worker.id, "swarm-b", "completed", 0)]
        self.wait.changed()
        reason, rows = self.results.get(timeout=2)
        self.assertIn("视图已更新", reason)
        self.assertEqual(rows, [])

    def test_close_discards_pending_wait_and_late_events(self):
        self.wait.start("all", self.rows, 5)
        self.reads.get(timeout=2)
        self.wait.close()
        self.finish(self.shell, "completed")
        self.finish(self.worker, "completed")
        self.assertIsNone(self.wait.activity())
        with self.assertRaises(queue.Empty):
            self.results.get(timeout=.08)


class BackgroundConsoleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        cfg = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"),
                                           ui__completion_notify=False, ui__error_notify=False)
        self.backend = ReplayBackend(cfg.llm)
        self.output = []
        self.console = Console(cfg, registry=ToolRegistry([TerminalStart()]), autostart_lab=False,
                               backend=self.backend, output=self.output.append, plain=True)
        self.addCleanup(self.console._cleanup)

    def start_shell(self, body, timeout=10):
        chat = self.console._ensure_chat()
        call = chat.registry.execute("terminal_start", {
            "argv": [sys.executable, "-u", "-c", body], "timeout": timeout,
        }, chat.ctx)
        self.assertTrue(call.ok, call.error)
        return chat, call.data["job_id"]

    def worker(self, status):
        row = {"goal": "整理单元测试", "model": "", "reasoning_effort": "", "profile": "",
               "started_at": time.time(), "ended_at": None, "status": status}
        self.console.subworkers._update({"run_id": "demo-worker-run", "tasks": {"unit-tests": row}})

    def test_list_escape_wait_and_stop_real_job(self):
        chat, job_id = self.start_shell("import time; print('ready'); time.sleep(8)")
        self.worker("running")
        self.console.handle("/jobs list")
        text = "\n".join(self.output)
        self.assertIn("terminal:" + job_id, text)
        self.assertIn("worker:unit-tests", text)
        choices = self.console._completion_values("jobs", ["logs"])
        self.assertEqual({item.value for item in choices}, {"terminal:" + job_id, "worker:unit-tests"})
        self.console.handle("/jobs wait all 5")
        self.assertEqual(self.console._activity()[0], "background_wait")

        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "", running=lambda: False,
                                          dynamic_values=lambda *_: (), cancel_wait=self.console._cancel_job_wait)
            editor.buffer.text = "保留输入"
            bindings = editor.app.key_bindings.get_bindings_for_keys((Keys.Escape,))
            bindings[-1].handler(SimpleNamespace())
            self.assertEqual(editor.buffer.text, "保留输入")
        self.assertIsNone(self.console._job_wait.activity())
        self.assertEqual(chat.ctx.extra["background_jobs"].snapshot(job_id)[0]["status"], "running")
        self.console.handle(f"/jobs stop terminal:{job_id}")
        self.assertEqual(chat.ctx.extra["background_jobs"].snapshot(job_id)[0]["status"], "cancelled")
        self.assertIn("已取消", "\n".join(self.output))
        self.assertEqual(self.backend.ledger.calls, 0)

    def test_worker_and_shell_events_resolve_one_wait_and_logs_are_available(self):
        chat, job_id = self.start_shell("import time; time.sleep(.15); print('test output'); raise SystemExit(3)")
        self.worker("running")
        completion = threading.Event()
        original = self.console._job_wait._on_done

        def report(reason, rows):
            original(reason, rows)
            completion.set()

        self.console._job_wait._on_done = report
        self.console.handle(f"/jobs wait all 5 terminal:{job_id} worker:unit-tests")
        self.worker("completed")
        self.assertTrue(completion.wait(5))
        self.console.handle(f"/jobs logs terminal:{job_id}")
        text = "\n".join(self.output)
        self.assertEqual(text.count("所选任务均已结束"), 1)
        self.assertIn("退出码 3", text)
        self.assertIn("test output", text)
        self.assertEqual(len(chat.events.replay(kinds=("job.finished",))), 1)
        self.assertEqual(self.backend.ledger.calls, 0)


if __name__ == "__main__":
    unittest.main()
