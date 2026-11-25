"""验证 Windows 本地编码回退和管道分片解码。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from trina.process_output import ProcessOutputDecoder


class ProcessOutputTests(unittest.TestCase):
    @patch("trina.process_output._windows_legacy_encoding", return_value="cp936")
    def test_windows_recovers_the_pending_byte_on_fallback(self, _encoding):
        decoder = ProcessOutputDecoder()
        text = "请按任意键继续. . ."
        payload = text.encode("cp936")
        self.assertEqual(decoder.decode(b"ready\n"), "ready\n")
        self.assertEqual(decoder.decode(payload[:1]), "")
        self.assertEqual(decoder.decode(payload[1:3]), "请")
        self.assertEqual(decoder.decode(payload[3:], final=True), text[1:])

    @patch("trina.process_output._windows_legacy_encoding", return_value="cp936")
    def test_split_utf8_is_preserved(self, _encoding):
        decoder = ProcessOutputDecoder()
        text = "编译完成 ✓"
        self.assertEqual("".join(decoder.decode(bytes([byte])) for byte in text.encode())
                         + decoder.decode(b"", final=True), text)

    @patch("trina.process_output._windows_legacy_encoding", return_value=None)
    def test_invalid_bytes_remain_visible_as_replacement_outside_windows(self, _encoding):
        decoder = ProcessOutputDecoder()
        self.assertEqual(decoder.decode(b"a\xffb", final=True), "a\ufffdb")
