"""真模型跑批：30 条授权任务 × 协议/提示词配置，逐条记录并统计。

与 :mod:`trina.eval.protocol_matrix` 的分工
-------------------------------------------
* matrix 用脚本化模型，验证**协议与执行链**（确定性、无网络、无 token 成本）；
* 本模块用**真实模型 + 真实靶机**，验证**模型能不能选对工具并真的执行成功**，
  产出可复现的逐条记录（输入、模型原始输出、工具序列、token、验收）。

验收只看步骤记录，不看模型自述：
``chosen`` 期望工具是否被执行、``ok`` 是否 ok=True 完成、``flag`` 是否拿到 flag。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..brain.agent import PentestAgent
from ..config import PROJECT_ROOT, TrinaConfig
from ..llm import make_backend
from ..tools import build_registry

DEFAULT_SUITE = PROJECT_ROOT / "trina" / "eval" / "suites" / "live.yaml"
LAB_URL = "http://127.0.0.1:9099/"

CONFIGS: dict[str, tuple[str, bool]] = {
    "legacy-json": ("json", False),
    "layered-json": ("json", True),
    "legacy-native": ("native", False),
    "layered-native": ("native", True),
}


@dataclass
class LiveTask:
    id: str
    goal: str
    target: str
    expect_tool: str = ""
    expect_ok: bool = True
    expect_flag: bool = False
    max_steps: int = 3
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTask":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def load_tasks(path: str | Path | None = None) -> list[LiveTask]:
    raw = Path(path or DEFAULT_SUITE).read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    rows = data.get("tasks") or []
    return [LiveTask.from_dict(item) for item in rows]


def lab_alive(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(LAB_URL, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - 探活失败一律视为不可用
        return False


def ensure_lab(*, port: int = 9099) -> subprocess.Popen | None:
    """靶机没起就拉一个，返回需要回收的进程（已在跑则返回 None）。"""
    if lab_alive():
        return None
    process = subprocess.Popen(
        ["python3", "-m", "trina.lab.run", "--http", "--port", str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    for _ in range(30):
        time.sleep(0.5)
        if lab_alive():
            return process
    process.terminate()
    raise RuntimeError("靶机启动失败：127.0.0.1:9099 不可达")


class RecordingBackend:
    """原样委托真实后端，记录每次请求与响应（用于逐条留证）。"""

    def __init__(self, inner: Any):
        self.inner = inner
        self.exchanges: list[dict[str, Any]] = []

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def complete(self, system: str, user: str, **kwargs: Any):
        response = self.inner.complete(system, user, **kwargs)
        self._record(user, response)
        return response

    def complete_messages(self, messages, **kwargs: Any):
        response = self.inner.complete_messages(messages, **kwargs)
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        self._record(last_user, response)
        return response

    def _record(self, prompt: str, response: Any) -> None:
        self.exchanges.append({
            "prompt_chars": len(prompt or ""),
            "text": getattr(response, "text", ""),
            "tool_calls": [call.to_dict() for call in getattr(response, "tool_calls", []) or []],
            "ok": bool(getattr(response, "ok", False)),
            "error": getattr(response, "error", ""),
            "reasoning_chars": len(getattr(response, "reasoning", "") or ""),
            "usage": {
                "in": getattr(getattr(response, "usage", None), "input_tokens", 0),
                "out": getattr(getattr(response, "usage", None), "output_tokens", 0),
            },
        })


def judge(task: LiveTask, steps: list[dict[str, Any]], flags: list[Any]) -> dict[str, bool]:
    """只看步骤记录的验收判定：选对工具 / 执行成功 / 拿到 flag / 是否通过。"""
    executed = [s for s in steps if s.get("action") and s.get("action") not in ("final", "final_rejected")]
    hit = [s for s in executed if s.get("action") == task.expect_tool]
    chosen = bool(hit)
    tool_ok = any(bool(s.get("ok")) for s in hit)
    accepted = chosen and (tool_ok or not task.expect_ok) and (bool(flags) or not task.expect_flag)
    return {"chosen": chosen, "tool_ok": tool_ok, "flag": bool(flags), "accepted": accepted}


def run_task(config: TrinaConfig, task: LiveTask, config_name: str) -> dict[str, Any]:
    protocol, layers = CONFIGS[config_name]
    with __import__("tempfile").TemporaryDirectory() as tmp:
        cfg = TrinaConfig.load(path=config.path)
        cfg.run.workspace = str(Path(tmp) / "runs")
        cfg.audit.dir = str(Path(tmp) / "logs")
        cfg.llm.max_steps = task.max_steps
        cfg.llm.max_seconds = 240
        cfg.llm.tool_protocol = protocol
        cfg.llm.prompt_layers = layers
        backend = RecordingBackend(make_backend(cfg.llm))
        agent = PentestAgent(cfg, registry=build_registry(), backend=backend)
        started = time.monotonic()
        state = agent.run(task.target, goal=task.goal, max_steps=task.max_steps, max_seconds=240)
        duration = round(time.monotonic() - started, 2)

        steps = [s for s in state.steps if isinstance(s, dict)]
        executed = [s for s in steps if s.get("action") and s.get("action") not in ("final", "final_rejected")]
        verdict = judge(task, steps, list(state.flags or []))
        chosen, ok, accepted = verdict["chosen"], verdict["tool_ok"], verdict["accepted"]
        ledger = state.ledger.to_dict() if hasattr(state.ledger, "to_dict") else (state.ledger or {})
        return {
            "task": task.id,
            "config": config_name,
            "protocol": protocol,
            "layers": layers,
            "goal": task.goal,
            "target": task.target,
            "expect_tool": task.expect_tool,
            "expect_ok": task.expect_ok,
            "expect_flag": task.expect_flag,
            "accepted": accepted,
            "chosen": chosen,
            "tool_ok": ok,
            "flag": bool(state.flags),
            "status": state.status,
            "steps": len(steps),
            "executed": len(executed),
            "tools": [s.get("action") for s in executed],
            "step_errors": [s.get("error") for s in steps if s.get("error")],
            "protocol_error": state.counters.get("protocol_error", 0),
            "errors": state.counters.get("errors", 0),
            "denied": state.counters.get("denied", 0),
            "native_calls": state.counters.get("native_calls", 0),
            "llm_calls": state.counters.get("llm_calls", ledger.get("calls", 0)),
            "tokens_in": ledger.get("input_tokens", 0),
            "tokens_out": ledger.get("output_tokens", 0),
            "duration": duration,
            "exchanges": backend.exchanges,
        }


def run_batch(
    config: TrinaConfig | None = None,
    *,
    tasks: list[LiveTask] | None = None,
    configs: tuple[str, ...] = ("legacy-json", "legacy-native"),
    out_dir: str | Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    base = config or TrinaConfig.load()
    task_list = (tasks or load_tasks())[: limit or None]
    lab = ensure_lab()
    results: list[dict[str, Any]] = []
    try:
        for task in task_list:
            for name in configs:
                try:
                    results.append(run_task(base, task, name))
                except Exception as exc:  # noqa: BLE001 - 单条失败不能毁掉整批
                    results.append({
                        "task": task.id, "config": name, "accepted": False,
                        "error": f"{type(exc).__name__}: {exc}", "goal": task.goal,
                    })
                print(f"  · {task.id} / {name} -> {results[-1].get('accepted')}", flush=True)
    finally:
        if lab is not None:
            lab.terminate()

    summary = summarise(results, task_list, configs)
    if out_dir:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "results.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in results) + "\n", encoding="utf-8"
        )
        (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (target / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
        summary["out_dir"] = str(target)
    return summary


def summarise(results: list[dict[str, Any]], tasks: list[LiveTask], configs: tuple[str, ...]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": os.environ.get("TRINA_REPORT_MODEL", ""),
        "tasks": len(tasks),
        "configs": list(configs),
        "by_config": {},
    }
    for name in configs:
        rows = [row for row in results if row.get("config") == name]
        summary["by_config"][name] = {
            "total": len(rows),
            "accepted": sum(1 for row in rows if row.get("accepted")),
            "chosen": sum(1 for row in rows if row.get("chosen")),
            "tool_ok": sum(1 for row in rows if row.get("tool_ok")),
            "flag": sum(1 for row in rows if row.get("flag")),
            "protocol_error": sum(int(row.get("protocol_error", 0) or 0) for row in rows),
            "steps": sum(int(row.get("steps", 0) or 0) for row in rows),
            "tokens_in": sum(int(row.get("tokens_in", 0) or 0) for row in rows),
            "tokens_out": sum(int(row.get("tokens_out", 0) or 0) for row in rows),
            "duration": round(sum(float(row.get("duration", 0) or 0) for row in rows), 1),
        }
    summary["results"] = results
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 真模型跑批",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 任务数：{summary['tasks']}",
        "",
        "| 配置 | 验收通过 | 选对工具 | 执行成功 | 拿到 flag | 协议错误 | 总步数 | Token 入/出 | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, row in summary["by_config"].items():
        lines.append(
            f"| {name} | {row['accepted']}/{row['total']} | {row['chosen']}/{row['total']} | "
            f"{row['tool_ok']}/{row['total']} | {row['flag']} | {row['protocol_error']} | {row['steps']} | "
            f"{row['tokens_in']}/{row['tokens_out']} | {row['duration']} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "CONFIGS",
    "DEFAULT_SUITE",
    "LiveTask",
    "RecordingBackend",
    "ensure_lab",
    "load_tasks",
    "render_markdown",
    "run_batch",
    "run_task",
    "summarise",
]
