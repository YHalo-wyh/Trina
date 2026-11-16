"""Trina 生产 prompt_toolkit 适配器；固定终端 I/O，不调用模型。"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from trina.console_ui import EnhancedConsoleInput
from trina.terminal import render_answer, streaming_answer


def send(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def rss_mb() -> float:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = ctypes.c_void_p
    info = ctypes.windll.psapi.GetProcessMemoryInfo
    info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not info(current(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.WorkingSetSize / 1048576


async def main() -> None:
    initial = json.loads(sys.stdin.readline())
    columns, rows = initial["columns"], initial["rows"]
    capture = io.StringIO()
    output = Vt100_Output(capture, lambda: Size(rows=rows, columns=columns), enable_cpr=False)
    state = {"seq": 0, "start": 0, "cpu_base": time.process_time(), "ready": False}
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(status=lambda: "fixture", running=lambda: False, dynamic_values=lambda *_: (),
                                      plain=False, motion="none", activity=lambda: ("idle", "fixture", 0))
        for text in initial["history"]:
            editor.write(render_answer(text, color=True))
        messages: dict[str, str] = {}
        finished_thinking: set[str] = set()
        worker_indices: dict[str, int] = {}
        def before(_app):
            state["start"] = time.perf_counter()
        def after(_app):
            data = capture.getvalue()
            capture.seek(0); capture.truncate(0)
            send({"type": "paint", "seq": state["seq"], "render_ms": (time.perf_counter() - state["start"]) * 1000,
                  "cpu_ms": (time.process_time() - state["cpu_base"]) * 1000, "rss_mb": rss_mb(),
                  "ansi": base64.b64encode(data.encode()).decode()})
        editor.app.before_render += before
        editor.app.after_render += after
        def handle(row):
            state["seq"] = row.get("seq", 0)
            if row["type"] == "delta":
                key = row.get("worker", "main") + ":" + row["kind"]
                messages[key] = messages.get(key, "") + row["delta"]
                if row["kind"] == "thinking":
                    editor.update_thinking(key, messages[key])
                elif row.get("worker", "main") == "main":
                    thought_key = row.get("worker", "main") + ":thinking"
                    if thought_key in messages and thought_key not in finished_thinking:
                        editor.update_thinking(thought_key, messages[thought_key], final=True)
                        finished_thinking.add(thought_key)
                    editor.update_stream(messages[key])
                else:
                    # 多路活动正文使用同一生产渲染器，独立索引；不模拟任务调度。
                    value = messages[key]
                    card = streaming_answer(value, color=True)
                    editor.update_tool(key, card)
            elif row["type"] == "tool_start":
                if "main:text" in messages:
                    editor.update_stream(messages["main:text"], final=True)
                from trina.event_view import EventTranscript
                view = EventTranscript(editor.write, editor=editor, color=True)
                worker_indices[row["call"]] = view
                handle.tool_event(row, "tool.started", params={"argv": ["bash", "-lc", row["command"]]})
            elif row["type"] in {"tool_output", "tool_end"}:
                if row["type"] == "tool_end":
                    handle.tool_event(row, "tool.finished", status="completed", ok=True, duration=1.0,
                                      data={"stdout": row["output"], "exit_code": 0})
                else:
                    view = worker_indices[row["call"]]
                    previous = view.calls.get(row["call"], {}).get("output", "")
                    handle.tool_event(row, "tool.output", stream="stdout", delta=row["output"][len(previous):])
            elif row["type"] == "key":
                pipe.send_text(row["text"])
            elif row["type"] == "scroll":
                pipe.send_text("\x1b[5~" if row["direction"] == "up" else "\x1b[6~")
            elif row["type"] == "done":
                for key, value in messages.items():
                    if key.endswith(":thinking") and key not in finished_thinking:
                        editor.update_thinking(key, value, final=True)
                        finished_thinking.add(key)
                if "main:text" in messages and editor._stream_index is not None:
                    editor.update_stream(messages["main:text"], final=True)
            elif row["type"] == "finish":
                async def finish():
                    await asyncio.sleep(.3)
                    send({"type": "summary", "cpu_ms": (time.process_time() - state["cpu_base"]) * 1000, "rss_mb": rss_mb()})
                    editor.app.exit()
                asyncio.create_task(finish())
        def tool_event(row, kind, **payload):
            from trina.events import RuntimeEvent
            worker_indices[row["call"]].consume(RuntimeEvent(seq=row["seq"], stream_id="fixture", run_id="fixture", kind=kind,
                time=time.time(), source="fixture", task_id="", worker_id="", call_id=row["call"], payload={"tool": "terminal_exec", **payload}))
        handle.tool_event = tool_event
        loop = asyncio.get_running_loop()
        def read():
            for line in sys.stdin:
                row = json.loads(line)
                loop.call_soon_threadsafe(handle, row)
        threading.Thread(target=read, daemon=True).start()
        async def ready():
            await asyncio.sleep(.7)
            state["cpu_base"] = time.process_time()
            send({"type": "ready", "runtime": sys.version.split()[0]})
        asyncio.create_task(ready())
        await editor.app.run_async()


if __name__ == "__main__":
    asyncio.run(main())
