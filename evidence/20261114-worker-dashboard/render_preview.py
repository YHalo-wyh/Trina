"""真实 prompt_toolkit 屏幕缓冲预览；任务内容全部是 UI 演示数据。"""
from __future__ import annotations

import importlib.util
import io
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
helper_path = ROOT / "evidence/20261114-subworkers/render_preview.py"
spec = importlib.util.spec_from_file_location("preview_helpers", helper_path)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
preview.OUT = Path(__file__).resolve().parent
preview.COLUMNS, preview.ROWS = 144, 36

from prompt_toolkit import Application
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from trina.config import TrinaConfig
from trina.console_state import ConsoleContext, ConsoleState
from trina.console_ui import EnhancedConsoleInput
from trina.llm.base import LLMUsage
from trina.subworkers import Subworkers
from trina.subworker_ui import render_grid, show_subworkers


def main():
    manager = Subworkers(lambda _: None)
    for index, (name, goal, model, output) in enumerate((
        ("WebSurface", "梳理接口与请求参数", "deepseek-chat", "GET /health → 200\n已记录响应证据，继续验证鉴权。"),
        ("BinaryTrace", "分析附件并验证候选", "gpt-5.2", "ELF: PIE enabled\n已定位输入处理函数，验证地址偏移。"),
        ("CodeTests", "实现最小修复并回归", "gpt-5.2", "python -m unittest tests.test_worker_dashboard\n6 tests · OK"),
        ("EvidenceReview", "交叉核对已收集的结果", "deepseek-chat", "已核对来源和任务 ID。\n当前结论保留验证边界，等待其余分支。"),
    )):
        manager._rows[name] = {"id": name, "goal": goal, "model": model, "reasoning_effort": "high", "status": "running", "calls": 6, "tools": 9, "tokens": 24000}
        manager._record(name, "task", "任务", goal)
        manager._record(name, "reply", "回复", "开始独立分支，保留可复现证据。")
        manager._record(name, "call", "工具", "terminal_exec · 有界命令执行")
        manager._record(name, "out", "输出", output)
    manager._badge = "4 项观察 · UI 演示"
    manager.usage.model_finished(LLMUsage(input_tokens=180000, output_tokens=8000, cache_read_tokens=144000))
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=36, columns=144), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        with patch("prompt_toolkit.Application", side_effect=lambda *args, **kwargs: preview.hook(Application(*args, **kwargs), "worker-grid")):
            show_subworkers(manager, TrinaConfig())
    context = ConsoleContext("就绪", "DeepSeek Flash Max", "C:/work/Trina", "四格 Worker 协作", "轮次 8", 180000, 500000,
                             branch="main", metrics=manager.usage.metrics(), workers="4 运行")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(status=lambda: "就绪", running=lambda: False, dynamic_values=lambda *_: (), context=lambda: context)
        editor.show_home()
        editor.buffer.text, editor.buffer.cursor_position = "/", 1
        preview.hook(editor.app, "console-clean-footer")
        editor.run(lambda _: True)
    samples = []
    for _ in range(500):
        start = time.perf_counter()
        render_grid(manager, 144, 29, 0)
        samples.append((time.perf_counter() - start) * 1000)
    stress = Subworkers(lambda _: None)
    for i in range(80):
        stress._record("stress", str(i), "输出", "x" * 4000)
    stress.dialog_lines("stress", 70)
    stress._record("stress", "79", "输出", "y" * 4000)
    start = time.perf_counter()
    stress.dialog_lines("stress", 70)
    changed_ms = (time.perf_counter() - start) * 1000
    (preview.OUT / "grid-performance.json").write_text(json.dumps({"scope": "本地已缓存四格布局函数，不含网络或实际终端绘制", "iterations": 500, "median_ms": statistics.median(samples), "p99_ms": sorted(samples)[494], "large_dialog_changed_one_block_ms": changed_ms}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
