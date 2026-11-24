"""在临时仓库验证自更新，不访问真实镜像或安装到测试解释器。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.cli import main
from trina.updater import UpdateError, _latest_release, _run, _update_checkout, _update_package


def git(root: Path, *args: str) -> str:
    result = _run(["git", *args], root)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class CheckoutUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.upstream = root / "upstream"
        self.checkout = root / "checkout"
        git(root, "init", "--initial-branch=main", str(self.upstream))
        git(self.upstream, "config", "user.name", "Trina Test")
        git(self.upstream, "config", "user.email", "trina@example.test")
        (self.upstream / "app.txt").write_text("old", encoding="utf-8")
        (self.upstream / "local.txt").write_text("default", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-m", "old")
        git(root, "clone", str(self.upstream), str(self.checkout))
        self.old = git(self.checkout, "rev-parse", "HEAD")
        (self.upstream / "app.txt").write_text("new", encoding="utf-8")
        git(self.upstream, "commit", "-am", "new")
        self.new = git(self.upstream, "rev-parse", "HEAD")
        mirror = patch.dict("trina.updater.MIRRORS", {"github": str(self.upstream)})
        mirror.start()
        self.addCleanup(mirror.stop)

    def test_check_reads_remote_and_update_preserves_unrelated_local_files(self):
        log = []
        _update_checkout(self.checkout, "github", True, log.append)
        self.assertIn(self.new[:10], log[0])
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.old)
        check = _run(["git", "cat-file", "-t", self.new], self.checkout)
        self.assertNotEqual(check.returncode, 0)
        (self.checkout / "local.txt").write_text("user edits", encoding="utf-8")
        _update_checkout(self.checkout, "github", False, log.append)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.new)
        self.assertEqual((self.checkout / "app.txt").read_text("utf-8"), "new")
        self.assertEqual((self.checkout / "local.txt").read_text("utf-8"), "user edits")

    def test_update_rejects_overlapping_changes_without_replacing_them(self):
        (self.checkout / "app.txt").write_text("user edits", encoding="utf-8")
        with self.assertRaises(UpdateError):
            _update_checkout(self.checkout, "github", False, lambda _: None)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.old)
        self.assertEqual((self.checkout / "app.txt").read_text("utf-8"), "user edits")

    def test_update_refuses_diverged_main(self):
        git(self.checkout, "-c", "user.name=Trina Test", "-c", "user.email=trina@example.test",
            "commit", "--allow-empty", "-m", "local")
        head = git(self.checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(UpdateError, "分叉"):
            _update_checkout(self.checkout, "github", False, lambda _: None)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)

    def test_check_reports_local_history_that_already_contains_remote(self):
        _update_checkout(self.checkout, "github", False, lambda _: None)
        git(self.checkout, "-c", "user.name=Trina Test", "-c", "user.email=trina@example.test",
            "commit", "--allow-empty", "-m", "ahead")
        log = []
        _update_checkout(self.checkout, "github", True, log.append)
        self.assertIn("已包含远端提交", log[-1])


class PackageUpdateTests(unittest.TestCase):
    def test_release_order_and_annotated_tag(self):
        refs = {"refs/tags/v0.9.0": "1" * 40, "refs/tags/v0.10.0": "2" * 40,
                "refs/tags/v0.10.0^{}": "3" * 40, "refs/tags/v1.0.0-rc1": "4" * 40}
        self.assertEqual(_latest_release(refs), ((0, 10, 0), "v0.10.0", "3" * 40))

    def test_check_only_and_install_is_pinned_to_commit(self):
        sha = "a" * 40
        refs = SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v9.0.0\n", stderr="")
        success = SimpleNamespace(returncode=0, stdout="", stderr="")
        log = []
        with patch("trina.updater._run", return_value=refs) as run:
            _update_package("gitee", True, log.append)
            run.assert_called_once()
            self.assertIn("发现新版", log[-1])
        with patch("trina.updater._run", side_effect=[refs, success]) as run:
            _update_package("gitee", False, log.append)
            argv = run.call_args.args[0]
            self.assertEqual(argv[1:3], ["-m", "pip"])
            self.assertEqual(argv[-1], f"trina @ git+https://gitee.com/YHalo/Trina.git@{sha}")

    def test_cli_dispatches_check_and_mirror(self):
        with patch("trina.updater.update") as update:
            self.assertEqual(main(["update", "--check", "--mirror", "gitee"]), 0)
        update.assert_called_once_with(check=True, mirror="gitee")
