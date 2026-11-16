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
from .text import clean_text
THEME = {"rose": "#edb2d2", "lilac": "#c7a5e8", "violet": "#9b7bc7", "muted": "#a595b3",
         "ink": "#1c1625", "selected": "#50355f", "text": "#ede5f2"}


class ResponsiveText(str):
    """保留渲染参数，让全屏终端改变宽度后能重新排版历史内容。"""

    def __new__(cls, value: str, renderer: Callable[[int], str] | None = None,
                fragments_renderer: Callable[[int], list[tuple[str, str]]] | None = None):
        instance = super().__new__(cls, value)
        instance._renderer = renderer
        instance._fragments_renderer = fragments_renderer
        return instance

    def at_width(self, width: int) -> str:
        return self._renderer(width) if self._renderer is not None else str(self)

    def fragments_at_width(self, width: int):
        return self._fragments_renderer(width) if self._fragments_renderer is not None else None


def _responsive(value: str, renderer: Callable[[int], str], width: int | None) -> str:
    """显式指定宽度时保持普通字符串；自动宽度渲染时附带可重排函数。"""
    return value if width is not None else ResponsiveText(value, renderer)

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
    """正式输出使用流式 Markdown 排版，清除控制字符，不附会话统计。"""
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    try:
        from .markdown_view import fragments_to_ansi, render_markdown_fragments, wrap_fragments
    except ImportError:
        value = _wrap(message, width)
        return _responsive(value, lambda new_width: str(render_answer(
            message, width=new_width, color=color)), requested_width)
    fragments = render_markdown_fragments(message, color=color)
    wrapped = wrap_fragments(fragments, width)
    value = fragments_to_ansi(wrapped) if color else "".join(text for _, text in wrapped)
    if requested_width is not None:
        return value
    return ResponsiveText(value,
                          lambda new_width: str(render_answer(message, width=new_width, color=color)),
                          lambda _new_width: render_markdown_fragments(message, color=color))


def streaming_answer(message: str, *, color: bool = False) -> ResponsiveText:
    """流式正文的延迟排版对象；未绘制的 delta 不产生 ANSI 或语法高亮。"""
    from .markdown_view import render_markdown_fragments
    return ResponsiveText(message, lambda width: str(render_answer(message or " ", width=width, color=color)),
                          lambda _width: render_markdown_fragments(message or " ", color=color))


def render_thinking(message: str, *, final: bool = False, width: int | None = None, color: bool = False) -> str:
    """供应商返回的思考单独显示，轻量排版，不走 Markdown/代码高亮。"""
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    heading = "◇ 思考" if final else "◌ 思考中"
    value = heading + "\n" + "\n".join("  " + line for line in _wrap(clean_text(message), max(1, width - 2)).splitlines())
    if color:
        value = "\x1b[38;2;160;143;177m" + value + "\x1b[0m"
    return _responsive(value, lambda new_width: str(render_thinking(
        message, final=final, width=new_width, color=color)), requested_width)


def render_tool_call(tool: str, params: dict[str, Any], *, status: str = "running", output: str = "",
                     stderr: str = "", error: str = "", duration: float = 0, command: str = "",
                     exit_code: int | None = None, width: int | None = None, color: bool = False) -> str:
    """工具输入、输出和状态共用一个窗口，脚本与 JSON 按字面量高亮。"""
    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    original = dict(tool=tool, params=params, status=status, output=output, stderr=stderr, error=error,
                    duration=duration, command=command, exit_code=exit_code, color=color)
    fragments = render_tool_call_fragments(**original, width=width)
    if color:
        from .markdown_view import fragments_to_ansi
        value = fragments_to_ansi(fragments)
    else:
        value = "".join(text for _, text in fragments)
    if requested_width is not None:
        return value
    return ResponsiveText(value, lambda new_width: str(render_tool_call(**original, width=new_width)),
                          lambda new_width: render_tool_call_fragments(**original, width=new_width))


def render_tool_call_fragments(tool: str, params: dict[str, Any], *, status: str = "running", output: str = "",
                               stderr: str = "", error: str = "", duration: float = 0, command: str = "",
                               exit_code: int | None = None, width: int = 88,
                               color: bool = False) -> list[tuple[str, str]]:
    """工具流的延迟排版入口，供全屏 transcript 直接消费。"""
    import json
    import shlex
    import subprocess
    width = max(20, width)
    label = {"requested": "准备", "awaiting_approval": "待审批", "running": "运行中", "completed": "完成",
             "failed": "失败", "denied": "已拒绝", "invalid": "参数错误", "unavailable": "不可用",
             "timed_out": "超时", "interrupted": "已中断", "cancelled": "已取消"}.get(status, status)
    argv = params.get("argv")
    script = ("$ " + (subprocess.list2cmdline([str(item) for item in argv]) if os.name == "nt" else
                     shlex.join([str(item) for item in argv]))) if isinstance(argv, list) else ""
    script = script or command or json.dumps(params, ensure_ascii=False, indent=2)
    title = f"{clean_text(tool)} · {label}"
    footer = f"{duration:.1f}s" + (f" · exit {exit_code}" if exit_code is not None else "")
    return _render_tool_box_fragments(title, script, output or "（暂无输出）", stderr, error, footer,
                                      width=width, color=color)


