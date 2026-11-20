from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from trina.watchdog import parse_summary, run_watchdog, summary_is_terminal


class WatchdogTests(unittest.TestCase):
    def test_summary_parser_and_terminal_detection(self):
        payload = {"tasks": {"a": {"status": "completed"}}}
        self.assertEqual(parse_summary("prefix\n" + json.dumps(payload)), payload)
        self.assertTrue(summary_is_terminal(payload))
        self.assertFalse(summary_is_terminal({"tasks": {"a": {"status": "running"}}}))

    def test_restarts_crashed_child_but_stops_on_normal_summary(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            checkpoint = directory / "swarm.json"
            checkpoint.write_text("{}", encoding="utf-8")
            marker = directory / "attempt"
            script = directory / "child.py"
            script.write_text(
                "import json, pathlib, sys\n"
                "p=pathlib.Path(sys.argv[1]); n=int(p.read_text() or '0') if p.exists() else 0\n"
                "p.write_text(str(n+1))\n"
                "if n == 0: raise SystemExit(7)\n"
                "print(json.dumps({'tasks': {'a': {'status': 'completed'}}}))\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            code = run_watchdog(
                checkpoint, max_restarts=2, interval=0,
                command_factory=lambda _attempt: [sys.executable, str(script), str(marker)],
                stream=output,
            )
            self.assertEqual(code, 0)
            self.assertIn("异常退出", output.getvalue())
            self.assertEqual(marker.read_text(encoding="utf-8"), "2")


if __name__ == "__main__":
    unittest.main()
