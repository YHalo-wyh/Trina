"""用户字标的随包资源、显示协议、尺寸和纯文本降级。"""
from __future__ import annotations

import base64
import builtins
import io
import re
import unittest
from unittest.mock import patch

from PIL import Image
from rich.cells import cell_len
from rich.text import Text

from trina.banner import layout, logo_bytes, native_protocol, render_blocks, render_native
from trina.terminal import render_home, show_home


class BannerTests(unittest.TestCase):
    def test_packaged_art_is_a_wide_png(self):
        data = logo_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(data)) as image:
            self.assertGreater(image.width, 800)
            self.assertGreater(image.width / image.height, 2)

    def test_color_adaptation_fits_narrow_wide_and_short_terminals(self):
        for columns in (20, 40, 88, 120):
            for rows in (12, 24):
                rendered = Text.from_ansi(render_blocks(columns, rows)).plain.splitlines()
                self.assertLessEqual(len(rendered), rows - 6)
                self.assertLessEqual(max(cell_len(line) for line in rendered), columns)

    def test_native_detection_and_explicit_portable_mode(self):
        with patch.dict("os.environ", {"WT_SESSION": "test", "TRINA_LOGO_MODE": "auto", "KITTY_WINDOW_ID": "", "TERM_PROGRAM": "", "TERM": "xterm", "WEZTERM_PANE": ""}):
            self.assertEqual(native_protocol(), "sixel")
            with patch.dict("os.environ", {"TRINA_LOGO_MODE": "ansi"}):
                self.assertIsNone(native_protocol())

    def test_iterm_payload_contains_original_png_unchanged(self):
        rendered = render_native(88, "iterm")
        encoded = rendered.split("preserveAspectRatio=1:", 1)[1].split("\x07", 1)[0]
        self.assertEqual(base64.b64decode(encoded), logo_bytes())

    def test_kitty_chunks_preserve_original_art(self):
        rendered = render_native(88, "kitty")
        packets = re.findall(r"\x1b_G([^;]+);([^\x1b]+)\x1b\\", rendered)
        self.assertTrue(packets)
        self.assertTrue(packets[-1][0].endswith("m=0"))
        self.assertTrue(all(len(data) <= 4096 for _, data in packets))
        self.assertEqual(base64.b64decode("".join(data for _, data in packets)), logo_bytes())

    def test_sixel_raster_keeps_aspect_and_is_bounded(self):
        rendered = render_native(88, "sixel")
        raster = re.search(r'\x1bP0;1;0q"1;1;(\d+);(\d+)', rendered)
        self.assertIsNotNone(raster)
        width, height = map(int, raster.groups())
        with Image.open(io.BytesIO(logo_bytes())) as image:
            self.assertAlmostEqual(width / height, image.width / image.height, delta=.02)
        self.assertLess(len(rendered), 500000)
        self.assertLessEqual(height, layout(88)[1] * 16)

    def test_plain_mode_never_emits_native_control_sequences(self):
        output = []
        with patch.dict("os.environ", {"TRINA_LOGO_MODE": "sixel", "WT_SESSION": "test"}):
            show_home(output=output.append, color=False)
        self.assertEqual([line.strip() for line in output[0].splitlines()], ["TRINA", "0.2.0"])
        self.assertNotIn("\x1b", output[0])

    def test_missing_image_dependency_keeps_plain_version(self):
        original = builtins.__import__
        def missing_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("test missing Pillow")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=missing_pillow):
            text = render_home(width=39, color=True)
        self.assertEqual([line.strip() for line in text.splitlines()], ["TRINA", "0.2.0"])


if __name__ == "__main__":
    unittest.main()
