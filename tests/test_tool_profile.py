"""CTF 工具按题型裁剪和执行白名单测试。"""
from __future__ import annotations

import unittest

from trina.tools import build_registry
from trina.tools.profile import CTF_PRUNED_TOOLS, visible_tool_names
from trina.tools.schema import tool_schemas


class ToolProfileTests(unittest.TestCase):
    def test_general_ctf_profile_does_not_expose_pruned_or_binary_tools(self):
        registry = build_registry()
        visible = visible_tool_names(registry, "general")
        self.assertLess(len(visible), len(registry.names()))
        self.assertTrue(CTF_PRUNED_TOOLS.isdisjoint(visible))
        self.assertIn("terminal_exec", visible)
        self.assertIn("workspace_read", visible)
        self.assertNotIn("binary_open", visible)

    def test_binary_and_browser_routes_bind_only_when_needed(self):
        registry = build_registry()
        binary = visible_tool_names(registry, "binary")
        self.assertIn("binary_open", binary)
        self.assertIn("debug_start", binary)
        self.assertNotIn("rce_exec", binary)
        browser = visible_tool_names(registry, "web", browser=True)
        self.assertIn("browser_interact", browser)
        self.assertIn("browser_open", browser)

    def test_file_ctf_route_keeps_only_local_analysis_tools(self):
        registry = build_registry()
        visible = visible_tool_names(registry, "ctf")
        self.assertEqual(
            visible,
            {"terminal_exec", "workspace_read", "workspace_read_many", "workspace_search", "workspace_write", "workspace_edit"},
        )

    def test_schema_export_uses_same_allowlist(self):
        registry = build_registry()
        visible = visible_tool_names(registry, "general")
        schemas = tool_schemas(registry, include=visible)
        names = {item["function"]["name"] for item in schemas}
        self.assertIn("finish", names)
        self.assertIn("terminal_exec", names)
        self.assertNotIn("port_scan", names)
        self.assertNotIn("binary_open", names)

    def test_compact_schema_keeps_protocol_shape_and_reduces_text(self):
        registry = build_registry()
        visible = visible_tool_names(registry, "ctf")
        full = tool_schemas(registry, include=visible)
        compact = tool_schemas(registry, include=visible, compact=True)
        self.assertEqual({item["function"]["name"] for item in full},
                         {item["function"]["name"] for item in compact})
        self.assertLess(len(str(compact)), len(str(full)))
        terminal = next(item for item in compact if item["function"]["name"] == "terminal_exec")
        self.assertIn("argv", terminal["function"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()

