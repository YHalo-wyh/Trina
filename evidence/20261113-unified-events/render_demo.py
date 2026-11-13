"""用真实注册工具生成正文/工具窗口预览，并核对事件重放的显示结果。"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.console_ui import EnhancedConsoleInput
from trina.events import EventBus
from trina.event_view import EventTranscript
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools.registry import ToolContext, ToolRegistry
from trina.tools.workspace import TerminalExec


def main() -> None:
    """正文为演示文本，工具输出来自真实 Python 进程，不调用付费模型。"""
    destination = Path(__file__).resolve().parent
    run_id = f"event-demo-{time.time_ns()}"
    workspace = ROOT / "runs" / run_id
    config = TrinaConfig().with_overrides(run__workspace=str(ROOT / "runs"), audit__dir=str(ROOT / "logs"))
    audit = AuditLog.open(config.audit_dir, run_id=run_id)
    guard = ScopeGuard(config.scope, audit=audit)
    bus = EventBus(workspace / "events.sqlite3", run_id)
    ctx = ToolContext(config=config, guard=guard, audit=audit, state=RunState(run_id, "127.0.0.1"),
                      http=None, workspace=workspace, event_bus=bus)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: False,
                                      dynamic_values=lambda *_: (), plain=False)
        editor.buffer.text = "继续检查工具执行结果"
        view = EventTranscript(editor.write, editor=editor, color=True)
        subscription = bus.subscribe(view.consume)
        bus.publish("assistant.message", message_id="before", final=True,
                    text="先检查命令的标准输出和错误输出。工具的输入与执行结果会显示在下面的窗口中。")
        result = ToolRegistry([TerminalExec()]).execute("terminal_exec", {
            "argv": [sys.executable, "-u", "-c",
                     "import sys,time; print('event stream connected'); time.sleep(.15); print('stderr channel ready', file=sys.stderr); print('command completed')"],
            "timeout": 5, "cwd": "run"}, ctx)
        assert result.ok, result.error
        bus.publish("assistant.message", message_id="after", final=True,
                    text="检查完成。命令正常退出，stdout 和 stderr 均已进入事件流；正文与工具窗口按执行顺序排列。")
        subscription.close()
        live = "".join(chunk.at_width(108) for chunk in editor._chunks)
        editor.clear_transcript()
        replay = EventTranscript(editor.write, editor=editor, color=True)
        for event in bus.replay():
            replay.consume(event)
        restored = "".join(chunk.at_width(108) for chunk in editor._chunks)
        assert restored == live
        assert editor.buffer.text == "继续检查工具执行结果"
        assert len(editor._chunks) == 3
    ansi = Console(file=io.StringIO(), width=108, record=True, force_terminal=True, color_system="truecolor")
    ansi.print(Text.from_ansi(live), end="")
    theme = TerminalTheme((16, 16, 18), (216, 216, 220),
                          [(0, 0, 0), (220, 70, 80), (70, 220, 140), (220, 200, 110),
                           (95, 150, 230), (190, 120, 220), (100, 210, 220), (216, 216, 220)])
    html_format = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Trina 工具窗口预览</title>
<style>body{{background:#101012;color:#d8d8dc;margin:0;padding:28px}}
pre{{font:15px/1.55 Consolas,'Microsoft YaHei',monospace;margin:0;white-space:pre}}
{stylesheet}</style></head><body><pre>{code}</pre></body></html>"""
    (destination / "tool-window-preview.html").write_text(
        ansi.export_html(theme=theme, code_format=html_format, clear=False), encoding="utf-8")
    (destination / "tool-window-preview.txt").write_text(ansi.export_text(clear=False), encoding="utf-8")
    (destination / "demo-events.jsonl").write_text(
        "\n".join(json.dumps(event.to_dict(), ensure_ascii=False) for event in bus.replay()) + "\n", encoding="utf-8")
    checks = {"ok": True, "run_id": run_id, "event_count": bus.last_seq,
              "events_path": str(bus.path), "audit_path": str(audit.path),
              "audit_verified": audit.verify()[0], "live_equals_replay": live == restored,
              "transcript_blocks": 3, "draft_preserved": True,
              "prose_source": "固定演示文本", "tool_source": "ToolRegistry 执行真实 Python 子进程"}
    (destination / "demo-result.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
