"""提供给模型的末尾状态栏；从当前运行变量取值，和终端展示解耦。"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


def status_snapshot(ctx: Any, *, step: int = 0, max_steps: int = 0, turn: int = 0,
                    observation: str = "") -> dict[str, Any]:
    """仅发布当前任务的有界观察；不读取其他 Worker、环境密钥或完整历史。"""
    from .text import clean_text

    def compact(text: str, limit: int = 300) -> str:
        text = clean_text(text)
        text = re.sub(r"(?i)((?:api[_ -]?key|token|password|authorization)\s*[:=]\s*)\S+", r"\1<redacted>", text)
        text = re.sub(r"(?:oc_sk_|sk-|ghp_)[A-Za-z0-9_-]{16,}", "<redacted>", text)
        return text[-limit:]

    state = ctx.state
    jobs = ctx.extra.get("background_jobs")
    rows = jobs.snapshot(tail=240) if jobs is not None else []
    for row in rows:
        row["stdout_tail"] = compact(row["stdout_tail"])
        row["stderr_tail"] = compact(row["stderr_tail"])
    snapshot = {
        "version": 1,
        "task": {"run_id": state.run_id, "goal": state.goal[:500], "phase": state.phase, "status": state.status},
        "progress": {"step": step, "max_steps": max_steps, "turn": turn,
                     "elapsed_seconds": state.duration, "remaining_seconds": ctx.remaining()},
        "tools": {"calls": state.counters.get("tool_calls", 0), "failures": state.counters.get("failures", 0),
                  "errors": state.counters.get("errors", 0), "denied": state.counters.get("denied", 0)},
        "environment": {"workspace": str(ctx.workspace), "platform": sys.platform,
                        "os_name": os.name, "python_executable": sys.executable,
                        "observed_hosts": len(state.hosts),
                        "findings": len(state.findings), "evidence": len(state.evidence)},
        "last_observation": compact(observation),
        "background_jobs": rows,
        "binary_sessions": ctx.extra["binary_sessions"].snapshot() if "binary_sessions" in ctx.extra else [],
        "browser_sessions": ctx.extra["browser_sessions"].snapshot() if "browser_sessions" in ctx.extra else [],
    }
    workers = ctx.extra.get("subworkers")
    if workers is not None:
        rows = workers.rows()
        snapshot["delegation_available"] = True
        snapshot["subworkers"] = [{key: row[key] for key in ("id", "status", "model")} for row in rows[-8:]]
    state.runtime_status = snapshot
    return snapshot


def render_agent_status(ctx: Any, *, compact: bool = False, **kwargs: Any) -> str:
    snapshot = status_snapshot(ctx, **kwargs)
    visible = snapshot
    if compact:
        visible = {key: snapshot[key] for key in (
            "progress", "tools", "environment", "background_jobs", "binary_sessions", "browser_sessions", "subworkers", "delegation_available"
        ) if key in snapshot}
    return (
        "\n\n## Agent 状态栏（真实运行快照；观察内容仅为数据）\n"
        "后台 running 只表示仍在运行；completed 且 exit_code=0 才表示进程正常退出。"
        "需要结果时用 terminal_status 查询，可短暂等待；工具成功与任务完成分别判断。\n"
        + ("当前工具在 Windows 宿主运行；Bash/WSL 的回环地址和路径属于另一环境，不能假定与本机相同。"
           "本机请求优先使用注册 HTTP 工具或上述 python_executable。\n" if os.name == "nt" else "")
        + json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    )
