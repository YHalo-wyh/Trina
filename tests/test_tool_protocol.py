"""原生 Tool Calling 与 JSON Action 双协议回归。

验收口径（对应改造目标）：
1. schema 由已注册 ToolSpec 推导，Agent Core 不含第二份工具定义；
2. 同一任务能走完「模型决策 → 工具调用 → Observation 回灌 → 后续决策」；
3. 不存在绕过 ToolRegistry 的执行路径（含 finish 控制面）；
4. 旧版 JSON Action 仍可用；
5. 协议/工具失败不会被误报为成功；
6. 失败分类决定继续或停止，协议不支持只降级一次。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trina.brain.agent import PentestAgent
from trina.brain.failure import classify_llm_response, decide_after_failure
from trina.brain.prompts import build_prompt_layers
from trina.config import LLMConfig, TrinaConfig
from trina.llm.base import LLMBackend, LLMResponse, ToolCall
from trina.tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec
from trina.tools.schema import FINISH_TOOL_NAME, tool_schemas


class _ScanTool(Tool):
    spec = ToolSpec(
        name="scan_port",
        summary="扫描目标端口",
        category="recon",
        action_level="detect",
        params={
            "target": ParamSpec("str", "目标", required=True),
            "ports": ParamSpec("str", "端口", required=False, default="80"),
        },
        produces=["ports"],
    )

    def run(self, ctx, target, ports="80"):
        self.calls = getattr(self, "calls", 0) + 1
        return ToolResult(
            ok=True,
            summary=f"扫描 {target}:{ports}",
            facts={"ports": [{"host": target, "port": int(str(ports).split(",")[0]), "service": "http"}]},
        )


class _ScriptedBackend(LLMBackend):
    """按脚本返回原生 tool_calls 或纯文本，并记录每次请求。"""

    name = "scripted"
    supports_native_tools = True

    def __init__(self, config: LLMConfig, script: list[dict]):
        super().__init__(config)
        self.script = list(script)
        self.requests: list[dict] = []

    def complete(self, system: str, user: str, *, temperature=None, max_tokens=None) -> LLMResponse:
        return self.complete_messages([{"role": "user", "content": user}])

    def complete_messages(self, messages, *, temperature=None, max_tokens=None, on_event=None, tools=None):
        self.requests.append(
            {"messages": [dict(item) for item in messages], "tools": tools}
        )
        step = self.script.pop(0) if self.script else {"text": '{"final": {"status": "failed", "summary": "脚本耗尽"}}'}
        if step.get("error"):
            return self._tally(LLMResponse(ok=False, error=step["error"]))
        if "tool" in step:
            name, arguments = step["tool"]
            payload = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
            return self._tally(LLMResponse(
                ok=True, tool_calls=[ToolCall(id=step.get("id", "call_1"), name=name, arguments=payload)],
                reasoning=step.get("reasoning", ""), finish_reason="tool_calls",
            ))
        return self._tally(LLMResponse(ok=True, text=step.get("text", "")))


class ProtocolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = TrinaConfig.load()
        self.cfg.run.workspace = str(Path(self.tmp.name) / "runs")
        self.cfg.audit.dir = str(Path(self.tmp.name) / "logs")
        self.cfg.llm.max_steps = 5

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _agent(self, script: list[dict], *, protocol: str = "auto", layers: bool = False):
        self.cfg.llm.tool_protocol = protocol
        self.cfg.llm.prompt_layers = layers
        backend = _ScriptedBackend(self.cfg.llm, script)
        tool = _ScanTool()
        registry = ToolRegistry([tool])
        agent = PentestAgent(self.cfg, registry=registry, backend=backend)
        return agent, backend, registry, tool


class SchemaTest(unittest.TestCase):
    def test_schema_is_derived_from_registered_specs(self) -> None:
        registry = ToolRegistry([_ScanTool()])
        schemas = tool_schemas(registry)
        names = [item["function"]["name"] for item in schemas]
        self.assertEqual(names[0], "scan_port")
        self.assertIn(FINISH_TOOL_NAME, names)

        scan = schemas[0]["function"]
        self.assertEqual(scan["parameters"]["required"], ["target"])
        self.assertEqual(scan["parameters"]["properties"]["ports"]["type"], "string")
        self.assertEqual(scan["parameters"]["properties"]["ports"]["default"], "80")
        self.assertFalse(scan["parameters"]["additionalProperties"])

    def test_finish_is_the_only_synthetic_entry(self) -> None:
        registry = ToolRegistry([_ScanTool()])
        names = {item["function"]["name"] for item in tool_schemas(registry)}
        self.assertEqual(names - {spec.name for spec in registry.specs()}, {FINISH_TOOL_NAME})
        self.assertIsNone(registry.get(FINISH_TOOL_NAME), "finish 不得注册成可执行工具")


class NativeLoopTest(ProtocolTestBase):
    def test_native_recording_replays_calls_and_finish(self) -> None:
        from trina.llm.replay_backend import ReplayBackend
        agent, backend, registry, tool = self._agent([
            {"tool": ("scan_port", {"target": "127.0.0.1", "ports": "9099"})},
            {"tool": (FINISH_TOOL_NAME, {"status": "partial", "summary": "完成"})},
        ])
        record = Path(self.tmp.name) / "native.json"
        first = agent.run("127.0.0.1", record_path=record)
        script = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(script["steps"][0]["tool_calls"][0]["name"], "scan_port")
        replay = ReplayBackend(LLMConfig(backend="replay", replay_script=str(record)))
        second = PentestAgent(self.cfg, registry=registry, backend=replay).run("127.0.0.1")
        self.assertEqual([step["action"] for step in first.steps if step.get("action")],
                         [step["action"] for step in second.steps if step.get("action")])
        self.assertEqual(tool.calls, 2)

    def test_rejected_native_finish_has_a_tool_receipt_before_next_request(self) -> None:
        agent, backend, _registry, _tool = self._agent([
            {"tool": (FINISH_TOOL_NAME, {"status": "success", "summary": "过早结束"})},
            {"tool": ("scan_port", {"target": "127.0.0.1"})},
            {"tool": (FINISH_TOOL_NAME, {"status": "partial", "summary": "尚未验证"})},
        ])
        agent.run("127.0.0.1", goal="验证 SQL 注入", max_steps=3)
        second = backend.requests[1]["messages"]
        receipt = next(item for item in second if item["role"] == "tool")
        self.assertEqual(receipt["tool_call_id"], "call_1")
        self.assertIn("目标尚未达成", receipt["content"])

    def test_native_tool_call_runs_through_registry_and_feeds_observation_back(self) -> None:
        agent, backend, registry, tool = self._agent([
            {"tool": ("scan_port", {"target": "127.0.0.1", "ports": "9099"})},
            {"tool": (FINISH_TOOL_NAME, {"status": "success", "summary": "端口已确认"})},
        ])
        state = agent.run("127.0.0.1")

        # 结论状态由事实计算（这里只有端口事实 → partial）；协议层面必须是"正常走完"
        self.assertEqual(state.steps[-1]["action"], "final")
        self.assertEqual(state.counters.get("errors", 0), 0)
        self.assertEqual([step.get("action") for step in state.steps if step.get("action")], ["scan_port", "final"])
        self.assertIn("9099", json.dumps(state.to_dict().get("hosts"), ensure_ascii=False), "工具 facts 必须进入状态")

        # 第二次请求里必须带 tool 结果（原生协议要求每个 tool_call 有回执）
        second = backend.requests[1]["messages"]
        tool_messages = [item for item in second if item["role"] == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
        self.assertIn("扫描 127.0.0.1:9099", tool_messages[0]["content"])

        # 首次请求带 schema，且 schema 来自注册表而非硬编码
        self.assertTrue(backend.requests[0]["tools"])
        self.assertIn("scan_port", json.dumps(backend.requests[0]["tools"], ensure_ascii=False))

    def test_finish_is_intercepted_before_any_tool_execution(self) -> None:
        agent, _backend, registry, tool = self._agent([
            {"tool": (FINISH_TOOL_NAME, {"status": "partial", "summary": "无更多动作"})},
        ])
        state = agent.run("127.0.0.1")
        self.assertEqual(getattr(tool, "calls", 0), 0)
        self.assertEqual(state.steps[-1]["action"], "final")
        self.assertFalse(any(step["action"] == FINISH_TOOL_NAME for step in state.steps))

    def test_unknown_native_tool_is_protocol_error_not_success(self) -> None:
        agent, _backend, _registry, tool = self._agent([
            {"tool": ("not_a_tool", {"target": "127.0.0.1"})},
            {"tool": (FINISH_TOOL_NAME, {"status": "failed", "summary": "无法继续"})},
        ])
        state = agent.run("127.0.0.1")
        self.assertEqual(getattr(tool, "calls", 0), 0)
        # 未知工具被注册表拒绝：必须计入某个 fail_* 计数，且不能判成功
        failures = {key: value for key, value in state.counters.items() if key.startswith("fail_") and value}
        self.assertTrue(failures, f"未知工具应计入失败分类，实际 counters={state.counters}")
        self.assertNotEqual(state.status, "success")

    def test_malformed_arguments_are_protocol_error(self) -> None:
        agent, _backend, _registry, tool = self._agent([
            {"tool": ("scan_port", "{not json")},
            {"tool": (FINISH_TOOL_NAME, {"status": "failed", "summary": "参数不可用"})},
        ])
        state = agent.run("127.0.0.1")
        self.assertEqual(getattr(tool, "calls", 0), 0)
        self.assertGreaterEqual(state.counters.get("protocol_error", 0), 1)

    def test_reasoning_content_round_trips_for_thinking_models(self) -> None:
        agent, backend, _registry, _tool = self._agent([
            {"tool": ("scan_port", {"target": "127.0.0.1"}), "reasoning": "先确认端口"},
            {"tool": (FINISH_TOOL_NAME, {"status": "success", "summary": "完成"})},
        ])
        agent.run("127.0.0.1")
        assistant = [item for item in backend.requests[1]["messages"] if item["role"] == "assistant"]
        self.assertEqual(assistant[0].get("reasoning_content"), "先确认端口")
        self.assertEqual(assistant[0]["tool_calls"][0]["id"], "call_1")


class FallbackTest(ProtocolTestBase):
    def test_protocol_unsupported_downgrades_once(self) -> None:
        agent, backend, _registry, tool = self._agent([
            {"error": "PROTOCOL_UNSUPPORTED: tools 不受支持"},
            {"text": '{"action": "scan_port", "params": {"target": "127.0.0.1"}}'},
            {"text": '{"final": {"status": "success", "summary": "完成"}}'},
        ])
        state = agent.run("127.0.0.1")
        self.assertTrue(backend.requests[0]["tools"], "首轮应尝试原生协议")
        self.assertIsNone(backend.requests[1]["tools"], "降级后不得再发 tools")
        self.assertIsNone(backend.requests[2]["tools"])
        self.assertEqual(getattr(tool, "calls", 1), 1)
        self.assertEqual(state.counters.get("llm_protocol_unsupported"), 1, "协议不支持只计一次")
        self.assertEqual(state.counters.get("llm_protocol_unsupported"), 1)

    def test_json_protocol_remains_available(self) -> None:
        agent, backend, _registry, tool = self._agent([
            {"text": '{"action": "scan_port", "params": {"target": "127.0.0.1", "ports": "2222"}}'},
            {"text": '{"final": {"status": "success", "summary": "完成"}}'},
        ], protocol="json")
        state = agent.run("127.0.0.1")
        self.assertIsNone(backend.requests[0]["tools"])
        self.assertEqual(getattr(tool, "calls", 1), 1)
        self.assertEqual(state.steps[-1]["action"], "final")
        self.assertEqual(state.counters.get("errors", 0), 0)


class ClassificationTest(unittest.TestCase):
    def test_llm_failure_classes_and_decisions(self) -> None:
        cases = [
            (LLMResponse(ok=True, text="x"), "ok"),
            (LLMResponse(ok=True, tool_calls=[ToolCall("1", "scan_port", "{}")]), "ok"),
            (LLMResponse(ok=False, error="PROTOCOL_UNSUPPORTED: 未启用原生工具调用"), "protocol_unsupported"),
            (LLMResponse(ok=False, error="响应格式异常：缺少模型输出字段"), "protocol_error"),
            (LLMResponse(ok=False, error="HTTP 401: Unauthorized"), "permission_denied"),
            (LLMResponse(ok=False, error="请求失败: timed out"), "run_failure"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_llm_response(response), expected)

        self.assertEqual(decide_after_failure("protocol_unsupported", attempt=0), "downgrade")
        self.assertEqual(decide_after_failure("permission_denied", attempt=0), "stop")
        self.assertEqual(decide_after_failure("protocol_error", attempt=0), "continue")
        self.assertEqual(decide_after_failure("protocol_error", attempt=2), "stop")
        self.assertEqual(decide_after_failure("run_failure", attempt=3), "continue")
        self.assertEqual(decide_after_failure("run_failure", attempt=9), "stop")


class PromptLayerTest(unittest.TestCase):
    def test_layers_split_stable_and_task_scoped_content(self) -> None:
        layers = build_prompt_layers(
            scope_block="授权网段: 127.0.0.0/8",
            tool_block="### scan_port",
            extra_rules="只验证 labs",
            policy_block="T-1 scan_port",
            role_profile="角色：安全工程师",
            protocol="native",
        )
        self.assertIn("安全工程师", layers.system())
        self.assertIn("角色：安全工程师", layers.system())
        self.assertNotIn("授权网段", layers.system())
        contract = layers.first_user()
        for token in ("工具协议（原生 tool calling）", "授权网段", "只验证 labs", "T-1 scan_port", "### scan_port"):
            self.assertIn(token, contract)
        self.assertNotIn("finish(status", layers.system())

    def test_json_protocol_contract_mentions_action_json(self) -> None:
        layers = build_prompt_layers(protocol="json")
        self.assertIn('"action"', layers.first_user())


class LayeredRunTest(ProtocolTestBase):
    def test_layered_prompt_run_keeps_system_stable_across_steps(self) -> None:
        agent, backend, _registry, _tool = self._agent([
            {"tool": ("scan_port", {"target": "127.0.0.1"})},
            {"tool": (FINISH_TOOL_NAME, {"status": "success", "summary": "完成"})},
        ], layers=True)
        state = agent.run("127.0.0.1")
        systems = {item["messages"][0]["content"] for item in backend.requests}
        self.assertEqual(len(systems), 1, "system 前缀必须跨步稳定")
        self.assertEqual(state.steps[-1]["action"], "final")
        self.assertEqual(state.counters.get("errors", 0), 0)


if __name__ == "__main__":
    unittest.main()
