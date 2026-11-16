"""真实 Git 工作流与命令参数回归；只操作隔离临时仓库。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.config import TrinaConfig
from trina.interactive import Console
from trina.llm.replay_backend import ReplayBackend
from trina.repo_ui import parse_repo_args
from trina.tools import build_registry


@unittest.skipUnless(shutil.which("git"), "需要 Git")
class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.cwd = patch("pathlib.Path.cwd", return_value=self.project)
        self.cwd.start()
        self.addCleanup(self.cwd.stop)
        config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))
        self.output = []
        self.backend = ReplayBackend(config.llm)
        self.console = Console(config, registry=build_registry(["workspace"]), backend=self.backend,
                               output=self.output.append, autostart_lab=False)
        self.addCleanup(self.console._cleanup)
        self.chat = self.console._ensure_chat()
        self.shell("git", "init", "-b", "main")
        self.shell("git", "config", "user.name", "Fixture")
        self.shell("git", "config", "user.email", "fixture@example.test")

    def shell(self, *argv):
        result = self.chat.registry.execute("terminal_exec", {"argv": list(argv)}, self.chat.ctx)
        self.assertTrue(result.ok, result.error)
        return result

    def git(self, action, **params):
        return self.chat.registry.execute("workspace_git", {"action": action, **params}, self.chat.ctx)

    def write(self, path, content):
        result = self.chat.registry.execute("workspace_write", {"path": path, "content": content}, self.chat.ctx)
        self.assertTrue(result.ok, result.error)

    def commit_base(self):
        self.write("main.py", "print('base')\n")
        self.assertTrue(self.git("stage", paths=["main.py"]).ok)
        self.assertTrue(self.git("commit", message="base").ok)

    def test_status_stage_diff_commit_log_and_literal_paths(self):
        self.write("中文 file.txt", "first\n")
        self.write("--help.txt", "literal\n")
        status = self.git("status")
        self.assertTrue(status.ok, status.error)
        self.assertEqual({row["path"] for row in status.data["changes"]}, {"中文 file.txt", "--help.txt"})
        self.assertTrue(self.git("stage", paths=["中文 file.txt", "--help.txt"]).ok)
        before = self.chat.ctx.events.last_seq
        diff = self.git("diff", staged=True, paths=["中文 file.txt"])
        self.assertTrue(diff.ok, diff.error)
        self.assertIn("+first", diff.data["output"])
        self.assertNotIn("+literal", diff.data["output"])
        live = "".join(event.payload.get("delta", "") for event in self.chat.ctx.events.replay(before, kinds=("tool.output",)))
        self.assertIn("+first", live)
        self.assertNotIn(str(self.project), live, "辅助 rev-parse 输出不应混入 Diff 窗口")
        self.assertTrue(self.git("commit", message="测试提交").ok)
        self.assertEqual(self.git("status").data["changes"], [])
        self.assertIn("测试提交", self.git("log", limit=1).data["output"])
        self.write("中文 file.txt", "second\n")
        self.assertIn("-first", self.git("diff").data["output"])
        self.assertIn("+second", self.git("diff").data["output"])

    def test_unstage_before_first_commit_keeps_the_file(self):
        self.write("first.txt", "keep\n")
        self.assertTrue(self.git("stage", paths=["first.txt"]).ok)
        result = self.git("unstage", paths=["first.txt"])
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.project / "first.txt").read_text(), "keep\n")
        self.assertEqual(self.git("status").data["changes"][0]["index"], "?")

    def test_conflicting_branch_switch_and_unmerged_delete_preserve_work(self):
        self.commit_base()
        self.assertTrue(self.git("create", branch="feature/test").ok)
        self.write("main.py", "print('feature')\n")
        self.assertTrue(self.git("stage", paths=["main.py"]).ok)
        self.assertTrue(self.git("commit", message="feature").ok)
        self.assertTrue(self.git("switch", branch="main").ok)
        self.write("main.py", "local draft\n")
        self.assertFalse(self.git("switch", branch="feature/test").ok)
        self.assertFalse(self.git("delete", branch="feature/test").ok)
        self.assertEqual((self.project / "main.py").read_text(), "local draft\n")
        self.assertIn("feature/test", self.git("branches").data["branches"])

    def test_worktree_isolation_commit_and_clean_removal(self):
        self.commit_base()
        self.write("source-only.txt", "uncommitted\n")
        added = self.git("worktree_add", path="trees/isolated", branch="feature/isolated")
        self.assertTrue(added.ok, added.error)
        worktree = self.project / "trees" / "isolated"
        self.assertFalse((worktree / "source-only.txt").exists())
        entries = self.git("worktrees").data["worktrees"]
        self.assertEqual(len(entries), 2)
        self.write("trees/isolated/child.txt", "child\n")
        execution = self.chat.registry.execute("terminal_exec", {"argv": [sys.executable, "-c",
            "from pathlib import Path; print(Path.cwd()); print(Path('child.txt').read_text())"],
            "working_directory": str(worktree)}, self.chat.ctx)
        self.assertTrue(execution.ok, execution.error)
        self.assertIn(str(worktree), execution.data["stdout"])
        self.assertIn("child", execution.data["stdout"])
        self.assertFalse(self.git("worktree_remove", path=str(worktree)).ok)
        self.assertTrue((worktree / "child.txt").exists())
        self.assertTrue(self.git("stage", repository=str(worktree), paths=["child.txt"]).ok)
        self.assertTrue(self.git("commit", repository=str(worktree), message="isolated commit").ok)
        self.assertNotIn("isolated commit", self.git("log").data["output"])
        self.assertTrue(self.git("worktree_remove", path=str(worktree)).ok)
        self.assertEqual(len(self.git("worktrees").data["worktrees"]), 1)
        self.assertTrue(self.git("worktree_add", path=str(worktree), branch="feature/isolated", new_branch=False).ok)
        self.assertIn("isolated commit", self.git("log", repository=str(worktree)).data["output"])
        self.assertFalse(self.git("worktree_remove", path=str(self.project)).ok)
        self.assertTrue(self.git("worktree_remove", path=str(worktree)).ok)
        self.assertEqual((self.project / "source-only.txt").read_text(), "uncommitted\n")

    def test_local_bare_remote_push_fetch_and_fast_forward_pull(self):
        self.commit_base()
        remote = self.root / "remote.git"
        self.shell("git", "init", "--bare", str(remote))
        self.shell("git", "remote", "add", "origin", str(remote))
        self.assertEqual(self.git("remotes").data["remotes"], ["origin"])
        for action in ("push", "fetch", "pull"):
            result = self.git(action)
            self.assertTrue(result.ok, f"{action}: {result.error}")
        self.assertIn("base", self.shell("git", "--git-dir", str(remote), "log", "main", "--oneline").data["stdout"])
        self.assertFalse(self.git("push", remote="missing").ok)
        peer = self.project / "peer"
        self.shell("git", "clone", "-b", "main", str(remote), str(peer))
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.test")):
            self.shell("git", "-C", str(peer), "config", key, value)
        self.write(str(peer / "main.py"), "remote change\n")
        self.shell("git", "-C", str(peer), "add", "main.py")
        self.shell("git", "-C", str(peer), "commit", "-m", "remote change")
        self.shell("git", "-C", str(peer), "push", "origin", "main")
        self.write("main.py", "local change\n")
        self.assertTrue(self.git("stage", paths=["main.py"]).ok)
        self.assertTrue(self.git("commit", message="local change").ok)
        self.assertFalse(self.git("pull").ok, "分叉历史不得自动合并或重写")
        self.assertFalse(self.git("push").ok, "不得强推覆盖远端")
        self.assertEqual((self.project / "main.py").read_text(), "local change\n")

    def test_invalid_arguments_never_stage_or_switch_and_busy_writes_fail(self):
        self.commit_base()
        self.write("draft.txt", "draft\n")
        for params in ({"action": "stage", "paths": []}, {"action": "stage", "paths": ["../outside"]},
                       {"action": "switch", "branch": "--help"}, {"action": "create", "branch": "bad..name"},
                       {"action": "commit", "message": ""}, {"action": "log", "limit": 101}):
            self.assertFalse(self.git(**params).ok, params)
        self.chat.ctx.extra["repository_busy"] = lambda: True
        self.assertFalse(self.git("stage", paths=["draft.txt"]).ok)
        self.assertTrue(self.git("status").ok)
        self.assertNotIn("draft.txt", self.git("diff", staged=True).data["output"])

    def test_console_commands_and_completion_do_not_call_the_model(self):
        self.commit_base()
        self.write("中文 file.txt", "draft\n")
        self.console._cmd_repo(["status"])
        self.console._cmd_branch(["new", "feature/ui"])
        self.assertEqual(self.console._branch, "feature/ui")
        self.console._cmd_branch(["list"])
        self.assertIn("main", self.console._git_branches)
        with patch.object(self.chat.registry, "execute") as execute:
            self.assertIn("main", [choice.value for choice in self.console._completion_values("branch", ["switch"])])
            self.assertEqual([choice.value for choice in self.console._completion_values("repo", ["stage"])], ["'中文 file.txt'"])
            execute.assert_not_called()
        self.assertEqual(self.console.view.usage_data()["calls"], 0)


class RepositoryCommandTests(unittest.TestCase):
    def test_paths_quotes_flags_and_worktree_arguments(self):
        result = parse_repo_args(["diff", "中文 file.txt", "--staged", "--repo", "C:/项目"])
        self.assertEqual(result["paths"], ["中文 file.txt"])
        self.assertTrue(result["staged"])
        self.assertEqual(result["repository"], "C:/项目")
        self.assertEqual(parse_repo_args(["commit", "fix: repair parser"])["message"], "fix: repair parser")
        tree = parse_repo_args(["add", "../trees/test", "feature/test", "--base", "main"], "worktree")
        self.assertEqual((tree["action"], tree["branch"], tree["base"]), ("worktree_add", "feature/test", "main"))
        for args in (["unknown"], ["log", "junk"], ["commit"], ["log", "--limit", "oops"]):
            with self.assertRaises(ValueError):
                parse_repo_args(args)