def _cells(text: str) -> int:
    """统计终端显示单元，工具边框不依赖 Rich 的逐帧布局。"""
    if text.isascii():
        return len(text)
    return sum(0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
               for ch in text)


def _wrap_clean_line(text: str, width: int) -> list[str]:
    """对已清洗的单行做硬折行；ASCII 工具日志直接切片。"""
    if not text:
        return [""]
    if text.isascii():
        return [text[index:index + width] for index in range(0, len(text), width)]
    lines, current, used = [], "", 0
    for char in text:
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if used + size > width and current:
            lines.append(current)
            current, used = "", 0
        current += char
        used += size
    lines.append(current)
    return lines


def _render_tool_box_fragments(title: str, script: str, output: str, stderr: str, error: str, footer: str, *,
                               width: int, color: bool) -> list[tuple[str, str]]:
    """线性绘制工具窗口；输出增量只进行文本折行和补齐。"""
    inner = max(1, width - 4)
    border_style = ("#dc6478" if error else "#a595b3") if color else ""
    input_style = "#c7a5e8" if color else ""
    stderr_style = "#ebbe5a" if color else ""
    error_style = "#f06478" if color else ""
    lines: list[list[tuple[str, str]]] = []

    def row(text: str, style: str = "") -> list[tuple[str, str]]:
        pad = " " * max(0, inner - _cells(text))
        return [(border_style, "│"), ("", " "), (style, text), ("", pad + " "), (border_style, "│")]

    def ruled(label: str) -> list[tuple[str, str]]:
        lead = f" {label} "
        return row(lead + "─" * max(0, inner - _cells(lead)))

    safe_title, safe_script = clean_text(title), clean_text(script)
    safe_output, safe_stderr, safe_error = clean_text(output), clean_text(stderr), clean_text(error)
    title_lines = _wrap_clean_line(safe_title, max(1, width - 5))
    top_lead = f"╭─ {title_lines[0]} "
    lines.append([(border_style, top_lead + "─" * max(0, width - _cells(top_lead) - 1) + "╮")])
    for title_tail in title_lines[1:]:
        for wrapped in _wrap_clean_line(title_tail, inner):
            lines.append(row(wrapped, border_style))
    for source_line in safe_script.splitlines() or [""]:
        for wrapped in _wrap_clean_line(source_line, inner):
            lines.append(row(wrapped, input_style))
    lines.append(ruled("Output"))
    for source_line in safe_output.splitlines() or [""]:
        for wrapped in _wrap_clean_line(source_line, inner):
            lines.append(row(wrapped))
    if stderr:
        lines.append(ruled("stderr"))
        for source_line in safe_stderr.splitlines():
            for wrapped in _wrap_clean_line(source_line, inner):
                lines.append(row(wrapped, stderr_style))
    if error:
        for source_line in safe_error.splitlines():
            for wrapped in _wrap_clean_line(source_line, inner):
                lines.append(row(wrapped, error_style))
    safe_footer = clean_text(footer)
    if _cells(safe_footer) > width - 4:
        for wrapped in _wrap_clean_line(safe_footer, inner):
            lines.append(row(wrapped, border_style))
        safe_footer = ""
    bottom_lead = "╰" + "─" * max(0, width - _cells(safe_footer) - (4 if safe_footer else 2))
    lines.append([(border_style, bottom_lead + (f" {safe_footer} " if safe_footer else "") + "╯")])
    fragments: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        for style, text in line:
            if text:
                if fragments and fragments[-1][0] == style:
                    fragments[-1] = style, fragments[-1][1] + text
                else:
                    fragments.append((style, text))
        if index < len(lines) - 1:
            fragments.append(("", "\n"))
    return fragments


def render_home(*, width: int | None = None, color: bool = False, phase: float = 0) -> str:
    """首页仅显示终端字符字标与版本。"""
    from . import __version__
    from .banner import render_banner, render_version

    requested_width = width
    width = max(20, width if width is not None else terminal_width())
    logo = render_banner(width, color=color, colors=(THEME["rose"], THEME["violet"]), phase=phase)
    value = logo + "\n" + render_version(__version__, width, color=color, phase=phase) + "\n"
    return _responsive(value, lambda new_width: str(render_home(
        width=new_width, color=color, phase=phase)), requested_width)


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
