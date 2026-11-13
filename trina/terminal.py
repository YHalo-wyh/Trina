"""终端展示组件：自适应宽度、字面量渲染和无颜色降级，不参与执行决策。"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import unicodedata
from typing import Any, Callable

PHASES = {"recon": "侦察", "enum": "枚举", "vuln": "验证", "exploit": "利用", "post": "后续", "report": "报告"}
STATUSES = {"running": "运行中", "chat": "对话", "success": "完成", "partial": "部分完成", "failed": "失败", "blocked": "已阻止", "stopped": "已停止"}
VERDICTS = {"CONFIRMED": "已确认", "NOT_CONFIRMED": "本轮未确认", "INCONCLUSIVE": "证据不足"}
_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
THEME = {"rose": "#edb2d2", "lilac": "#c7a5e8", "violet": "#9b7bc7", "muted": "#a595b3",
         "ink": "#1c1625", "selected": "#50355f", "text": "#ede5f2"}


class ResponsiveText(str):
    """保留渲染参数，让全屏终端改变宽度后能重新排版历史内容。"""

    def __new__(cls, value: str, renderer: Callable[[int], str] | None = None):
        instance = super().__new__(cls, value)
        instance._renderer = renderer
        return instance

    def at_width(self, width: int) -> str:
        return self._renderer(width) if self._renderer is not None else str(self)


def _responsive(value: str, renderer: Callable[[int], str], width: int | None) -> str:
    """显式指定宽度时保持普通字符串；自动宽度渲染时附带可重排函数。"""
    return value if width is not None else ResponsiveText(value, renderer)

def clean_text(value: Any) -> str:
    """目标、模型文本和工具输出按字面量显示，移除终端控制序列。"""
    text = _ANSI.sub("", str(value)).replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))


def terminal_width() -> int:
    return max(20, min(110, shutil.get_terminal_size(fallback=(88, 24)).columns))


def use_color(*, plain: bool = False) -> bool:
    return not plain and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb" and bool(getattr(sys.stdout, "isatty", lambda: False)())


def _wrap(text: str, width: int) -> str:
    """无 rich 时按显示单元折行，中文占两格，组合字符不额外占格。"""
    lines = []
    for line in clean_text(text).split("\n"):
        current, used = "", 0
        for ch in line:
            size = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
            if used + size > width and current:
                lines.append(current)
                current, used = "", 0
            current += ch
            used += size
        lines.append(current)
    return "\n".join(lines)


def _center(text: str, width: int) -> str:
    lines = []
    for line in _wrap(text, width).split("\n"):
        cells = sum(0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1) for ch in line)
        lines.append(" " * max(0, (width - cells) // 2) + line)
    return "\n".join(lines)


def render_panel(title: str, *, rows: list[tuple[str, Any]] | None = None, sections: list[tuple[str, str]] | None = None,
                 footer: str = "", width: int | None = None, color: bool = False, accent: str = THEME["lilac"]) -> str:
    """返回完整卡片；所有动态值用 Text，禁止作为 rich markup 执行。"""
    requested_width = width
    original_title, original_rows, original_sections, original_footer = title, rows, sections, footer
    width = max(20, width if width is not None else terminal_width())
    title, footer = clean_text(title), clean_text(footer)
    rows = [(clean_text(k), clean_text(v)) for k, v in rows or []]
    sections = [(clean_text(k), clean_text(v)) for k, v in sections or []]
    try:
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich import box
    except ImportError:
        lines = [title, "─" * min(width, 36)]
        lines.extend(f"{k}  {v}" for k, v in rows)
        for heading, body in sections:
            lines.extend((["", heading] if heading else [""]) + [body])
        if footer:
            lines.extend(["", footer])
        value = _wrap("\n".join(lines), width)
        return _responsive(value, lambda new_width: str(render_panel(
            original_title, rows=original_rows, sections=original_sections, footer=original_footer,
            width=new_width, color=color, accent=accent)), requested_width)
    parts = []
    if rows:
        if width < 48:
            for key, value in rows:
                parts.extend([Text(key, style="dim"), Text(value, overflow="fold")])
        else:
            table = Table.grid(padding=(0, 2), expand=True)
            table.add_column(style="dim", width=10)
            table.add_column(ratio=1, overflow="fold")
            for key, value in rows:
                table.add_row(Text(key), Text(value, overflow="fold"))
            parts.append(table)
    for heading, body in sections:
        if parts:
            parts.append(Text(""))
        if heading:
            parts.append(Text(heading, style=f"bold {accent}"))
        parts.append(Text(body, overflow="fold"))
    if footer:
        parts.extend([Text(""), Text(footer, style="dim", overflow="fold")])
    output = io.StringIO()
    console = Console(file=output, width=width, height=25, force_terminal=color, color_system="truecolor" if color else None,
                      no_color=not color, legacy_windows=False, highlight=False)
    console.print(Panel(Group(*parts), title=Text(title, style=f"bold {accent}"), title_align="left",
                        border_style=accent, box=box.ROUNDED, padding=(1, 2) if width >= 48 else (0, 1)))
    value = output.getvalue().rstrip("\n")
    return _responsive(value, lambda new_width: str(render_panel(
        original_title, rows=original_rows, sections=original_sections, footer=original_footer,
        width=new_width, color=color, accent=accent)), requested_width)


def render_message(message: str, *, label: str = "提示", width: int | None = None, color: bool = False) -> str:
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    text = f"{clean_text(label)}  {clean_text(message)}"
    try:
        from rich.console import Console
        from rich.text import Text
    except ImportError:
        value = _wrap(text, width)
        return _responsive(value, lambda new_width: str(render_message(
            message, label=label, width=new_width, color=color)), requested_width)
    stream = io.StringIO()
    content = Text(text, overflow="fold")
    content.stylize("bold " + THEME["rose"] if label.strip("[] ") not in ("错误", "拒绝") else "bold red", 0, len(clean_text(label)))
    Console(file=stream, width=width, height=25, force_terminal=color, color_system="truecolor" if color else None,
            no_color=not color, legacy_windows=False, highlight=False).print(content)
    value = stream.getvalue().rstrip("\n")
    return _responsive(value, lambda new_width: str(render_message(
        message, label=label, width=new_width, color=color)), requested_width)


def render_process(message: str, *, label: str = "过程", width: int | None = None, color: bool = False) -> str:
    """普通进展直接写入内容区，不套工具窗口。"""
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    text = _wrap(f"{clean_text(label)}  {clean_text(message)}", width)
    if color:
        rgb = ";".join(str(int(THEME["muted"][index:index + 2], 16)) for index in (1, 3, 5))
        value = f"\x1b[38;2;{rgb}m" + text + "\x1b[0m"
    else:
        value = text
    return _responsive(value, lambda new_width: str(render_process(
        message, label=label, width=new_width, color=color)), requested_width)


def render_answer(message: str, *, width: int | None = None, color: bool = False) -> str:
    """正式输出渲染 Markdown，清除终端控制字符，不附会话统计。"""
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    try:
        from rich.console import Console
        from rich.markdown import Markdown
    except ImportError:
        value = _wrap(message, width)
        return _responsive(value, lambda new_width: str(render_answer(
            message, width=new_width, color=color)), requested_width)
    stream = io.StringIO()
    console = Console(file=stream, width=width, force_terminal=color, no_color=not color,
                      color_system="truecolor" if color else None, legacy_windows=False)
    console.print(Markdown(clean_text(message), hyperlinks=False))
    value = stream.getvalue().rstrip("\n")
    return _responsive(value, lambda new_width: str(render_answer(
        message, width=new_width, color=color)), requested_width)


def render_tool_call(tool: str, params: dict[str, Any], *, status: str = "running", output: str = "",
                     stderr: str = "", error: str = "", duration: float = 0, command: str = "",
                     exit_code: int | None = None, width: int | None = None, color: bool = False) -> str:
    """工具输入、输出和状态共用一个窗口，脚本与 JSON 按字面量高亮。"""
    import json
    import shlex
    import subprocess
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    original = dict(tool=tool, params=params, status=status, output=output, stderr=stderr, error=error,
                    duration=duration, command=command, exit_code=exit_code, color=color)
    label = {"requested": "准备", "awaiting_approval": "待审批", "running": "运行中", "completed": "完成", "failed": "失败",
             "denied": "已拒绝", "invalid": "参数错误", "unavailable": "不可用",
             "timed_out": "超时", "interrupted": "已中断", "cancelled": "已取消"}.get(status, status)
    argv = params.get("argv")
    script = ("$ " + (subprocess.list2cmdline([str(item) for item in argv]) if os.name == "nt" else
                     shlex.join([str(item) for item in argv]))) if isinstance(argv, list) else ""
    script = script or command or json.dumps(params, ensure_ascii=False, indent=2)
    title = f"{clean_text(tool)} · {label}"
    footer = f"{duration:.1f}s" + (f" · exit {exit_code}" if exit_code is not None else "")
    try:
        from rich import box
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.syntax import Syntax
        from rich.text import Text
    except ImportError:
        value = render_panel(title, sections=[("Input", script), ("Output", output or "（暂无输出）"),
                             *([("stderr", stderr)] if stderr else []), *([("错误", error)] if error else [])],
                             footer=footer, width=width, color=False)
    else:
        parts = [Syntax(clean_text(script), "bash" if script.startswith("$ ") or command else "json",
                        theme="monokai", background_color="default", word_wrap=True),
                 Rule("Output", style="dim", align="left"), Text(clean_text(output or "（暂无输出）"))]
        if stderr:
            parts.extend([Text("stderr", style="yellow"), Text(clean_text(stderr))])
        if error:
            parts.append(Text(clean_text(error), style="red"))
        stream = io.StringIO()
        Console(file=stream, width=width, force_terminal=color, no_color=not color,
                color_system="truecolor" if color else None, legacy_windows=False).print(
                    Panel(Group(*parts), title=Text(title), title_align="left", subtitle=Text(footer),
                          border_style="red" if error else THEME["muted"], box=box.ROUNDED, padding=(0, 1)))
        value = stream.getvalue().rstrip("\n")
    return _responsive(str(value), lambda new_width: str(render_tool_call(**original, width=new_width)), requested_width)


def render_home(*, width: int | None = None, color: bool = False) -> str:
    """首页仅显示终端字符字标与版本。"""
    from . import __version__
    from .banner import render_banner

    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    logo = render_banner(width, color=color, colors=(THEME["rose"], THEME["violet"]))
    value = logo + "\n" + _center(f"v{__version__}", width) + "\n"
    return _responsive(value, lambda new_width: str(render_home(
        width=new_width, color=color)), requested_width)


def show_home(*, output, color: bool) -> None:
    """字符输出不依赖终端图片能力，也不探测标准输入。"""
    output(render_home(color=color))


def render_step(step: Any, *, width: int | None = None, color: bool = False) -> str:
    status = "拒绝" if step.denied else ("完成" if step.ok else "失败")
    phase = PHASES.get(step.phase, step.phase)
    seconds = float(getattr(step, "duration", 0) or 0) + float(getattr(step, "llm_seconds", 0) or 0)
    return render_message(f"{phase} · {step.action} · {seconds:.1f}s\n    {step.summary or getattr(step, 'error', '') or '无摘要'}",
                          label=f"{step.index:02d} {status}", width=width, color=color)


def render_approval(request: dict[str, Any], timeout: float | None, *, width: int | None = None, color: bool = False) -> str:
    import json
    from .audit import redact
    footer = (f"输入 y 允许 / n 拒绝 · {timeout:g} 秒未答复按拒绝处理。"
              if timeout is not None else "输入 y 允许 / n 拒绝；直接回车按拒绝处理。")
    return render_panel("待审批", rows=[("动作", request.get("tool", "")), ("等级", request.get("level", "")),
                        ("检查项", request.get("test_id") or "未指定"),
                        ("参数", json.dumps(redact(request.get("params") or {}), ensure_ascii=False))],
                        sections=[("理由", str(request.get("thought") or "未提供"))],
                        footer=footer,
                        width=width, color=color, accent="yellow")
