"""命令行编码工具必须真实读写、执行并经过统一注册表。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext


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
        for name in ("terminal_exec", "workspace_read", "workspace_write", "workspace_edit"):
            self.assertIn(name, self.registry.names())
        written = self.registry.execute("workspace_write", {
            "path": "hello.py", "content": "print('before')\n", "root": "run",
        }, self.ctx)
        self.assertTrue(written.ok, written.error)
        edited = self.registry.execute("workspace_edit", {
            "path": "hello.py", "old": "before", "new": "after", "root": "run",
        }, self.ctx)
        self.assertTrue(edited.ok, edited.error)
        read = self.registry.execute("workspace_read", {"path": "hello.py", "root": "run"}, self.ctx)
        self.assertTrue(read.ok, read.error)
        self.assertIn("print('after')", read.data["text"])
        executed = self.registry.execute("terminal_exec", {
            "argv": [sys.executable, "hello.py"], "cwd": "run", "timeout": 10,
        }, self.ctx)
        self.assertTrue(executed.ok, executed.to_observation())
        self.assertEqual(executed.data["stdout"].strip(), "after")

    def test_edit_mismatch_is_non_destructive(self):
        path = self.ctx.workspace / "stable.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("same same", encoding="utf-8")
        result = self.registry.execute("workspace_edit", {
            "path": "stable.txt", "old": "same", "new": "changed", "expected": 1, "root": "run",
        }, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), "same same")


if __name__ == "__main__":
    unittest.main()
