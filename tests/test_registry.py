"""工具注册表测试：参数校验、护栏拒绝、超时、异常隔离、事实吸收。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from trina.audit import AuditLog
from trina.config import PAIConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools.http import HttpClient
from trina.tools.registry import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec


class _Echo(Tool):
    spec = ToolSpec(
        name="echo_tool",
        summary="回显",
        category="utility",
        action_level="detect",
        params={
            "target": ParamSpec("str", "目标", required=True),
            "count": ParamSpec("int", "次数", required=False, default=1),
            "flag": ParamSpec("bool", "开关", required=False, default=False),
            "tags": ParamSpec("list", "标签", required=False, default=[]),
            "mode": ParamSpec("str", "模式", required=False, default="a", enum=["a", "b"]),
        },
    )

    def run(self, ctx, **params):
        return ToolResult(
            ok=True,
            summary=f"echo {params}",
            facts={"ports": [{"host": params["target"], "port": 80, "service": "http"}]},
            evidence=[{"kind": "test", "target": params["target"]}],
        )


class _Boom(Tool):
    spec = ToolSpec(name="boom_tool", summary="炸", action_level="detect", params={"target": ParamSpec("str", required=True)})

    def run(self, ctx, **params):
        raise RuntimeError("internal failure")


class _Slow(Tool):
    spec = ToolSpec(name="slow_tool", summary="慢", action_level="detect", params={"target": ParamSpec("str", required=True)})

    def run(self, ctx, **params):
        time.sleep(3)
        return ToolResult(ok=True, summary="done")


class _Destructive(Tool):
    spec = ToolSpec(
        name="destructive_tool",
        summary="破坏性",
        action_level="exploit",
        destructive=True,
        params={"target": ParamSpec("str", required=True)},
    )

    def run(self, ctx, **params):
        return ToolResult(ok=True, summary="已执行")


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = PAIConfig.load()
        self.cfg.run.tool_timeout = 2
        self.audit = AuditLog.open(self.tmp.name, run_id="reg", resume=False)
        self.guard = ScopeGuard(self.cfg.scope, audit=self.audit)
        self.state = RunState(run_id="reg", target="127.0.0.1")
        self.ctx = ToolContext(
            config=self.cfg,
            guard=self.guard,
            audit=self.audit,
            state=self.state,
            http=HttpClient(self.guard),
            workspace=Path(self.tmp.name),
            deadline=time.monotonic() + 30,
        )
        self.registry = ToolRegistry([_Echo(), _Boom(), _Slow(), _Destructive()])

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 校验
    def test_unknown_tool(self):
        result = self.registry.execute("nope", {}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)
        self.assertIn("available_tools", result.data)

    def test_unknown_param_rejected(self):
        result = self.registry.execute("echo_tool", {"target": "127.0.0.1", "bogus": 1}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("未知参数", result.error)
        self.assertIn("valid_params", result.data)

    def test_missing_required_rejected(self):
        result = self.registry.execute("echo_tool", {}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.error)

    def test_type_coercion_and_enum(self):
        ok = self.registry.execute("echo_tool", {"target": "127.0.0.1", "count": "7", "flag": "yes", "tags": "a,b", "mode": "b"}, self.ctx)
        self.assertTrue(ok.ok, ok.error)
        bad = self.registry.execute("echo_tool", {"target": "127.0.0.1", "mode": "z"}, self.ctx)
        self.assertFalse(bad.ok)
        self.assertIn("取值必须是", bad.error)

    # ------------------------------------------------------------------ 护栏
    def test_out_of_scope_target_denied(self):
        result = self.registry.execute("echo_tool", {"target": "8.8.8.8"}, self.ctx)
        self.assertTrue(result.denied)
        self.assertEqual(result.category, "scope")
        self.assertTrue(result.next_hints)
        self.assertGreaterEqual(self.state.counters["denied"], 1)

    def test_action_level_gate(self):
        self.cfg.scope.exploit_level = "detect"
        result = self.registry.execute("destructive_tool", {"target": "127.0.0.1"}, self.ctx)
        self.assertTrue(result.denied)
        self.assertEqual(result.category, "action-level")

    def test_destructive_switch_off(self):
        self.cfg.scope.exploit_level = "exploit"
        result = self.registry.execute("destructive_tool", {"target": "127.0.0.1"}, self.ctx)
        self.assertTrue(result.denied)
        self.assertEqual(result.category, "destructive")

    def test_denials_are_audited(self):
        self.registry.execute("echo_tool", {"target": "8.8.8.8"}, self.ctx)
        events = [record.get("event") for record in self.audit.read_all()]
        self.assertIn("tool.denied", events)

    # ------------------------------------------------------------------ 执行
    def test_success_path_absorbs_facts(self):
        result = self.registry.execute("echo_tool", {"target": "127.0.0.1"}, self.ctx)
        self.assertTrue(result.ok)
        self.assertIn(80, self.state.hosts["127.0.0.1"].ports)
        self.assertEqual(self.state.evidence[-1]["kind"], "test")
        events = [record.get("event") for record in self.audit.read_all()]
        self.assertIn("tool.start", events)
        self.assertIn("tool.end", events)

    def test_exception_isolated(self):
        result = self.registry.execute("boom_tool", {"target": "127.0.0.1"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error)
        self.assertGreaterEqual(self.state.counters["errors"], 1)

    def test_timeout(self):
        result = self.registry.execute("slow_tool", {"target": "127.0.0.1"}, self.ctx, timeout=0.3)
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)

    def test_prompt_block_and_describe(self):
        block = self.registry.prompt_block()
        self.assertIn("echo_tool", block)
        described = {item["name"] for item in self.registry.describe()}
        self.assertIn("echo_tool", described)


if __name__ == "__main__":
    unittest.main()
