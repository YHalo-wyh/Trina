"""语义颜色、动态切换与纯文本降级不改变真实输出内容。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from trina.config import ConfigError, TrinaConfig
from trina.console_ui import EnhancedConsoleInput
from trina.interactive import Console
from trina.markdown_view import render_markdown_fragments
from trina.output_styles import output_style
from trina.terminal import render_answer, render_thinking, render_tool_call, render_tool_call_fragments


class OutputColorTests(unittest.TestCase):
    def test_tool_state_colors_and_diff_have_distinct_semantics(self):
        styles = []
        for state, role in (("running", "running"), ("completed", "success"), ("failed", "error"), ("timed_out", "warning")):
            fragments = render_tool_call_fragments("terminal_exec", {"argv": ["python", "-V"]}, status=state, color=True)
            self.assertEqual(fragments[0][0], output_style(role))
            styles.append(fragments[0][0])
        self.assertEqual(len(set(styles)), 4)
        diff = "@@ -1 +1 @@\n-old\n+new\n unchanged"
        fragments = render_tool_call_fragments("workspace_git", {"action": "diff"}, output=diff, color=True)
        for role, text in (("diff_header", "@@"), ("diff_removed", "-old"), ("diff_added", "+new")):
            self.assertTrue(any(style == output_style(role) and text in value for style, value in fragments))

    def test_markdown_patch_colors_and_palette_change_keep_text(self):
        source = "## 结果\n正文 `code`\n\n```diff\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n```"
        variants = [render_markdown_fragments(source, color=True, palette=name) for name in ("trina", "contrast", "mono")]
        self.assertEqual(len({"".join(text for _, text in fragments) for fragments in variants}), 1)
        self.assertNotEqual([style for style, _ in variants[0]], [style for style, _ in variants[1]])
        self.assertTrue(all(not style for style, _ in variants[2]))
        self.assertIn(output_style("diff_added"), {style for style, _ in variants[0]})

    def test_no_color_and_mono_never_emit_ansi(self):
        for name, color in (("trina", False), ("mono", True)):
            for value in (render_answer("**正文**\n```python\nx=1\n```", color=color, palette=name),
                          render_thinking("检查\x1b[2J结果", color=color, palette=name),
                          render_tool_call("tool", {}, output="text\x1b[2J", color=color, palette=name)):
                self.assertNotIn("\x1b", value)

    def test_live_palette_recolors_cached_history_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            config = TrinaConfig().with_overrides(run__workspace=str(Path(directory) / "runs"), audit__dir=str(Path(directory) / "logs"))
            console = Console(config, output=lambda _: None, autostart_lab=False)
            self.addCleanup(console._cleanup)
            console.color = True
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False, dynamic_values=lambda *_: (),
                                          motion="none", output_palette=console._output_palette)
            console._enhanced_input, console.output = editor, editor.write
            editor.write(render_answer("正文", color=True, palette=console._output_palette))
            editor.transcript_control.sync(80)
            self.assertIn(output_style("text", "trina"), {style for style, _ in editor.transcript_control.rows[0]})
            console._cmd_theme(["contrast"])
            editor.transcript_control.sync(80)
            self.assertIn(output_style("text", "contrast"), {style for style, _ in editor.transcript_control.rows[0]})
            console._cmd_theme(["mono"])
            editor.transcript_control.sync(80)
            self.assertTrue(all(not style for row in editor.transcript_control.rows for style, _ in row))
            self.assertIsNone(console.chat)

    def test_palette_configuration_roundtrip_and_invalid_value(self):
        config = TrinaConfig._from_mapping({"ui": {"output_palette": "contrast"}})
        config.validate()
        self.assertEqual(TrinaConfig._from_mapping(config.to_dict()).ui.output_palette, "contrast")
        with self.assertRaises(ConfigError):
            config.with_overrides(ui__output_palette="invalid")
