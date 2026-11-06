"""字符首页的布局、无色输出和终端兼容性。"""
from __future__ import annotations

import builtins
import re
import unittest
from unittest.mock import patch

from trina.terminal import render_home, show_home


class BannerTests(unittest.TestCase):
    def test_plain_art_is_filled_text_and_fits_available_columns(self):
        for width in (20, 35, 36, 40, 50, 51, 64, 88, 120):
            with self.subTest(width=width):
                text = render_home(width=width)
                self.assertTrue(all(character == "\n" or character.isprintable()
                                    for character in text))
                if width >= 40:
                    characters = set("".join(text.splitlines()[:-1]))
                    self.assertLessEqual(characters, set(" █▓▀▄·✦"))
                    self.assertTrue({"█", "▓"} <= characters)
                self.assertLessEqual(max(map(len, text.splitlines())), width)
                self.assertEqual(text.splitlines()[-1].strip(), "v0.2.0")
        self.assertGreater(len(render_home(width=88).splitlines()),
                           len(render_home(width=40).splitlines()))

    def test_color_changes_only_foreground_and_preserves_all_characters(self):
        foreground_sgr = re.compile(r"\x1b\[(?:0|38;2;\d+;\d+;\d+)m")
        for width in (20, 35, 36, 40, 50, 51, 64, 88, 120):
            with self.subTest(width=width):
                colored = render_home(width=width, color=True)
                self.assertIn("\x1b[38;2;", colored)
                self.assertEqual(foreground_sgr.sub("", colored), render_home(width=width))

    def test_terminal_graphics_settings_do_not_change_character_output(self):
        for mode in ("auto", "sixel", "kitty", "iterm"):
            with self.subTest(mode=mode), patch.dict("os.environ", {
                "TRINA_LOGO_MODE": mode, "WT_SESSION": "test",
                "KITTY_WINDOW_ID": "1", "TERM_PROGRAM": "iTerm.app",
            }), patch("trina.terminal.terminal_width", return_value=88):
                output = []
                show_home(output=output.append, color=True)
                self.assertEqual(output, [render_home(width=88, color=True)])

    def test_home_needs_no_image_or_rich_dependency(self):
        original = builtins.__import__

        def without_display_dependencies(name, *args, **kwargs):
            if name.split(".")[0] in {"PIL", "rich"}:
                raise ImportError("simulated missing display dependency")
            return original(name, *args, **kwargs)

        expected = render_home(width=88, color=True)
        with patch("builtins.__import__", side_effect=without_display_dependencies):
            self.assertEqual(render_home(width=88, color=True), expected)

    def test_narrow_terminal_keeps_name_and_version_readable(self):
        self.assertEqual([line.strip() for line in render_home(width=20).splitlines()],
                         ["TRINA", "v0.2.0"])


if __name__ == "__main__":
    unittest.main()
