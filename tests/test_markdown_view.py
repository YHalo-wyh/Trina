from __future__ import annotations

import unittest

from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from trina.markdown_view import _highlight_line_fragments, render_markdown_fragments
from trina.terminal import render_answer, render_tool_call_fragments, streaming_answer


class MarkdownViewTests(unittest.TestCase):
    def test_common_blocks_preserve_content_and_strip_terminal_controls(self):
        source = ("## 结果\n\n**完成** `check.py` file_name_here\n"
                  "> 证据\n- 第一项\n1. 第二项\n"
                  "```python\nprint('ok')\n```\n\x1b[2J安全结尾 &#x202e;实体")
        fragments = render_markdown_fragments(source, color=True)
        plain = "".join(text for _, text in fragments)
        for expected in ("结果", "完成", "check.py", "file_name_here", "│ 证据", "• 第一项", "1. 第二项",
                         "print('ok')", "安全结尾"):
            self.assertIn(expected, plain)
        self.assertNotIn("\x1b", plain)
        self.assertNotIn("\u202e", plain, "Markdown 实体解码后仍须移除方向控制字符")
        self.assertTrue(any(style for style, _ in fragments), "标题、行内标记和代码必须保留样式")

    def test_append_only_code_reuses_completed_line_highlights(self):
        _highlight_line_fragments.cache_clear()
        first = "```python\n" + "\n".join(f"value_{index} = {index}" for index in range(20))
        render_markdown_fragments(first, color=True)
        before = _highlight_line_fragments.cache_info()
        render_markdown_fragments(first + "\nvalue_20 = 20", color=True)
        after = _highlight_line_fragments.cache_info()
        self.assertEqual(after.misses - before.misses, 1)
        self.assertGreaterEqual(after.hits - before.hits, 20)

    def test_static_ansi_and_direct_fragments_have_identical_text(self):
        source = "## 标题\n```python\nprint(42)\n```"
        rendered = render_answer(source, width=80, color=True)
        ansi_text = "".join(text for _, text in to_formatted_text(ANSI(rendered)))
        direct_text = "".join(text for _, text in render_markdown_fragments(source, color=True))
        self.assertEqual(ansi_text, direct_text)

    def test_streaming_answer_defers_highlighting_until_display(self):
        _highlight_line_fragments.cache_clear()
        source = streaming_answer("```python\nprint(42)", color=True)
        self.assertEqual(_highlight_line_fragments.cache_info().misses, 0)
        fragments = source.fragments_at_width(80)
        self.assertIn("print(42)", "".join(text for _, text in fragments))
        self.assertEqual(_highlight_line_fragments.cache_info().misses, 1)

    def test_tool_fragments_are_bounded_and_sanitize_output(self):
        from rich.cells import cell_len
        for width in (20, 40, 120):
            fragments = render_tool_call_fragments(
                "terminal_exec", {"argv": ["python", "-V"]}, status="completed",
                output="结果\x1b[2J\n" + "x" * 70, duration=.2, exit_code=12345678, width=width, color=True)
            plain = "".join(text for _, text in fragments)
            self.assertNotIn("\x1b", plain)
            self.assertIn("─── Output ───", plain)
            self.assertNotIn("python", plain)
            self.assertTrue(all(cell_len(line) <= width for line in plain.splitlines()))

    def test_tool_card_separates_input_output_and_failures_without_empty_sections(self):
        def text(*args, **kwargs):
            return "".join(part for _, part in render_tool_call_fragments(*args, **kwargs))

        card = text("terminal_exec", {"argv": ["python", "-V"]}, status="failed", duration=.3,
                    exit_code=1, output="已生成摘要", stderr="命令执行出错", error="命令执行出错", width=72)
        self.assertIn("terminal_exec · 失败", card.splitlines()[0])
        self.assertIn("0.3s · exit 1", card.splitlines()[0])
        self.assertNotIn(" 命令 ", card)
        self.assertNotIn("python", card)
        divider = next(line for line in card.splitlines() if "─── Output ───" in line)
        self.assertTrue(divider.startswith("│") and divider.endswith("│"))
        self.assertLess(card.index("─── Output"), card.index(" stderr "))
        self.assertEqual(card.count("命令执行出错"), 1)

        pending = text("workspace_read", {"path": "会议纪要.md"}, status="running", width=60)
        self.assertIn('{"path": "会议纪要.md"}', pending)
        self.assertNotIn("Output", pending)
        self.assertNotIn("0.0s", pending)
        failure = text("workspace_read", {}, status="denied", error="工作目录不可读", width=60)
        self.assertIn(" 错误 ", failure)
        self.assertIn("工作目录不可读", failure)
        self.assertNotIn(" 参数 ", failure)

    def test_commonmark_nested_markup_links_and_tables(self):
        fragments = render_markdown_fragments(
            "- **bold with *italic***\n  - child\n\n[text][ref]\n\n[ref]: https://example.com\n\n"
            "| A | B |\n| --- | --- |\n| x | y |\n\n\\*literal\\* &amp; file_name_here", color=True)
        plain = "".join(text for _, text in fragments)
        self.assertIn("  • child", plain)
        self.assertIn("text (https://example.com)", plain)
        self.assertIn("│ A │ B │", plain)
        self.assertIn("│ x │ y │", plain)
        self.assertIn("*literal* & file_name_here", plain)
        self.assertTrue(any("bold" in style and "italic" in style and "italic" in text
                            for style, text in fragments))

    def test_multiline_strings_and_fence_delimiters_keep_source_and_style(self):
        fragments = render_markdown_fragments("````python\nx = \"\"\"hello\nworld\"\"\"\n```\n````", color=True)
        plain = "".join(text for _, text in fragments)
        self.assertIn('world"""', plain)
        self.assertIn("```", plain, "短围栏必须保留为源码")
        self.assertTrue(any("#e6db74" in style and "world" in text for style, text in fragments))

    def test_nonfullscreen_wraps_unicode_without_exposing_style_sequences(self):
        from rich.cells import cell_len
        rendered = render_answer("**" + "中文" * 25 + "**\n```text\n" + "x" * 51 + "\n```", width=20, color=True)
        plain = "".join(text for _, text in to_formatted_text(ANSI(rendered)))
        self.assertTrue(all(cell_len(line) <= 20 for line in plain.splitlines()))
        self.assertEqual(plain.replace("\n", "").replace(" ", ""), "中文" * 25 + "x" * 51)


if __name__ == "__main__":
    unittest.main()
