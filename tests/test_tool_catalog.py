"""工具控制中心的目录解析与筛选测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from trina.state import RunState
from trina.tool_catalog import build_tool_catalog, filter_tool_catalog
from trina.tools import build_registry
from trina.tools.profile import visible_tool_names


class ToolCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_registry()

    def test_catalog_explains_route_visibility_and_public_names(self):
        visible = visible_tool_names(self.registry, "general")
        entries = build_tool_catalog(
            self.registry, visible=visible, route="general", role="commander",
            check_availability=False,
        )
        by_name = {item.internal_name: item for item in entries}
        self.assertEqual(by_name["terminal_exec"].name, "shell")
        self.assertTrue(by_name["terminal_exec"].visible)
        self.assertIn("模型 Schema 已注入", by_name["terminal_exec"].resolution)
        self.assertFalse(by_name["binary_open"].visible)
        self.assertIn("Binary/Pwn/Reverse", by_name["binary_open"].visibility_reason)

    def test_catalog_keeps_session_stats_without_becoming_state_source(self):
        state = RunState("catalog", "")
        state.steps.extend([
            {"kind": "tool", "tool": "terminal_exec", "ok": True, "duration": .1},
            {"kind": "tool", "tool": "terminal_exec", "ok": False, "duration": .9},
        ])
        entries = build_tool_catalog(
            self.registry, visible={"terminal_exec"}, state=state,
            check_availability=False,
        )
        shell = next(item for item in entries if item.internal_name == "terminal_exec")
        self.assertEqual(shell.stats.calls, 2)
        self.assertEqual(shell.stats.failed, 1)
        self.assertEqual(shell.stats.p50_seconds, .1)
        self.assertEqual(shell.stats.p95_seconds, .9)

    def test_search_covers_descriptions_parameters_and_visibility(self):
        entries = build_tool_catalog(
            self.registry,
            visible=visible_tool_names(self.registry, "code"),
            route="code",
            check_availability=False,
        )
        visible = filter_tool_catalog(entries, query="argv", group="visible")
        self.assertIn("shell", {item.name for item in visible})
        hidden = filter_tool_catalog(entries, query="binary", group="hidden")
        self.assertTrue(hidden)
        self.assertTrue(all(not item.visible for item in hidden))

    def test_console_projection_hides_unconnected_worker_dispatch(self):
        from trina.config import TrinaConfig
        from trina.interactive import Console

        console = Console(TrinaConfig.load(), registry=self.registry, output=lambda _text: None,
                          autostart_lab=False)
        by_name = {item.internal_name: item for item in console._tool_catalog()}
        self.assertTrue(by_name["terminal_exec"].visible)
        self.assertFalse(by_name["worker_tasks"].visible)
        self.assertIn("Subworker", by_name["worker_tasks"].visibility_reason)

    def test_shared_availability_probe_runs_once_per_implementation(self):
        with patch("trina.tools.browser._available", return_value=(True, "")) as probe:
            entries = build_tool_catalog(self.registry, check_availability=True)
        browser_count = sum(item.source == "tools.browser" for item in entries)
        self.assertGreater(browser_count, 1)
        self.assertEqual(probe.call_count, 1)

if __name__ == "__main__":
    unittest.main()
