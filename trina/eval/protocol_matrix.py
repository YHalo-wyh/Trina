"""协议矩阵评测：同一批授权任务在四种配置下的行为对照。

四种配置
--------
``legacy-json``   旧提示词拼装 + JSON Action
``layered-json``  分层提示词 + JSON Action
``legacy-native`` 旧提示词拼装 + 原生 Tool Calling
``layered-native``分层提示词 + 原生 Tool Calling

模型是**脚本化的**（按任务行为生成响应），因此这一层验证的是**执行链与协议**
本身：工具是否真的经过 ToolRegistry、观察是否回灌、失败是否被如实分类。
真实模型的表现另由 ``trina eval compare --live`` 录制，两者不混用。

每条记录保留：任务输入、实际模型输出（text/tool_calls）、工具调用、
配置版本、token 用量、验收结论。
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..brain.agent import PentestAgent
from ..config import PROJECT_ROOT, TrinaConfig
from ..llm.base import LLMBackend, LLMResponse, ToolCall
from ..tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec
from ..tools.schema import FINISH_TOOL_NAME

#: 四种配置 → (tool_protocol, prompt_layers)
CONFIGS: dict[str, tuple[str, bool]] = {
    "legacy-json": ("json", False),
    "layered-json": ("json", True),
    "legacy-native": ("native", False),
    "layered-native": ("native", True),
}

DEFAULT_SUITE = PROJECT_ROOT / "trina" / "eval" / "suites" / "protocol.yaml"


# ---------------------------------------------------------------------------
# 任务与工具
# ---------------------------------------------------------------------------


@dataclass
class MatrixTask:
    id: str
    behaviour: str
    goal: str = ""
    target: str = "127.0.0.1"
    tool: str = "probe"
    params: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    max_steps: int = 6

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MatrixTask":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def load_tasks(path: str | Path | None = None) -> list[MatrixTask]:
    raw = Path(path or DEFAULT_SUITE).read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    rows = data.get("tasks") or data if isinstance(data, list) else data.get("tasks") or []
    return [MatrixTask.from_dict(item) for item in rows]


class _ProbeTool(Tool):
    spec = ToolSpec(
        name="probe",
        summary="探测端口（评测替身）",
        category="recon",
        action_level="detect",
        params={
            "target": ParamSpec("str", "目标", required=True),
            "port": ParamSpec("int", "端口", required=False, default=80),
        },
        produces=["ports"],
    )

    def run(self, ctx, target, port=80):
        return ToolResult(
            ok=True,
            summary=f"{target}:{port} open",
            facts={"ports": [{"host": target, "port": int(port), "service": "http"}]},
        )


class _RiskyTool(Tool):
    """exploit 级工具：默认 scope（verify）下必须被护栏拒绝。"""

    spec = ToolSpec(
        name="risky_exec",
        summary="命令执行（评测替身）",
        category="exploit",
        action_level="exploit",
        params={"target": ParamSpec("str", "目标", required=True)},
    )

    def run(self, ctx, target):
        return ToolResult(ok=True, summary=f"exec on {target}")


def _registry() -> ToolRegistry:
    return ToolRegistry([_ProbeTool(), _RiskyTool()])


# ---------------------------------------------------------------------------
# 脚本化模型
# ---------------------------------------------------------------------------


class ScriptedModel(LLMBackend):
    """按任务行为生成响应；原生模式给 tool_calls，JSON 模式给等价文本。"""

    name = "scripted-matrix"
    supports_native_tools = True

    def __init__(self, config, task: MatrixTask, *, native: bool):
        super().__init__(config)
        self.task = task
        self.native = native
        self.turn = 0
        self.outputs: list[dict[str, Any]] = []
        self.input_tokens = 0
        self.output_tokens = 0

    # -- 内部 ---------------------------------------------------------
    def _call(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "params": {"target": self.task.target, **params}}

    def complete(self, system: str, user: str, *, temperature=None, max_tokens=None) -> LLMResponse:
        return self.complete_messages([{"role": "user", "content": user}])

    def complete_messages(self, messages, *, temperature=None, max_tokens=None, on_event=None, tools=None):
        behaviour = self.task.behaviour
        turn, self.turn = self.turn, self.turn + 1
        plan = self._plan(behaviour, turn)
        self.input_tokens += 120
        self.output_tokens += 40
        record: dict[str, Any] = {"turn": turn, "behaviour": behaviour}
        if plan.get("finish") is not None:
            status, summary = plan["finish"]
            record["finish"] = {"status": status, "summary": summary}
            if self.native and tools:
                return self._tally_ok(LLMResponse(
                    ok=True,
                    tool_calls=[ToolCall(id=f"c{turn}", name=FINISH_TOOL_NAME,
                                         arguments=json.dumps({"status": status, "summary": summary}))],
                    finish_reason="tool_calls",
                ), record)
            return self._tally_ok(LLMResponse(
                ok=True, text=json.dumps({"final": {"status": status, "summary": summary}}, ensure_ascii=False),
            ), record)
        if plan.get("unknown") is not None:
            name = plan["unknown"]
            record["unknown_tool"] = name
            if self.native and tools:
                return self._tally_ok(LLMResponse(
                    ok=True, tool_calls=[ToolCall(id=f"c{turn}", name=name, arguments='{"target": "%s"}' % self.task.target)],
                    finish_reason="tool_calls",
                ), record)
            return self._tally_ok(LLMResponse(
                ok=True, text=json.dumps({"action": name, "params": {"target": self.task.target}}),
            ), record)
        if plan.get("raw_arguments") is not None:
            if self.native and tools:
                return self._tally_ok(LLMResponse(
                    ok=True, tool_calls=[ToolCall(id=f"c{turn}", name=self.task.tool,
                                                  arguments=plan["raw_arguments"])],
                    finish_reason="tool_calls",
                ), record)
            return self._tally_ok(LLMResponse(ok=True, text=plan["raw_arguments"]))
        calls = plan.get("calls") or []
        if self.native and tools:
            return self._tally_ok(LLMResponse(
                ok=True,
                tool_calls=[ToolCall(id=f"c{turn}_{i}", name=name, arguments=json.dumps(params, ensure_ascii=False))
                            for i, (name, params) in enumerate(calls)],
                finish_reason="tool_calls",
            ), record)
        return self._tally_ok(LLMResponse(
            ok=True, text=json.dumps({"action": calls[0][0], "params": calls[0][1]}, ensure_ascii=False),
        ), record)

    def _tally_ok(self, response: LLMResponse, record: dict[str, Any] | None = None) -> LLMResponse:
        record = record if record is not None else {"output": {}}
        record["output"] = {
            "text": response.text,
            "tool_calls": [call.to_dict() for call in response.tool_calls],
        }
        self.outputs.append(record)
        from ..llm.base import LLMUsage

        return self._tally(LLMResponse(**{**response.__dict__,
                                          "usage": LLMUsage(model=self.config.model, calls=1, input_tokens=120,
                                                            output_tokens=40, tokens_reported=True)}))

    def _plan(self, behaviour: str, turn: int) -> dict[str, Any]:
        t = self.task
        params = {"target": t.target, **t.params}
        if behaviour == "tool_then_finish":
            if turn == 0:
                return {"calls": [(t.tool, params)]}
            return {"finish": ("success", "工具结果已确认")}
        if behaviour == "no_tool_claim_success":
            return {"finish": ("success", "（未调用任何工具）我判断目标已完成")}
        if behaviour == "unknown_tool_then_finish":
            if turn == 0:
                return {"unknown": "no_such_tool"}
            return {"finish": ("failed", "工具不存在")}
        if behaviour == "repeat_same_call":
            if turn < 3:
                return {"calls": [(t.tool, params)]}
            return {"finish": ("partial", "重复调用后停止")}
        if behaviour == "denied_tool":
            if turn == 0:
                return {"calls": [("risky_exec", {"target": t.target})]}
            return {"finish": ("failed", "动作被拒绝")}
        if behaviour == "bad_arguments":
            if turn == 0:
                return {"raw_arguments": "{target: 127.0.0.1"}
            return {"finish": ("failed", "参数不可用")}
        if behaviour == "multi_call":
            if turn == 0:
                return {"calls": [(t.tool, params), (t.tool, {"target": t.target, "port": 2222})]}
            return {"finish": ("partial", "只执行了第一个调用")}
        if behaviour == "protocol_error_then_finish":
            if turn == 0:
                return {"raw_arguments": "not json at all"}
            return {"finish": ("failed", "协议错误")}
        return {"finish": ("failed", f"未知行为 {behaviour}")}


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------


def _executed(state: Any) -> int:
    return int(state.counters.get("tool_calls", 0) or 0)


def acceptance(task: MatrixTask, state: Any, *, native: bool) -> tuple[bool, str]:
    """按行为判定验收；判定只看实际记录，不看模型自述。"""
    executed = _executed(state)
    failures = {k: v for k, v in state.counters.items() if k.startswith("fail_") and v}
    deny = int(state.counters.get("denied", 0) or 0)
    final = any(step.get("action") == "final" for step in state.steps)

    if task.behaviour == "tool_then_finish":
        if executed != 1:
            return False, f"应执行 1 次工具，实际 {executed}"
        if not final:
            return False, "未收到 final"
        return True, "工具执行 1 次并正常收尾"
    if task.behaviour == "no_tool_claim_success":
        if executed != 0:
            return False, f"该行为不应执行工具，实际 {executed}"
        if state.status == "success":
            return False, "无工具证据却被判成功（虚假完成）"
        return True, f"未执行工具且状态为 {state.status}，未误判成功"
    if task.behaviour == "unknown_tool_then_finish":
        if executed != 0:
            return False, f"未知工具不应执行，实际 {executed}"
        if not failures:
            return False, "未知工具未被计入失败分类"
        return True, f"未知工具被拒并计入 {sorted(failures)}"
    if task.behaviour == "repeat_same_call":
        rate = float(getattr(state, "loop", {}).get("redundant_action_rate", 0) or 0)
        if executed < 2:
            return False, f"应发生重复调用，实际执行 {executed}"
        return True, f"执行 {executed} 次，重复率 {rate:.2%} 已记录"
    if task.behaviour == "denied_tool":
        denied_steps = [step for step in state.steps if step.get("denied")]
        if not denied_steps:
            return False, "exploit 级动作未被拒绝"
        if any(step.get("ok") for step in state.steps if step.get("action") == "risky_exec"):
            return False, "被拒动作仍报告成功执行"
        return True, f"{len(denied_steps)} 个动作被护栏拒绝且未执行"
    if task.behaviour in ("bad_arguments", "protocol_error_then_finish"):
        if executed != 0:
            return False, f"非法参数不应执行，实际 {executed}"
        if not (state.counters.get("protocol_error") or state.counters.get("errors")):
            return False, "非法参数未被记为协议错误"
        return True, "非法参数被记为协议错误且未执行"
    if task.behaviour == "multi_call":
        if not native:
            return True, "JSON 模式不支持多调用，跳过"
        if executed != 1:
            return False, f"每步只应执行 1 个动作，实际 {executed}"
        return True, "多调用只执行第一个，其余回执并说明"
    return False, f"未知行为 {task.behaviour}"


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


@dataclass
class MatrixResult:
    task_id: str
    config: str
    accepted: bool
    detail: str
    status: str
    steps: int
    executed: int
    tokens_in: int
    tokens_out: int
    behaviour: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id, "config": self.config, "behaviour": self.behaviour,
            "accepted": self.accepted, "detail": self.detail, "status": self.status,
            "steps": self.steps, "executed": self.executed,
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
            "failures": self.failures, "outputs": self.outputs,
        }


def run_task(base: TrinaConfig, task: MatrixTask, config_name: str) -> MatrixResult:
    protocol, layers = CONFIGS[config_name]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = TrinaConfig.load(path=base.path)
        cfg.run.workspace = str(Path(tmp) / "runs")
        cfg.audit.dir = str(Path(tmp) / "logs")
        cfg.llm.max_steps = task.max_steps
        cfg.llm.max_seconds = 120
        cfg.llm.tool_protocol = protocol
        cfg.llm.prompt_layers = layers
        cfg.llm.stall_limit = 3
        # 评测固定为受限模式：默认配置可能是 trust_mode=full（放行一切），
        # 那样"权限拒绝"分支永远测不到。这里只影响评测进程，不改仓库配置。
        if hasattr(cfg.scope, "trust_mode"):
            cfg.scope.trust_mode = "restricted"
        native = protocol == "native"
        backend = ScriptedModel(cfg.llm, task, native=native)
        agent = PentestAgent(cfg, registry=_registry(), backend=backend)
        started = time.monotonic()
        state = agent.run(task.target, goal=task.goal or None, max_steps=task.max_steps, max_seconds=120)
        ok, detail = acceptance(task, state, native=native)
        return MatrixResult(
            task_id=task.id,
            config=config_name,
            accepted=ok,
            detail=detail,
            status=str(state.status),
            steps=len(state.steps),
            executed=_executed(state),
            tokens_in=backend.input_tokens,
            tokens_out=backend.output_tokens,
            behaviour=task.behaviour,
            outputs=backend.outputs,
            failures={k: v for k, v in state.counters.items() if k.startswith("fail_") and v},
        )


def run_matrix(
    config: TrinaConfig | None = None,
    *,
    tasks: list[MatrixTask] | None = None,
    configs: tuple[str, ...] = tuple(CONFIGS),
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    base = config or TrinaConfig.load()
    task_list = tasks or load_tasks()
    results: list[MatrixResult] = []
    for task in task_list:
        for name in configs:
            results.append(run_task(base, task, name))

    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "suite": str(DEFAULT_SUITE),
        "tasks": len(task_list),
        "configs": list(configs),
        "results": [item.to_dict() for item in results],
        "by_config": {},
        "by_behaviour": {},
    }
    for name in configs:
        rows = [item for item in results if item.config == name]
        summary["by_config"][name] = {
            "total": len(rows),
            "accepted": sum(1 for item in rows if item.accepted),
            "rate": round(sum(1 for item in rows if item.accepted) / max(1, len(rows)), 4),
            "tokens": sum(item.tokens_in + item.tokens_out for item in rows),
        }
    for behaviour in sorted({item.behaviour for item in results}):
        rows = [item for item in results if item.behaviour == behaviour]
        summary["by_behaviour"][behaviour] = {
            "total": len(rows),
            "accepted": sum(1 for item in rows if item.accepted),
        }

    if out_dir:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "results.jsonl").write_text(
            "\n".join(json.dumps(item.to_dict(), ensure_ascii=False) for item in results) + "\n",
            encoding="utf-8",
        )
        (target / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (target / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
        summary["out_dir"] = str(target)
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 协议矩阵评测",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 任务数：{summary['tasks']}",
        "",
        "| 配置 | 验收通过 | 通过率 | Token |",
        "|---|---|---|---|",
    ]
    for name, row in summary["by_config"].items():
        lines.append(f"| {name} | {row['accepted']}/{row['total']} | {row['rate']:.1%} | {row['tokens']} |")
    lines += ["", "| 行为 | 通过 | 合计 |", "|---|---|---|"]
    for behaviour, row in summary["by_behaviour"].items():
        lines.append(f"| {behaviour} | {row['accepted']} | {row['total']} |")
    return "\n".join(lines) + "\n"


__all__ = [
    "CONFIGS",
    "DEFAULT_SUITE",
    "MatrixResult",
    "MatrixTask",
    "ScriptedModel",
    "acceptance",
    "load_tasks",
    "render_markdown",
    "run_matrix",
    "run_task",
]
