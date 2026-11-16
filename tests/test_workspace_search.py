"""Bash 与工作区 rg 检索能力必须经过统一工具链真实执行。"""
from __future__ import annotations

import shutil
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


class WorkspaceSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"))
        self.registry = build_registry()
        guard = ScopeGuard(self.config.scope)
        workspace = self.config.workspace_dir / "search"
        workspace.mkdir(parents=True)
        self.ctx = ToolContext(config=self.config, guard=guard,
            audit=AuditLog.open(self.config.audit_dir, "search"), state=RunState("search", "", "code"),
            http=HttpClient(guard), workspace=workspace, tools=self.registry)

    def test_search_is_registered_and_returns_structured_locations(self) -> None:
        self.assertIn("workspace_search", self.registry.names())
        (self.ctx.workspace / "src").mkdir()
        (self.ctx.workspace / "src" / "main.py").write_text("alpha\nneedle_value = 1\n", encoding="utf-8")
        (self.ctx.workspace / "notes.txt").write_text("NEEDLE value\n", encoding="utf-8")
        result = self.registry.execute("workspace_search", {
            "query": "needle", "path": str(self.ctx.workspace), "globs": ["*.py"], "fixed": True,
        }, self.ctx)
        self.assertTrue(result.ok, result.to_observation())
        self.assertIn(result.data["engine"], {"ripgrep", "python"})
        self.assertEqual([(item["path"], item["line"], item["column"]) for item in result.data["matches"]],
                         [("src/main.py", 2, 1)])

    def test_search_rejects_escape_and_bounds_results(self) -> None:
        escaped = self.registry.execute("workspace_search", {"query": "x", "path": ".."}, self.ctx)
        self.assertFalse(escaped.ok)
        invalid = self.registry.execute("workspace_search", {
            "query": "x", "path": str(self.ctx.workspace), "max_results": 501,
        }, self.ctx)
        self.assertFalse(invalid.ok)

    @unittest.skipUnless(shutil.which("bash"), "系统未安装 bash")
    def test_terminal_exec_runs_basic_bash(self) -> None:
        result = self.registry.execute("terminal_exec", {
            "argv": ["bash", "-lc", "printf trina-bash-ok"], "cwd": "run", "timeout": 15,
        }, self.ctx)
        self.assertTrue(result.ok, result.to_observation())
        self.assertIn("trina-bash-ok", result.data["stdout"])


if __name__ == "__main__":
    unittest.main()
