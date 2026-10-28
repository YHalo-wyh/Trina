"""终端展示组件：自适应宽度、字面量渲染和无颜色降级，不参与执行决策。"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import unicodedata
from typing import Any

PHASES = {"recon": "侦察", "enum": "枚举", "vuln": "验证", "exploit": "利用", "post": "后续", "report": "报告"}
STATUSES = {"running": "运行中", "chat": "对话", "success": "完成", "partial": "部分完成", "failed": "失败", "blocked": "已阻止", "stopped": "已停止"}
VERDICTS = {"CONFIRMED": "已确认", "NOT_CONFIRMED": "本轮未确认", "INCONCLUSIVE": "证据不足"}
_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


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


def render_panel(title: str, *, rows: list[tuple[str, Any]] | None = None, sections: list[tuple[str, str]] | None = None,
                 footer: str = "", width: int | None = None, color: bool = False, accent: str = "cyan") -> str:
    """返回完整卡片；所有动态值用 Text，禁止作为 rich markup 执行。"""
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
        return _wrap("\n".join(lines), width)
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
    console = Console(file=output, width=width, height=25, force_terminal=color, color_system="standard" if color else None,
                      no_color=not color, legacy_windows=False, highlight=False)
    console.print(Panel(Group(*parts), title=Text(title, style=f"bold {accent}"), title_align="left",
                        border_style=accent, box=box.ROUNDED, padding=(1, 2) if width >= 48 else (0, 1)))
    return output.getvalue().rstrip("\n")


def render_message(message: str, *, label: str = "提示", width: int | None = None, color: bool = False) -> str:
    width = max(20, width if width is not None else terminal_width())
    text = f"{clean_text(label)}  {clean_text(message)}"
    try:
        from rich.console import Console
        from rich.text import Text
    except ImportError:
        return _wrap(text, width)
    stream = io.StringIO()
    content = Text(text, overflow="fold")
    content.stylize("bold cyan" if label.strip("[] ") not in ("错误", "拒绝") else "bold red", 0, len(clean_text(label)))
    Console(file=stream, width=width, height=25, force_terminal=color, color_system="standard" if color else None,
            no_color=not color, legacy_windows=False, highlight=False).print(content)
    return stream.getvalue().rstrip("\n")


def render_home(config: Any, approval_mode: str, *, width: int | None = None, color: bool = False) -> str:
    from . import __version__
    actual_width = width if width is not None else terminal_width()
    shortcuts = ("/play       开始任务      /board     结果与证据\n/model      切换模型      /help      全部命令"
                 if actual_width >= 76 else "/play    开始任务\n/board   结果与证据\n/model   切换模型\n/help    全部命令")
    return render_panel(f"T R I N A  /  {__version__}", rows=[
        ("模型", config.llm.model), ("后端", config.llm.backend),
        ("执行设置", f"审批 {approval_mode} · 策略 {config.policy.mode} · 等级上限 {config.scope.exploit_level}"),
        ("授权范围", ", ".join([*config.scope.authorized_cidrs, *config.scope.authorized_hosts]) or "未配置"),
    ], sections=[("开始", "直接输入问题开始对话，或使用 /play 启动完整任务。"),
                 ("常用入口", shortcuts)],
       footer="运行中：直接输入提示介入 · /status 查看 · /pause 暂停 · /stop 停止\n审批时：y 允许 / n 拒绝；回车查看状态。", width=width, color=color)


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
