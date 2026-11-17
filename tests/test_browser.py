"""浏览器工具的注册、会话、证据、脱敏和执行边界测试。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.config import ConfigError, TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.external import ExternalRun, run_external
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext


class BrowserToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = TrinaConfig.load()
        self.config.scope.trust_mode = "full"
        self.config.browser.command_timeout = 2
        self.audit = AuditLog.open(self.root / "logs", run_id="browser-test", resume=False)
        self.guard = ScopeGuard(self.config.scope, audit=self.audit)
        self.state = RunState(run_id="browser-test", target="https://example.test")
        self.ctx = ToolContext(
            config=self.config,
            guard=self.guard,
            audit=self.audit,
            state=self.state,
            http=HttpClient(self.guard),
            workspace=self.root / "run",
            deadline=time.monotonic() + 30,
        )
        self.registry = build_registry(["browser"])
        self.calls: list[tuple[list[str], dict]] = []
        self.launcher_patcher = patch("trina.browser.sessions.playwright_launcher", return_value=["playwright-cli"])
        self.external_patcher = patch("trina.browser.sessions.run_external", side_effect=self.fake_external)
        self.launcher_patcher.start()
        self.external_patcher.start()
        self.addCleanup(self.launcher_patcher.stop)
        self.addCleanup(self.external_patcher.stop)
        self.addCleanup(self.ctx.extra["browser_sessions"].close)

    def fake_external(self, argv, _ctx, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        command = argv[argv.index("--json") + 1]
        payload: dict = {"result": command}
        if command == "open":
            payload = {"session": "fixture", "pid": 123, "result": {"snapshot": {"file": "page.yml"}}}
        elif command == "snapshot":
            payload = {
                "snapshot": [
                    {"role": "generic", "ref": "e1", "children": [
                        {"role": "textbox", "name": "账号", "ref": "e2"},
                        {"role": "button", "name": "登录", "ref": "e3"},
                    ]}
                ]
            }
        elif command == "screenshot":
            filename = argv[argv.index("--filename") + 1]
            (Path(kwargs["cwd"]) / filename).write_bytes(b"fixture-png")
        return ExternalRun(ok=True, stdout=json.dumps(payload, ensure_ascii=False), code=0, command=" ".join(argv))

    def execute(self, name: str, params: dict):
        return self.registry.execute(name, params, self.ctx)

    def test_registry_exposes_structured_browser_tools(self):
        self.assertEqual(
            self.registry.names(),
            [
                "browser_close", "browser_interact", "browser_open", "browser_requests",
                "browser_screenshot", "browser_snapshot", "browser_storage",
            ],
        )
        self.assertEqual(self.registry.get("browser_interact").required_level({"action": "click"}), "verify")
        self.assertEqual(self.registry.get("browser_interact").required_level({"action": "goto"}), "detect")
        self.assertEqual(self.registry.get("browser_storage").required_level({"action": "set"}), "verify")

    def test_open_snapshot_screenshot_and_close_keep_evidence(self):
        opened = self.execute("browser_open", {"url": "https://example.test"})
        self.assertTrue(opened.ok, opened.error)
        sid = opened.data["session_id"]
        self.assertTrue(sid.startswith("web-"))
        self.assertTrue(self.calls[-1][1]["allow_detached_children"])

        snapshot = self.execute("browser_snapshot", {"session_id": sid})
        self.assertTrue(snapshot.ok, snapshot.error)
        self.assertIn("ref=e2", snapshot.data["snapshot"])
        self.assertTrue(Path(snapshot.data["evidence_ref"]).is_file())

        screenshot = self.execute("browser_screenshot", {"session_id": sid, "label": "login"})
        self.assertTrue(screenshot.ok, screenshot.error)
        self.assertTrue(Path(screenshot.data["path"]).is_file())
        self.assertIn(screenshot.data["path"], self.state.artifacts["browser_screenshots"])

        closed = self.execute("browser_close", {"session_id": sid})
        self.assertTrue(closed.ok, closed.error)
        self.assertFalse(self.ctx.extra["browser_sessions"].snapshot()[-1]["live"])
        self.assertTrue(self.audit.verify()[0])

    def test_session_limit_and_cross_worker_handle_rejected(self):
        self.config.browser.max_sessions = 1
        first = self.execute("browser_open", {"url": "https://example.test/one"})
        second = self.execute("browser_open", {"url": "https://example.test/two"})
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertIn("上限", second.error)

        other_state = RunState(run_id="other", target="https://example.test")
        other = ToolContext(
            config=self.config,
            guard=self.guard,
            audit=self.audit,
            state=other_state,
            http=HttpClient(self.guard),
            workspace=self.root / "other",
            deadline=time.monotonic() + 30,
        )
        self.addCleanup(other.extra["browser_sessions"].close)
        result = self.registry.execute("browser_snapshot", {"session_id": first.data["session_id"]}, other)
        self.assertFalse(result.ok)
        self.assertIn("失效", result.error)

    def test_storage_values_are_redacted_in_events_and_audit(self):
        opened = self.execute("browser_open", {"url": "https://example.test"})
        secret = "cookie-secret-value"
        result = self.execute("browser_storage", {
            "session_id": opened.data["session_id"], "area": "cookies", "action": "set",
            "key": "session", "storage_value": secret,
        })
        self.assertTrue(result.ok, result.error)
        records = list(self.audit.iter_records(self.audit.path))
        starts = [row for row in records if row.get("event") == "tool.start" and row.get("tool") == "browser_storage"]
        self.assertTrue(starts)
        self.assertNotIn(secret, json.dumps(records, ensure_ascii=False))
        self.assertIn("<redacted:", starts[-1]["params"]["storage_value"])

    def test_restricted_detect_mode_blocks_click_before_cli(self):
        opened = self.execute("browser_open", {"url": "https://example.test"})
        self.config.scope.trust_mode = "restricted"
        self.config.scope.exploit_level = "detect"
        before = len(self.calls)
        result = self.execute("browser_interact", {
            "session_id": opened.data["session_id"], "action": "click", "ref": "e3",
        })
        self.assertTrue(result.denied)
        self.assertEqual(len(self.calls), before)
        snapshot = self.execute("browser_snapshot", {"session_id": opened.data["session_id"]})
        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.data["error_code"], "RESTRICTED_UNSUPPORTED")
        self.assertEqual(len(self.calls), before)

    def test_browser_config_validation(self):
        self.config.browser.max_sessions = 0
        with self.assertRaises(ConfigError):
            self.config.validate()

    def test_run_external_forwards_managed_detach_to_scope(self):
        self.ctx.extra["_tool_call_id"] = "detach-test"
        completed = subprocess.CompletedProcess(["fixture"], 0, "ok", "")
        with patch("trina.tools.external.which", return_value="fixture"), \
             patch("trina.scope.execute_streamed", return_value=completed) as execute:
            result = run_external(
                ["fixture"], self.ctx, tool="browser:open", target="https://example.test",
                allow_detached_children=True, stream_output=False,
            )
        self.assertTrue(result.ok)
        self.assertTrue(execute.call_args.kwargs["allow_detached_children"])


if __name__ == "__main__":
    unittest.main()
