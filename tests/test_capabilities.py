"""Capability Fabric 的发现、租约、复用与 MCP 接入验收。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import yaml

from trina.audit import AuditLog
from trina.capabilities import (
    CapabilityCatalog,
    CapabilityFabric,
    CapabilityLeaseError,
    CapabilityRuntime,
)
from trina.config import TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec


class _Echo(Tool):
    spec = ToolSpec(
        name="fixture_echo",
        summary="回显测试值",
        category="utility",
        action_level="detect",
        params={"value": ParamSpec("str", "测试值", required=True)},
        produces=["files"],
    )

    def run(self, ctx, **params):
        return ToolResult(
            ok=True,
            summary=f"echo {params['value']}",
            data={"value": params["value"]},
            evidence=[{"kind": "fixture", "note": params["value"]}],
        )


class _Upper(Tool):
    spec = ToolSpec(
        name="fixture_upper",
        summary="转成大写",
        category="utility",
        action_level="detect",
        params={"value": ParamSpec("str", "测试值", required=True)},
    )

    def run(self, ctx, **params):
        value = params["value"].upper()
        return ToolResult(ok=True, summary=value, data={"value": value})


class CapabilityFabricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = TrinaConfig.load().with_overrides(
            scope__trust_mode="full",
            run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"),
            report__output_dir=str(self.root / "reports"),
        )
        self.audit = AuditLog.open(self.root / "logs", run_id="capability", resume=False)
        self.guard = ScopeGuard(self.config.scope, audit=self.audit)
        self.state = RunState(run_id="capability", target="fixture")
        self.context = ToolContext(
            config=self.config,
            guard=self.guard,
            audit=self.audit,
            state=self.state,
            http=None,
            workspace=self.root / "workspace",
            deadline=time.monotonic() + 30,
        )

    @staticmethod
    def _fabric(*tools: Tool) -> tuple[CapabilityCatalog, CapabilityFabric]:
        registry = ToolRegistry(tools)
        catalog = CapabilityCatalog.from_registry(registry)
        return catalog, CapabilityFabric(catalog, CapabilityRuntime(registry))

    def test_manifest_is_discovered_without_core_change_and_guidance_is_jit(self) -> None:
        capability_dir = self.root / "capabilities"
        capability_dir.mkdir()
        guidance = capability_dir / "SKILL.md"
        guidance.write_text("旧内容", encoding="utf-8")
        manifest = {
            "id": "fixture.cli.echo",
            "version": "1.0.0",
            "description": "外部回显能力",
            "provides": ["fixture.echo"],
            "interface": {"name": "fixture_cli_echo", "params": {}},
            "guidance": "SKILL.md",
            "runtime": {"type": "cli", "command": [sys.executable, "-c", "print('ok')"]},
            "policy": {"action_level": "detect", "filesystem": "workspace"},
            "requires": {},
        }
        (capability_dir / "echo.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")

        catalog = CapabilityCatalog()
        self.assertEqual(catalog.discover([capability_dir]), 1)
        capability = catalog.get("fixture.cli.echo")
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability.provides, ("fixture.echo",))

        # Catalog 建立后再改 Guidance；租约建立时才应读取新内容。
        guidance.write_text("租约时读取的新内容", encoding="utf-8")
        fabric = CapabilityFabric(catalog, CapabilityRuntime(ToolRegistry()))
        lease = fabric.lease(
            "worker-1", "task-1", "echo",
            constraints={"selectors": ["fixture.cli.echo"]},
        )
        self.assertIn("租约时读取的新内容", lease.guidance)
        self.assertEqual(lease.snapshot[0]["version"], "1.0.0")
        fabric.release(lease.id)

    def test_worker_switches_capability_and_released_lease_cannot_be_reused(self) -> None:
        _catalog, fabric = self._fabric(_Echo(), _Upper())
        first = fabric.lease(
            "worker-1", "task-echo", "echo",
            constraints={"selectors": ["tool.fixture_echo"]},
        )
        result = fabric.invoke(
            "tool.fixture_echo", {"value": "trina"},
            {"lease_id": first.id, "ctx": self.context},
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["value"], "trina")
        fabric.release(first.id)
        with self.assertRaisesRegex(CapabilityLeaseError, "已释放"):
            fabric.invoke(
                "tool.fixture_echo", {"value": "stale"},
                {"lease_id": first.id, "ctx": self.context},
            )

        second = fabric.lease(
            "worker-1", "task-upper", "upper",
            constraints={"selectors": ["tool.fixture_upper"]},
        )
        result = fabric.invoke(
            "tool.fixture_upper", {"value": "trina"},
            {"lease_id": second.id, "ctx": self.context},
        )
        self.assertEqual(result.data["value"], "TRINA")
        self.assertNotEqual(first.id, second.id)
        fabric.release(second.id)

    def test_two_workers_reuse_definition_and_publish_independent_evidence_refs(self) -> None:
        _catalog, fabric = self._fabric(_Echo())
        first = fabric.lease("worker-a", "task-a", "echo", constraints={"selectors": ["tool.fixture_echo"]})
        second = fabric.lease("worker-b", "task-b", "echo", constraints={"selectors": ["tool.fixture_echo"]})
        self.assertEqual(first.snapshot, second.snapshot)

        for lease, value in ((first, "A"), (second, "B")):
            result = fabric.invoke(
                "tool.fixture_echo", {"value": value},
                {"lease_id": lease.id, "ctx": self.context},
            )
            self.assertTrue(result.ok, result.error)

        refs_a = fabric.refs_for_task("task-a")
        refs_b = fabric.refs_for_task("task-b")
        self.assertEqual(len(refs_a), 1)
        self.assertEqual(len(refs_b), 1)
        self.assertNotEqual(refs_a, refs_b)
        entries = {item["id"]: item for item in fabric.memory_snapshot()}
        self.assertEqual(entries[refs_a[0]]["worker"], "worker-a")
        self.assertEqual(entries[refs_b[0]]["worker"], "worker-b")
        self.assertNotIn("conversation", entries[refs_a[0]])
        fabric.release(first.id)
        fabric.release(second.id)

    def test_mcp_stdio_manifest_invokes_real_server_through_registry(self) -> None:
        capability_dir = self.root / "mcp"
        capability_dir.mkdir()
        server = capability_dir / "server.py"
        server.write_text(textwrap.dedent("""
            import json
            import os
            import sys

            for line in sys.stdin:
                message = json.loads(line)
                if message.get("method") == "initialize":
                    response = {
                        "jsonrpc": "2.0", "id": message["id"],
                        "result": {
                            "protocolVersion": message["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fixture", "version": "1.0.0"},
                        },
                    }
                    print(json.dumps(response), flush=True)
                elif message.get("method") == "tools/call":
                    value = message["params"]["arguments"]["value"]
                    response = {
                        "jsonrpc": "2.0", "id": message["id"],
                        "result": {
                            "content": [{"type": "text", "text": "echo " + value}],
                            "structuredContent": {
                                "summary": "echo " + value,
                                "value": value,
                                "inherited_secret": os.environ.get("TRINA_CAPABILITY_TEST_SECRET", ""),
                            },
                            "isError": False,
                        },
                    }
                    print(json.dumps(response), flush=True)
        """).lstrip(), encoding="utf-8")
        manifest = {
            "id": "fixture.mcp.echo",
            "version": "1.0.0",
            "description": "MCP 回显能力",
            "provides": ["fixture.echo"],
            "interface": {
                "name": "fixture_mcp_echo",
                "params": {"value": {"type": "str", "required": True}},
            },
            "guidance": "",
            "runtime": {
                "type": "mcp",
                "command": [sys.executable, "server.py"],
                "tool": "echo",
                "protocol": "2025-11-25",
            },
            "policy": {"action_level": "detect", "filesystem": "workspace"},
            "requires": {},
        }
        (capability_dir / "echo.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
        catalog = CapabilityCatalog()
        catalog.discover([capability_dir])
        fabric = CapabilityFabric(catalog, CapabilityRuntime(ToolRegistry()))

        old_secret = os.environ.get("TRINA_CAPABILITY_TEST_SECRET")
        os.environ["TRINA_CAPABILITY_TEST_SECRET"] = "must-not-leak"
        self.addCleanup(
            lambda: os.environ.pop("TRINA_CAPABILITY_TEST_SECRET", None)
            if old_secret is None
            else os.environ.__setitem__("TRINA_CAPABILITY_TEST_SECRET", old_secret)
        )
        lease = fabric.lease(
            "worker-mcp", "task-mcp", "echo",
            constraints={"selectors": ["fixture.mcp.echo"]},
        )
        result = fabric.invoke(
            "fixture.mcp.echo", {"value": "fixture"},
            {"lease_id": lease.id, "ctx": self.context},
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["value"], "fixture")
        self.assertEqual(result.data["inherited_secret"], "")
        self.assertEqual(len(fabric.refs_for_task("task-mcp")), 1)
        self.assertTrue(self.audit.verify()[0])
        fabric.release(lease.id)


if __name__ == "__main__":
    unittest.main()
