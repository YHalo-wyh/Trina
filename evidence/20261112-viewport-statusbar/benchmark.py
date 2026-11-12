"""对比既有提交与当前工作区的视口计算路径；不测终端端到端延迟。"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from trina.console_ui import EnhancedConsoleInput
from trina.terminal import ResponsiveText

BASELINE = "b285218de597b6220223885e3c9577d90712360a"


def measure(editor_type):
    with create_pipe_input() as input_pipe, create_app_session(input=input_pipe, output=DummyOutput()):
        editor = editor_type(status=lambda: "ready", running=lambda: False,
                             dynamic_values=lambda *_: (), plain=True)
        renders = []
        for index in range(100):
            editor.write(ResponsiveText(f"block {index}", lambda width, index=index:
                         renders.append(index) or (f"block {index}\n" * 20)))
        editor._transcript()
        rows = editor._rendered_lines
        started = time.perf_counter()
        for _ in range(1000):
            editor._scroll_by(1)
            editor._cursor_line()
        scroll_ms = (time.perf_counter() - started) * 1000
        renders.clear()
        started = time.perf_counter()
        for index in range(20):
            editor.update_stream("answer " * (index + 1))
            editor._transcript()
        stream_ms = (time.perf_counter() - started) * 1000
        return {"logical_rows": rows, "scroll_1000_ms": round(scroll_ms, 3),
                "stream_20_ms": round(stream_ms, 3),
                "history_block_rerenders_during_stream": len(renders)}


if __name__ == "__main__":
    source = subprocess.check_output(["git", "show", f"{BASELINE}:trina/console_ui.py"], cwd=ROOT).decode("utf-8")
    legacy = types.ModuleType("trina._viewport_baseline")
    legacy.__package__ = "trina"
    sys.modules[legacy.__name__] = legacy
    exec(compile(source, f"{BASELINE}:trina/console_ui.py", "exec"), legacy.__dict__)
    print(json.dumps({"baseline": BASELINE, "python": platform.python_version(),
                      "platform": platform.system(), "before": measure(legacy.EnhancedConsoleInput),
                      "after": measure(EnhancedConsoleInput)}, ensure_ascii=False, indent=2))
