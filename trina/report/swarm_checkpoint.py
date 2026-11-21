"""从磁盘上的 Swarm 检查点生成任务状态与耗时摘要。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from ..config import TrinaConfig
from .builder import Report


_STATUS = {
    "completed": "已完成", "running": "运行中", "pending": "待执行",
    "failed": "失败", "cancelled": "已取消", "skipped": "跳过",
    "blocked": "受阻", "timed_out": "超时",
}


def _number(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            return None
        if math.isfinite(number):
            return number
    return None


def _duration(start: Any, end: Any) -> float | None:
    first, last = _number(start), _number(end)
    if first is not None and last is not None and 0 < first <= last:
        return round(last - first, 2)
    return None


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} 秒"


def build_swarm_checkpoint_report(path: Path, checkpoint: dict[str, Any], cfg: TrinaConfig) -> Report:
    """只读汇总检查点中已保存的任务和可选 Commander 性能文件。"""
    run_id = str(checkpoint.get("run_id") or "swarm")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("Swarm 检查点的运行 ID 无效")

    try:
        performance = json.loads(path.with_name("commander-performance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        performance = {}
    if not isinstance(performance, dict):
        performance = {}
    origin = performance.get("started_at")
    wall_seconds = _number(performance.get("wall_time_seconds"))

    coordination = checkpoint.get("coordination") or {}
    if not isinstance(coordination, dict):
        raise ValueError("Swarm 检查点的协调状态无效")
    records = coordination.get("intents", checkpoint.get("records", {}))
    if not isinstance(records, dict):
        raise ValueError("Swarm 检查点的任务状态无效")
    tasks = []
    for task in checkpoint["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise ValueError("Swarm 检查点包含无效的任务记录")
        task_id = task["id"]
        record = records.get(task_id, {})
        if not isinstance(record, dict):
            raise ValueError("Swarm 检查点包含无效的任务状态")
        tasks.append({
            "id": task_id,
            "status": str(record.get("status") or "unknown"),
            "duration_seconds": _duration(record.get("started_at"), record.get("ended_at")),
            "ended_at_seconds": _duration(origin, record.get("ended_at")),
        })

    title = f"Swarm 运行摘要 · {run_id}"
    generated_at = datetime.now(timezone.utc).isoformat()
    complete = sum(task["status"] == "completed" for task in tasks)
    summary = f"已完成 {complete}/{len(tasks)} 个任务，Commander 总耗时 {_seconds(wall_seconds)}。"
    headers = ("任务", "快照状态", "执行耗时", "记录结束时的 Commander 耗时")
    rows = [(str(task["id"]), _STATUS.get(task["status"], "状态未知"),
             _seconds(task["duration_seconds"]), _seconds(task["ended_at_seconds"])) for task in tasks]
    markdown_lines = [f"# {title}", "", summary, "", " | ".join(headers), " | ".join(["---"] * len(headers))]
    for task_id, *cells in rows:
        label = escape(" ".join(task_id.split())).replace("|", "&#124;")
        markdown_lines.append(" | ".join([f"<code>{label}</code>", *cells]))
    markdown = "\n".join([*markdown_lines, ""])
    head = "".join(f"<th scope=\"col\">{escape(cell)}</th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    html = (f"<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>{escape(title)}</title>"
            f"<h1>{escape(title)}</h1><p>{escape(summary)}</p>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></html>\n")
    data = {"run_id": run_id, "title": title, "generated_at": generated_at, "completed_tasks": complete,
            "commander_wall_seconds": wall_seconds, "tasks": tasks}
    return Report(title, generated_at, markdown, html, data, default_dir=str(cfg.report_dir),
                  default_formats=cfg.report.formats)
