"""研究工作台面板：把「任务状态 / 证据 / 假设 / 覆盖 / 成本」压成一屏可读的视图。

设计目标（对齐文档的建议）
------------------------
* 打开时第一眼看到的是**任务状态与研究成果**，而不是滚动日志；
* Agent 说的不是"正在思考"，而是**阶段 + 已验证数量 + 待验证假设 + 下一步**；
* 同一份渲染逻辑同时给控制台（`/board`）与命令行（`trina watch`）用，避免两套视图漂移。

`render_board()` 是纯函数（输入 dict / RunState，输出字符串），便于单测。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
SEVERITY_CN = {"critical": "严重", "high": "高危", "medium": "中危", "low": "低危", "info": "信息"}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 RunState 对象与 state.json 字典。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _seconds(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number < 60:
        return f"{number:.0f}s"
    minutes, seconds = divmod(int(number), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def summarise(state: Any, *, hypotheses: list[dict[str, Any]] | None = None, now: float | None = None) -> dict[str, Any]:
    """把状态压成面板需要的字段（对象/字典都吃）。"""
    findings = list(_get(state, "findings") or [])
    by_severity: dict[str, int] = {sev: 0 for sev in SEVERITY_ORDER}
    verified = 0
    for finding in findings:
        severity = _get(finding, "severity", "info") or "info"
        by_severity[severity] = by_severity.get(severity, 0) + 1
        status = _get(finding, "status", "")
        if status in ("verified", "exploited"):
            verified += 1
    coverage = _get(state, "coverage") or {}
    loop = _get(state, "loop") or {}
    ledger = hypotheses if hypotheses is not None else (_get(state, "hypotheses") or [])
    open_hypotheses = [h for h in ledger if h.get("status") in ("open", "verifying")]
    open_hypotheses.sort(key=lambda h: h.get("priority", 0), reverse=True)
    counters = _get(state, "counters") or {}
    steps = list(_get(state, "steps") or [])
    started = _get(state, "started_at") or 0
    ended = _get(state, "ended_at")
    duration = (ended or (now or time.time())) - started if started else 0

    return {
        "run_id": _get(state, "run_id", ""),
        "target": _get(state, "target", ""),
        "goal": _get(state, "goal", ""),
        "status": _get(state, "status", ""),
        "verdict": _get(state, "verdict", ""),
        "phase": _get(state, "phase", ""),
        "duration_seconds": duration,
        "duration": _seconds(duration),
        "steps": len(steps),
        "findings_total": len(findings),
        "findings_verified": verified,
        "by_severity": by_severity,
        "flags": list(_get(state, "flags") or []),
        "credentials": list(_get(state, "credentials") or []),
        "hosts": list((_get(state, "hosts") or {}).keys()),
        "coverage_executed": list(coverage.get("executed") or []),
        "coverage_untested": list(coverage.get("untested") or []),
        "coverage_stop_reason": coverage.get("stop_reason") or "",
        "checks_used": coverage.get("checks_used", 0),
        "hypotheses_total": len(ledger),
        "hypotheses_open": len(open_hypotheses),
        "hypotheses_confirmed": len([h for h in ledger if h.get("status") == "confirmed"]),
        "hypotheses_refuted": len([h for h in ledger if h.get("status") == "refuted"]),
        "top_hypotheses": open_hypotheses[:3],
        "denied": counters.get("denied", 0),
        "errors": counters.get("errors", 0),
        "redundant_rate": loop.get("redundant_action_rate", 0.0),
        "loop_advice": loop.get("last_advice", ""),
        "scheduler": _get(state, "scheduler") or {},
        "ledger": _get(state, "ledger") or {},
        "last_steps": steps[-3:],
        "artifacts": _get(state, "artifacts") or {},
    }


def render_board(state: Any, *, hypotheses: list[dict[str, Any]] | None = None, width: int = 76, color: bool = False) -> str:
    """渲染一屏工作台视图（纯文本；`color=True` 时给关键值加 ANSI 色）。"""
    data = summarise(state, hypotheses=hypotheses)
    def hl(text: str, code: str = "36") -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    status = data["status"] or "?"
    verdict = data["verdict"] or "-"
    line = "─" * width
    lines = [
        line,
        f" TRINA / WORKSPACE   {hl(data['run_id'], '1')}",
        f" 目标   {data['target'] or '(未指定)'}   阶段 {data['phase'] or '-'}   状态 {status}   结论 {verdict}",
        f" 耗时   {data['duration']}   步数 {data['steps']}   检查 {data['checks_used']}   被拒 {data['denied']}   错误 {data['errors']}",
        line,
        " 研究成果",
        f"   漏洞 {data['findings_total']}（已验证 {data['findings_verified']}）"
        + "｜"
        + " ".join(f"{SEVERITY_CN[sev]} {data['by_severity'].get(sev, 0)}" for sev in SEVERITY_ORDER),
        f"   flag {len(data['flags'])}   凭据 {len(data['credentials'])}   主机 {len(data['hosts'])}",
    ]
    ledger = data["ledger"]
    if ledger:
        lines.append(
            f"   模型调用 {ledger.get('calls', 0)} 次 · token 入 {ledger.get('input_tokens', 0)}/出 {ledger.get('output_tokens', 0)}"
            f" · 成本 ${ledger.get('cost_usd', 0)}"
        )
    lines.append(line)
    lines.append(
        f" 假设台账   待验证 {data['hypotheses_open']} · 已确认 {data['hypotheses_confirmed']} · "
        f"已否定 {data['hypotheses_refuted']}（共 {data['hypotheses_total']}）"
    )
    for item in data["top_hypotheses"]:
        statement = str(item.get("statement", ""))[:52]
        lines.append(f"   - {item.get('id')} P={item.get('confidence')} 优先级={item.get('priority')} {statement}")
    lines.append(
        f" 覆盖       已测 {len(data['coverage_executed'])} 项 · 未测 {len(data['coverage_untested'])} 项"
        + (f" · 停止原因 {data['coverage_stop_reason']}" if data["coverage_stop_reason"] else "")
    )
    if data["coverage_untested"]:
        preview = "、".join(str(item.get("test_id", item)) for item in data["coverage_untested"][:5])
        lines.append(f"   未测项（前 5）：{preview}")
    if data["redundant_rate"]:
        lines.append(f" 循环识别   无新信息重复比例 {data['redundant_rate']:.0%}" + (f" · {data['loop_advice'][:40]}" if data["loop_advice"] else ""))
    scheduler = data.get("scheduler") or {}
    if scheduler.get("decisions"):
        expected = scheduler.get("followed", 0) + scheduler.get("missed", 0)
        lines.append(
            f" 调度遵从   决策 {scheduler.get('decisions')} 次 · 按调度执行 {scheduler.get('followed', 0)}"
            + (f"/{expected}（遵从率 {float(scheduler.get('compliance_rate') or 0):.0%}）" if expected else "")
            + (f" · 未遵从 {scheduler.get('missed')}" if scheduler.get("missed") else "")
        )
        last = scheduler.get("last") or {}
        if last:
            lines.append(
                f"   - 最近调度：验证 {last.get('hypothesis_id')} → "
                + (f"{last.get('test_id')}（{last.get('tool')}）" if last.get("test_id") else f"自由验证 {last.get('vuln_class')}")
            )
    if data["last_steps"]:
        lines.append(line)
        lines.append(" 最近动作")
        for step in data["last_steps"]:
            if isinstance(step, dict) and step.get("kind") == "tool":
                mark = "OK" if step.get("ok") else "×"
                lines.append(f"   [{mark}] {step.get('tool')} — {str(step.get('summary', ''))[:52]}")
    if data["artifacts"].get("workspace"):
        lines.append(line)
        lines.append(f" 工作区 {data['artifacts']['workspace']}")
    lines.append(line)
    return "\n".join(lines)


def load_state(path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"读取失败：{type(exc).__name__}: {exc}"}


def load_run_state(path: str | Path) -> Any:
    """把 state.json 还原成 ``RunState``（失败返回 None）；控制台 / 复核用。"""
    payload = load_state(path)
    if payload.get("error"):
        return None
    try:
        from .state import RunState

        return RunState.from_dict(payload)
    except Exception:  # noqa: BLE001 - 视图层尽力而为
        return payload


def latest_state_file(workspace: str | Path) -> Path | None:
    """取最近一次运行的 state.json（trina watch 不带参数时用）。"""
    root = Path(workspace)
    candidates = sorted(root.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


__all__ = ["SEVERITY_CN", "SEVERITY_ORDER", "latest_state_file", "load_run_state", "load_state", "render_board", "summarise"]
