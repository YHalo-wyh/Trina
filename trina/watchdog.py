"""Swarm 进程级保活器。

Watchdog 只判断子进程是否留下了可解析的任务摘要，不参与模型决策、任务
排序或工具授权。子进程异常退出且没有正常摘要时，使用同一个检查点重新启动。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TextIO


TERMINAL = {"completed", "blocked", "failed", "skipped", "timed_out", "cancelled"}


def parse_summary(output: str) -> dict | None:
    """从子进程 stdout 尾部提取 ``--json`` Swarm 摘要。"""
    text = str(output or "").strip()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value = json.loads(text[index:])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("tasks"), dict):
            return value
    return None


def summary_is_terminal(summary: dict | None) -> bool:
    if not isinstance(summary, dict) or not isinstance(summary.get("tasks"), dict) or not summary["tasks"]:
        return False
    return all(str(item.get("status")) in TERMINAL for item in summary["tasks"].values()
               if isinstance(item, dict))


def run_watchdog(
    checkpoint: str | Path,
    *,
    max_restarts: int = 3,
    interval: float = 2.0,
    command_factory: Callable[[int], list[str]] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stream: TextIO | None = None,
) -> int:
    """运行并保活一个 Swarm 子进程，返回最后一次子进程退出码。"""
    checkpoint_path = Path(checkpoint).resolve()
    if not checkpoint_path.is_file():
        raise FileNotFoundError(checkpoint_path)
    if type(max_restarts) is not int or max_restarts < 0:
        raise ValueError("max_restarts 必须为非负整数")
    if interval < 0:
        raise ValueError("interval 必须非负")
    output_stream = stream or sys.stdout
    factory = command_factory or (lambda _attempt: [
        sys.executable, "-m", "trina", "swarm", "--resume", str(checkpoint_path), "--json",
    ])
    for attempt in range(max_restarts + 1):
        process = subprocess.Popen(
            factory(attempt), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
        )
        chunks: list[str] = []
        if process.stdout is not None:
            for line in process.stdout:
                chunks.append(line)
                output_stream.write(line)
                output_stream.flush()
            process.stdout.close()
        process.wait()
        output = "".join(chunks)
        if output and not chunks:
            output_stream.write(output)
            output_stream.flush()
        summary = parse_summary(output)
        if summary_is_terminal(summary) or attempt >= max_restarts:
            return int(process.returncode or 0)
        output_stream.write(f"[watchdog] 子进程异常退出，{interval:g}s 后恢复检查点（第 {attempt + 1} 次）\n")
        output_stream.flush()
        if interval:
            time.sleep(interval)
    return 1


__all__ = ["parse_summary", "summary_is_terminal", "run_watchdog"]
