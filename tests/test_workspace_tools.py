"""命令行编码工具必须真实读写、执行并经过统一注册表。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.events import EventBus
from trina.scope import ScopeGuard, execution_budget
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext
from trina.tools.workspace import _is_unbounded_find, _read_source_lines


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"))
        self.registry = build_registry()
        workspace = self.config.workspace_dir / "coding"
        guard = ScopeGuard(self.config.scope)
        self.ctx = ToolContext(
            config=self.config, guard=guard, audit=AuditLog.open(self.config.audit_dir, "coding"),
            state=RunState("coding", "", "code"), http=HttpClient(guard), workspace=workspace,
            tools=self.registry,
        )

    def test_registered_coding_toolchain_writes_edits_reads_and_executes(self):
        for name, legacy in (("shell", "terminal_exec"), ("read", "workspace_read"), ("write", "workspace_write")):
            self.assertIs(self.registry.get(name), self.registry.get(legacy))
            self.assertIsNotNone(self.registry.get(name))
        written = self.registry.execute("write", {
            "path": "hello.py", "content": "print('before')\n", "root": "run",
        }, self.ctx)
        self.assertTrue(written.ok, written.error)
        edited = self.registry.execute("workspace_edit", {
            "path": "hello.py", "old": "before", "new": "after", "root": "run",
        }, self.ctx)
        self.assertTrue(edited.ok, edited.error)
        read = self.registry.execute("read", {"path": "hello.py", "root": "run"}, self.ctx)
        self.assertTrue(read.ok, read.error)
        self.assertIn("print('after')", read.data["text"])
        executed = self.registry.execute("shell", {
            "argv": [sys.executable, "hello.py"], "cwd": "run", "timeout": 10,
        }, self.ctx)
        self.assertTrue(executed.ok, executed.to_observation())
        self.assertEqual(executed.data["stdout"].strip(), "after")

    def test_public_names_share_the_legacy_allowlist_and_audit_identity(self):
        path = self.ctx.workspace / "memo.txt"
        path.write_text("已有内容", encoding="utf-8")
        self.ctx.extra["tool_allowlist"] = {"workspace_read"}
        self.assertTrue(self.registry.execute("read", {"path": path.name, "root": "run"}, self.ctx).ok)
        denied = self.registry.execute("write", {"path": path.name, "root": "run", "content": "未授权改动"}, self.ctx)
        self.assertTrue(denied.denied)
        self.assertEqual(path.read_text(encoding="utf-8"), "已有内容")
        self.ctx.extra["tool_allowlist"] = {"read"}
        self.assertTrue(self.registry.execute("workspace_read", {"path": path.name, "root": "run"}, self.ctx).ok)
        completed = self.ctx.events.replay(kinds=("tool.finished",))
        self.assertEqual([event.payload["tool"] for event in completed],
                         ["workspace_read", "workspace_write", "workspace_read"])

    def test_exploit_script_flag_output_becomes_task_fact(self):
        path = self.ctx.workspace / "exploit.py"
        path.write_text("print('flag{workspace_fact}')\n", encoding="utf-8")
        executed = self.registry.execute("terminal_exec", {
            "argv": [sys.executable, "exploit.py"], "cwd": "run", "timeout": 10,
        }, self.ctx)
        self.assertTrue(executed.ok, executed.to_observation())
        self.assertEqual([item["value"] for item in self.ctx.state.flags], ["flag{workspace_fact}"])

    def test_plain_echo_flag_is_not_promoted_to_task_fact(self):
        executed = self.registry.execute("terminal_exec", {
            "argv": ["echo", "flag{not-authoritative}"], "cwd": "run", "timeout": 10,
        }, self.ctx)
        self.assertTrue(executed.ok, executed.to_observation())
        self.assertFalse(self.ctx.state.flags)

    def test_edit_mismatch_is_non_destructive(self):
        path = self.ctx.workspace / "stable.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("same same", encoding="utf-8")
        result = self.registry.execute("workspace_edit", {
            "path": "stable.txt", "old": "same", "new": "changed", "expected": 1, "root": "run",
        }, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), "same same")

    def test_edit_many_commits_independent_files_and_reports_hashes(self):
        for name, text in (("a.py", "VALUE = 1\n"), ("b.py", "VALUE = 2\n")):
            (self.ctx.workspace / name).write_text(text, encoding="utf-8")
        result = self.registry.execute("workspace_edit_many", {
            "edits": [
                {"path": "a.py", "old": "VALUE = 1", "new": "VALUE = 10", "root": "run"},
                {"path": "b.py", "old": "VALUE = 2", "new": "VALUE = 20", "root": "run"},
            ],
        }, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["count"], 2)
        self.assertTrue(all(item["before_sha256"] != item["after_sha256"] for item in result.data["edits"]))
        self.assertIn("VALUE = 10", (self.ctx.workspace / "a.py").read_text(encoding="utf-8"))
        self.assertIn("VALUE = 20", (self.ctx.workspace / "b.py").read_text(encoding="utf-8"))

    def test_edit_many_validates_all_items_before_writing(self):
        (self.ctx.workspace / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.ctx.workspace / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.registry.execute("workspace_edit_many", {
            "edits": [
                {"path": "a.py", "old": "VALUE = 1", "new": "VALUE = 10", "root": "run"},
                {"path": "b.py", "old": "MISSING", "new": "VALUE = 20", "root": "run"},
            ],
        }, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual((self.ctx.workspace / "a.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertEqual((self.ctx.workspace / "b.py").read_text(encoding="utf-8"), "VALUE = 2\n")

    def test_edit_many_serializes_conflicting_worker_commits(self):
        path = self.ctx.workspace / "shared.py"
        path.write_text("VALUE = 1\n", encoding="utf-8")
        other = ToolContext(
            config=self.config, guard=ScopeGuard(self.config.scope),
            audit=AuditLog.open(self.config.audit_dir, "coding-other"),
            state=RunState("coding-other", "", "code"), http=HttpClient(ScopeGuard(self.config.scope)),
            workspace=self.ctx.workspace, tools=self.registry,
            event_bus=EventBus(Path(self.temp.name) / "other-events.sqlite3", "coding-other"),
        )
        try:
            def edit(ctx, value):
                return self.registry.execute("workspace_edit_many", {
                    "edits": [{"path": "shared.py", "old": "VALUE = 1", "new": f"VALUE = {value}", "root": "run"}],
                }, ctx)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = [future.result() for future in (
                    pool.submit(edit, self.ctx, 2), pool.submit(edit, other, 3),
                )]
            self.assertEqual(sum(result.ok for result in (first, second)), 1)
            self.assertIn(path.read_text(encoding="utf-8"), ("VALUE = 2\n", "VALUE = 3\n"))
            failed = second if first.ok else first
            self.assertTrue("发生变化" in failed.error or "匹配次数为 0" in failed.error)
        finally:
            other.extra["background_jobs"].close()
            other.extra["binary_sessions"].close()
            other.extra["browser_sessions"].close()

    def test_read_many_keeps_short_ctf_sources_in_one_observation(self):
        for name, text in (("README.md", "# fixture\nread me\n"), ("challenge.py", "print('candidate')\n"),
                           ("verifier.py", "print('OK')\n")):
            path = self.ctx.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        result = self.registry.execute("workspace_read_many", {
            "paths": ["README.md", "challenge.py", "verifier.py"], "root": "run",
        }, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertIn("challenge.py", result.data["text"])
        self.assertIn("candidate", result.data["text"])
        self.assertIn("verifier.py", result.data["text"])

    def test_read_many_reports_missing_without_discarding_other_files(self):
        (self.ctx.workspace / "notes.txt").write_text("notes", encoding="utf-8")
        result = self.registry.execute("workspace_read_many", {
            "paths": ["missing.txt", "notes.txt", "missing_again.txt"], "root": "run",
        }, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["files"][0]["path"], "notes.txt")
        self.assertIn("notes", result.data["text"])
        self.assertEqual(result.errors, [
            "文件不存在或不是普通文件: missing.txt",
            "文件不存在或不是普通文件: missing_again.txt",
        ])

    def test_code_source_reads_preserve_lines_without_starting_native_processes(self):
        self.ctx.extra["tool_route"] = "code"
        (self.ctx.workspace / "values.py").write_text("first\nsecond\nthird\n", encoding="utf-8")
        with patch("trina.tools.workspace.native_read") as native:
            single = self.registry.execute("read", {
                "path": "values.py", "start_line": 2, "line_count": 1, "root": "run",
            }, self.ctx)
            batch = self.registry.execute("workspace_read_many", {
                "paths": ["values.py", "missing.py"], "line_count": 1, "root": "run",
            }, self.ctx)
        native.assert_not_called()
        self.assertTrue(single.ok)
        self.assertEqual(single.data["text"], "     2  second")
        self.assertEqual(single.data["total_lines"], 3)
        self.assertEqual(batch.errors, ["文件不存在或不是普通文件: missing.py"])
        self.assertEqual(batch.data["text"], "## values.py\n     1  first")
        self.assertEqual(batch.data["files"], [{"path": "values.py", "total_lines": 3, "returned_lines": 1}])

    def test_code_reads_on_wsl_mount_overlap_and_keep_request_order(self):
        self.ctx.extra["tool_route"] = "code"
        paths = ["second.py", "first.py"]
        for path in paths:
            (self.ctx.workspace / path).write_text(path, encoding="utf-8")
        simultaneous_reads = Barrier(2, timeout=3)

        def read_after_both_start(path, start, count):
            self.assertLessEqual(execution_budget(30), 8)
            simultaneous_reads.wait()
            return _read_source_lines(path, start, count)

        with patch("trina.tools.workspace._is_wsl_windows_path", return_value=True), patch(
                "trina.tools.workspace._read_source_lines", side_effect=read_after_both_start):
            result = self.registry.execute("workspace_read_many", {"paths": paths, "root": "run"}, self.ctx,
                                           timeout=8, call_id="read-batch")

        self.assertTrue(result.ok, result.error)
        self.assertEqual([file["path"] for file in result.data["files"]], paths)
        self.assertEqual(result.data["text"], "## second.py\n     1  second.py\n\n## first.py\n     1  first.py")
        finished = self.ctx.events.replay(kinds=("tool.finished",))
        self.assertEqual([event.call_id for event in finished], ["read-batch"])

    def test_unbounded_find_is_rejected_before_process_start(self):
        self.assertTrue(_is_unbounded_find(["find", "/", "-name", "healthcheckd"]))
        self.assertFalse(_is_unbounded_find(["find", "/workspace", "-name", "healthcheckd"]))
        result = self.registry.execute("terminal_exec", {
            "argv": ["find", "/", "-name", "healthcheckd"], "timeout": 120,
        }, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.category, "invalid")
        self.assertIn("当前工作目录", result.to_observation())

if __name__ == "__main__":
    unittest.main()
