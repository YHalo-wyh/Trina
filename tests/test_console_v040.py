"""0.4.0 真实分支编辑、角色会话隔离、底栏溢出和首页动画回归。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.config import ConfigError, TrinaConfig
from trina.console_state import ConsoleContext, ConsoleState, _cells
from trina.interactive import Console
from trina.llm.base import LLMUsage
from trina.llm.replay_backend import ReplayBackend
from trina.tools import build_registry


class ConsoleV040Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))

    def test_worker_switch_restores_history_role_and_usage_without_model_call(self):
        backend = ReplayBackend(self.config.llm)
        backend.script = [{"response": "Commander 完成"}, {"response": "代码完成"}]
        console = Console(self.config, output=lambda _: None, registry=build_registry(), backend=backend)
        self.addCleanup(console._cleanup)
        first = console._ensure_chat()
        first.ctx.extra["disable_title_generation"] = True
        first.ask("检查任务")
        first_id = first.run_id
        console._cmd_worker(["code"])
        second = console._ensure_chat()
        second.ctx.extra["disable_title_generation"] = True
        second.ask("修改代码")
        second_id = second.run_id
        self.assertNotEqual(first_id, second_id)
        self.assertIn("工程编码", second._messages[0]["content"])
        self.assertFalse(any(item.get("content") == "检查任务" for item in second.history))
        self.assertEqual(console._footer_context().role, "Code Worker")
        console._cmd_worker(["commander"])
        self.assertEqual(console.chat.run_id, first_id)
        self.assertEqual(console.chat.worker_role, "commander")
        self.assertEqual(console.chat.turns, 1)
        console._cmd_worker(["code"])
        self.assertEqual(console.chat.run_id, second_id)
        self.assertEqual(console.chat.worker_role, "code")
        self.assertEqual(console.chat.turns, 1)

    def test_real_git_main_edit_and_branch_switch_preserve_files(self):
        console = Console(self.config, output=lambda _: None, registry=build_registry(), backend=ReplayBackend(self.config.llm))
        self.addCleanup(console._cleanup)
        chat = console._ensure_chat()
        project = self.root / "project"
        project.mkdir()
        with patch("trina.tools.workspace.Path.cwd", return_value=project):
            def execute(argv):
                result = chat.registry.execute("terminal_exec", {"argv": argv}, chat.ctx)
                self.assertTrue(result.ok, result.to_observation())
            execute(["git", "init", "-b", "main"])
            execute(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "initial"])
            written = chat.registry.execute("workspace_write", {"path": "main.py", "content": "print('on main')\n"}, chat.ctx)
            self.assertTrue(written.ok, written.error)
            execute(["git", "add", "main.py"])
            execute(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "base"])
            console._cmd_branch(["new", "feature/test"])
            self.assertEqual(console._branch, "feature/test")
            changed = chat.registry.execute("workspace_write", {"path": "main.py", "content": "print('feature')\n"}, chat.ctx)
            self.assertTrue(changed.ok)
            execute(["git", "add", "main.py"])
            execute(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "feature"])
            console._cmd_branch(["main"])
            self.assertEqual(console._branch, "main")
            changed = chat.registry.execute("workspace_edit", {"path": "main.py", "old": "on main", "new": "local changes"}, chat.ctx)
            self.assertTrue(changed.ok)
            conflict = chat.registry.execute("workspace_git", {"action": "switch", "branch": "feature/test"}, chat.ctx)
            self.assertFalse(conflict.ok)
            bad = chat.registry.execute("workspace_git", {"action": "switch", "branch": "missing"}, chat.ctx)
            self.assertFalse(bad.ok)
            self.assertEqual((project / "main.py").read_text(), "print('local changes')\n")

    def test_home_animation_updates_only_welcome_and_stops_on_submit(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: ())
            editor.show_home()
            with patch("trina.console_ui.time.monotonic", return_value=1):
                editor._animate_home()
            first = editor._chunks[0]
            with patch("trina.console_ui.time.monotonic", return_value=2):
                editor._animate_home()
            self.assertNotEqual(editor._chunks[0], first)
            from prompt_toolkit.keys import Keys
            enter = editor.app.key_bindings.get_bindings_for_keys((Keys.ControlM,))[0]
            from types import SimpleNamespace
            enter.handler(SimpleNamespace(app=SimpleNamespace(exit=lambda **_: None)))
            frozen = editor._chunks[0]
            with patch("trina.console_ui.time.monotonic", return_value=3):
                editor._animate_home()
            self.assertEqual(editor._chunks[0], frozen)

    def test_footer_is_single_line_at_all_widths_and_role_never_disappears(self):
        context = ConsoleContext("运行中", "provider/very-long-model-name high", "C:/项目/很长的目录名称", "标题",
                                 "步骤 4/40", 450_000, 500_000, role="Code Worker", branch="feature/long-name")
        usage = ConsoleState()
        usage.model_finished(LLMUsage(input_tokens=125_000, output_tokens=15_000, cache_read_tokens=100_000))
        for width in (1, 8, 20, 40, 88, 144):
            for text in ("".join(t for _, t in context.footer(width)), "".join(t for _, t in context.rail(width)), usage.footer(width)):
                self.assertNotIn("\n", text)
                self.assertLessEqual(_cells(text), width)
            if width >= 20:
                self.assertIn("Code", "".join(t for _, t in context.footer(width)))
        self.assertIn("class:context.hot", {s for s, _ in context.rail(88)})

    def test_ui_config_roundtrip_override_and_validation(self):
        config = TrinaConfig._from_mapping({"ui": {"status_line_preset": "compact", "motion": "none"}})
        config.validate()
        self.assertEqual(TrinaConfig._from_mapping(config.to_dict()).ui.status_line_preset, "compact")
        with self.assertRaises(ConfigError):
            config.with_overrides(ui__status_line_preset="unknown")
