"""控制台退出必须停止生产者，承受重复中断，并继续清理其他资源。"""
from __future__ import annotations

import signal
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trina.interactive import Console
from trina.session import Session
from trina.shutdown import Shutdown


class ConsoleShutdownTests(unittest.TestCase):
    def console(self):
        console = Console.__new__(Console)
        console._cleanup_done = False
        console._emit = Mock()
        console.session = Session()
        console.subworkers = SimpleNamespace(close=Mock(), thread=None)
        console._scheduler = SimpleNamespace(stop_event=threading.Event())
        console._scheduler_thread = None
        console.chat = SimpleNamespace(close=Mock())
        console._plan = SimpleNamespace(started_fixtures=True, teardown=Mock())
        return console

    def test_repeat_interrupt_in_join_still_closes_resources_once(self):
        console = self.console()
        def interrupted_join(**_kwargs):
            self.assertTrue(console.session.stop_event.is_set())
            self.assertTrue(console._scheduler.stop_event.is_set())
            console.subworkers.close.assert_called_once_with(timeout=0)
            raise KeyboardInterrupt
        console.session.thread = Mock()
        console.session.thread.join.side_effect = interrupted_join
        console._scheduler_thread = Mock()
        previous = signal.getsignal(signal.SIGINT)
        console._cleanup()
        console._cleanup()
        console.chat.close.assert_called_once()
        console._plan.teardown.assert_called_once()
        console._scheduler_thread.join.assert_not_called()
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_real_sigint_during_join_skips_remaining_waits(self):
        console = self.console()
        console.session.thread = Mock()
        console.session.thread.join.side_effect = lambda **_kwargs: signal.raise_signal(signal.SIGINT)
        previous = signal.getsignal(signal.SIGINT)
        console._cleanup()
        self.assertEqual(console.session.thread.join.call_count, 1)
        console.chat.close.assert_called_once()
        console._plan.teardown.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_cleanup_error_does_not_prevent_fixture_teardown(self):
        console = self.console()
        console.chat.close.side_effect = RuntimeError("save failed")
        console._cleanup()
        console._plan.teardown.assert_called_once()
        self.assertIn("save failed", console._emit.call_args.args[0])

    @unittest.skipIf(os.name == "nt", "POSIX SIGINT 子进程回归；Windows 另有 raise_signal 回归")
    def test_repeated_os_sigint_during_real_thread_join_exits_without_traceback(self):
        script = textwrap.dedent('''
            import os, signal, threading, time
            from types import SimpleNamespace
            from trina.interactive import Console
            from trina.session import Session
            console = Console.__new__(Console)
            console._cleanup_done = False
            console._emit = lambda text: None
            console.session = Session()
            console.session.thread = threading.Thread(target=lambda: time.sleep(3), daemon=True)
            console.session.thread.start()
            console.subworkers = SimpleNamespace(close=lambda **kw: None, thread=None)
            console._scheduler = console._scheduler_thread = None
            console.chat = SimpleNamespace(close=lambda: (time.sleep(.15), print('chat closed')))
            console._plan = SimpleNamespace(started_fixtures=True, teardown=lambda: print('fixture closed'))
            def interrupt():
                time.sleep(.05)
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(.01)
                os.kill(os.getpid(), signal.SIGINT)
            sender = threading.Thread(target=interrupt)
            sender.start()
            console._cleanup()
            sender.join()
            assert console.session.thread.is_alive()
            assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
            print('clean exit')
        ''')
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["chat closed", "fixture closed", "clean exit"])
        self.assertNotIn("Traceback", result.stderr)

    def test_plain_input_and_handler_interrupt_both_cleanup(self):
        for source in ("input", "handle"):
            with self.subTest(source=source):
                console = self.console()
                console._input = Mock()
                console._custom_output = True
                console.exit_requested = False
                console.color = False
                console._display = Mock()
                console._read_input = Mock(return_value="hello")
                console.handle = Mock(return_value=True)
                getattr(console, "_read_input" if source == "input" else "handle").side_effect = KeyboardInterrupt
                self.assertEqual(console.run(), 0)
                console.chat.close.assert_called_once()
                self.assertTrue(console.session.stop_event.is_set())

    def test_enhanced_output_restored_even_if_cleanup_fails(self):
        console = self.console()
        console._input = None
        console._custom_output = False
        console._plain = True
        console.color = False
        console.config = SimpleNamespace(ui=SimpleNamespace(motion="none"))
        console._output_palette = Mock()
        original = console.output = Mock()
        console._start_scheduler = Mock()
        console._cleanup = Mock(side_effect=RuntimeError("cleanup failed"))
        with patch("trina.interactive.enhanced_input_available", return_value=True), \
             patch("trina.interactive.EnhancedConsoleInput") as editor:
            editor.return_value.run.side_effect = KeyboardInterrupt
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                console.run()
        self.assertIs(console.output, original)

    def test_shutdown_wait_has_deadline_and_does_not_join_itself(self):
        release = threading.Event()
        thread = threading.Thread(target=release.wait)
        thread.start()
        try:
            started = time.monotonic()
            with Shutdown(self.fail, timeout=.05) as shutdown:
                shutdown.join(thread)
                shutdown.join(threading.current_thread())
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(1)

    def test_stop_releases_paused_approval_and_denies_late_request(self):
        entered = threading.Event()
        session = Session(approval_mode="all", emit=lambda _text: entered.set())
        session.pause_event.set()
        results = []
        thread = threading.Thread(target=lambda: results.append(session.request_approval({"tool": "test", "level": "exploit"})))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            session.request_stop()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [False])
            self.assertFalse(session.pause_event.is_set())
            self.assertFalse(session.request_approval({"tool": "late", "level": "exploit"}))
            self.assertIsNone(session.pending)
        finally:
            session.request_stop()
            thread.join(1)


if __name__ == "__main__":
    unittest.main()
