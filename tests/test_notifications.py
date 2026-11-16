"""完成提醒应可配置、可测试，并安全生成终端通知序列。"""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from trina.config import ConfigError, TrinaConfig
from trina.notifications import notification_sequence, send_terminal_notification


class NotificationTests(unittest.TestCase):
    def test_known_terminals_use_notification_protocol_and_unknown_uses_bell(self) -> None:
        self.assertTrue(notification_sequence("Trina", "完成", env={"TERM": "xterm-kitty"}).startswith("\x1b]99;;"))
        self.assertTrue(notification_sequence("Trina", "完成", env={"TERM_PROGRAM": "WezTerm"}).startswith("\x1b]9;"))
        self.assertEqual(notification_sequence("Trina", "完成", env={}), "\a")

    def test_payload_removes_control_sequence_injection(self) -> None:
        sequence = notification_sequence("Trina\x1b]0;bad", "done\nnext", env={"TERM": "xterm-kitty"})
        self.assertEqual(sequence.count("\x1b"), 2)
        self.assertNotIn("\n", sequence)
        self.assertIn("done next", sequence)

    def test_redirected_output_does_not_receive_terminal_control_codes(self) -> None:
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            self.assertFalse(send_terminal_notification("Trina", "完成"))
        self.assertEqual(stream.getvalue(), "")

    def test_notification_configuration_round_trip_and_validation(self) -> None:
        config = TrinaConfig().with_overrides(ui__completion_notify=False, ui__error_notify=True)
        self.assertFalse(config.ui.completion_notify)
        self.assertTrue(config.ui.error_notify)
        with self.assertRaises(ConfigError):
            TrinaConfig().with_overrides(ui__completion_notify="yes")

    def test_enhanced_console_shows_and_clears_in_app_notification(self) -> None:
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False,
                                          dynamic_values=lambda *_: (), plain=True)
            editor.show_notification("Trina", "任务完成", seconds=.5)
            self.assertEqual(editor._toast_message, "Trina · 任务完成")
            self.assertIn("任务完成", "".join(text for _, text in editor._toast()))
            editor._clear_notification(editor._toast_token)
            self.assertEqual(editor._toast_message, "")


if __name__ == "__main__":
    unittest.main()
