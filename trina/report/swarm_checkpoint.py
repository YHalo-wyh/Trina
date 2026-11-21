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
_PERFORMANCE_FIELDS = {
    "llm": ("模型请求", ("input_tokens", "output_tokens", "cache_read_tokens")),
    "tools": ("工具调用", ("failed",)),
    "subworkers": ("Worker", ("startup_seconds",)),
}


def _number(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            return None
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _duration(start: Any, end: Any) -> float | None:
    first, last = _number(start), _number(end)
    if first is not None and last is not None and 0 < first <= last:
        return round(last - first, 2)
    return None


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} 秒"


def _performance_metrics(data: dict[str, Any], wall_seconds: float | None) -> dict[str, dict[str, Any]]:
    """读取已有聚合值，保留缺失字段与实测零值的区别。"""
    metrics = {}
    for key, (_, extra_fields) in _PERFORMANCE_FIELDS.items():
        recorded = data.get(key)
        if not isinstance(recorded, dict):
            continue
        fields = {}
        for name in ("calls", "total_seconds", "p50_seconds", "p95_seconds", *extra_fields):
            value = recorded.get(name)
            if name.endswith("_seconds"):
                fields[name] = _number(value)
            else:
                fields[name] = value if type(value) is int and value >= 0 else None
        if any(value is not None for value in fields.values()):
            metrics[key] = fields
    workers = metrics.get("subworkers")
    if workers and wall_seconds and workers["total_seconds"] is not None:
        workers["average_concurrency"] = _number(workers["total_seconds"] / wall_seconds)
    return metrics


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = [" | ".join(headers), " | ".join(["---"] * len(headers))]
    for row in rows:
        cells = [re.sub(r"[\[\]|`~*_\\]", lambda match: f"&#{ord(match[0])};", escape(" ".join(cell.split())))
                 for cell in row]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _html_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    head = "".join(f"<th scope=\"col\">{escape(cell)}</th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


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
    metrics = _performance_metrics(performance, wall_seconds)

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
    markdown = f"# {title}\n\n{summary}\n\n{_markdown_table(headers, rows)}\n"
    html = (f"<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>{escape(title)}</title>"
            f"<h1>{escape(title)}</h1><p>{escape(summary)}</p>{_html_table(headers, rows)}")
    if metrics:
        stats_headers = ("阶段", "样本数", "累计耗时", "单次 p50", "单次 p95")
        stats_rows = [(_PERFORMANCE_FIELDS[key][0], str(item["calls"]) if item["calls"] is not None else "—",
                       _seconds(item["total_seconds"]), _seconds(item["p50_seconds"]), _seconds(item["p95_seconds"]))
                      for key, item in metrics.items()]
        notes = ["累计耗时包含并行重叠时间。"]
        workers = metrics.get("subworkers", {})
        concurrency = workers.get("average_concurrency")
        if concurrency is not None:
            notes.append(f"按已记录 Worker 累计耗时除以 Commander 总耗时估算，平均并发为 {concurrency:.2f}。")
        if workers.get("startup_seconds") is not None:
            notes.append(f"Worker 启动累计 {_seconds(workers['startup_seconds'])}。")
        llm = metrics.get("llm", {})
        for key, label in (("input_tokens", "输入"), ("output_tokens", "输出"), ("cache_read_tokens", "缓存读取")):
            if llm.get(key) is not None:
                notes.append(f"{label} token {llm[key]:,}。")
        failed = metrics.get("tools", {}).get("failed")
        if failed is not None:
            notes.append(f"工具调用失败 {failed} 次。")
        note = "".join(notes)
        markdown += f"\n## 运行性能\n\n{_markdown_table(stats_headers, stats_rows)}\n\n{note}\n"
        html += f"<h2>运行性能</h2>{_html_table(stats_headers, stats_rows)}<p>{escape(note)}</p>"
    data = {"run_id": run_id, "title": title, "generated_at": generated_at, "completed_tasks": complete,
            "commander_wall_seconds": wall_seconds, "tasks": tasks, "performance": metrics}
    return Report(title, generated_at, markdown, html + "</html>\n", data, default_dir=str(cfg.report_dir),
                  default_formats=cfg.report.formats)
