"""子 Worker 轻量任务面板：只投影已提交状态，模型设置通过命令层处理。"""
from __future__ import annotations

import time
from typing import Any

from .console_state import _cells, _fit_cells, _number
from .terminal import THEME


STATUS = {"pending": ("○", "等待"), "running": ("◉", "运行"), "completed": ("✓", "完成"),
          "failed": ("×", "失败"), "blocked": ("!", "受阻"), "cancelled": ("■", "已取消"),
          "skipped": ("–", "跳过"), "timed_out": ("!", "超时")}


def render_workers(rows: list[dict[str, Any]], width: int, selected: int, *, now: float | None = None,
                   count: int = 8) -> list[tuple[str, str]]:
    """每项三行，名称/模型对齐两端；窄屏保留名称、状态和思考档位。"""
    width = max(1, width)
    now = time.time() if now is None else now
    start = max(0, selected - count + 1)
    out: list[tuple[str, str]] = []
    for index in range(start, min(len(rows), start + count)):
        row = rows[index]
        icon, status = STATUS.get(row["status"], ("?", row["status"]))
        style = "class:chosen" if index == selected else "class:name"
        right = _fit_cells(f"{row['model']}  ◦ {row['reasoning_effort'] or 'default'}", max(4, width // 2)) if width >= 42 else ""
        left = _fit_cells(f"{'›' if index == selected else ' '} {icon} {row['id']} · {status}", max(1, width - _cells(right) - (2 if right else 0)))
        gap = max(0, width - _cells(left) - _cells(right))
        out.extend([(style, left), ("", " " * gap), ("class:model", right), ("", "\n")])
        out.append(("class:dim", _fit_cells("    " + row["goal"], width) + "\n"))
        stats = []
        if row.get("cost") is not None:
            stats.append(f"${row['cost']:.3f}")
        if row.get("started_at"):
            elapsed = max(0, int((row.get("ended_at") or now) - row["started_at"]))
            stats.append(f"{elapsed // 60}m{elapsed % 60:02d}s")
        for key, label in (("calls", "req"), ("tools", "tools"), ("tokens", "tok")):
            if key in row:
                stats.append(f"{_number(row[key])} {label}")
        out.append(("class:dim", _fit_cells("    " + " · ".join(stats), width) + "\n"))
    if not rows:
        out.append(("class:dim", _fit_cells("  暂无任务 · N 创建子 Worker", width) + "\n"))
    return out


def render_grid(manager, width: int, height: int, selected: int, *, scroll: int = 0, zoom: bool = False):
    """有界四宫格；每格只绘制可见物理行，小窗口自动显示所选任务。"""
    width, height = max(1, width), max(1, height)
    if width < 4 or height < 3:
        return [("class:dim", _fit_cells("Worker", width))]
    rows = manager.rows()
    wide = width >= 96 and height >= 16 and not zoom
    columns, per_page = (2, 4) if wide else (1, 1)
    selected = max(0, min(selected, max(0, len(rows) - 1)))
    start = selected // per_page * per_page
    box_width = (width - 1) // 2 if wide else width
    box_height = height // 2 if wide else height
    inner = max(1, box_width - 2)
    result = []
    def pad(text):
        fitted = _fit_cells(text, inner)
        return fitted + " " * max(0, inner - _cells(fitted))
    def panel(index):
        focused = index == selected
        border = "class:focusborder" if focused else "class:rail"
        row = rows[index] if index < len(rows) else None
        title = (f" {index + 1} {row['id']} · {STATUS.get(row['status'], ('', row['status']))[1]} " if row else " 空闲 ")
        title = _fit_cells(title, inner)
        lines = [[(border, "┌" + title + "─" * max(0, inner - _cells(title)) + "┐")]]
        if row:
            model = f"{row['model']} ◦ {row['reasoning_effort'] or 'default'}"
            text = manager.dialog_lines(row["id"], inner)
            available = max(1, box_height - 4)
            offset = min(max(0, scroll), max(0, len(text) - available)) if focused else 0
            end = max(0, len(text) - offset)
            body = text[max(0, end - available):end]
            if not body:
                body = [("过程", row.get("activity") or ("等待依赖或空闲 Worker" if row["status"] == "pending" else row["goal"]))]
            body = [("模型", model), *body]
            stats = [f"{_number(row[key])} {label}" for key, label in (("calls", "req"), ("tools", "tools"), ("tokens", "tok")) if key in row]
            if stats:
                body.append(("状态", " · ".join(stats)))
        else:
            body = [("过程", "等待 Commander 分配任务")]
        for kind, text in body[:max(0, box_height - 2)]:
            tone = "class:model" if kind == "模型" else "class:name" if kind in {"回复", "输出"} else "class:dim"
            lines.append([(border, "│"), (tone, pad(text)), (border, "│")])
        while len(lines) < max(1, box_height - 1):
            lines.append([(border, "│"), ("", " " * inner), (border, "│")])
        if box_height >= 2:
            lines.append([(border, "└" + "─" * inner + "┘")])
        return lines
    for band in range(2 if wide else 1):
        panes = [panel(start + band * columns + col) for col in range(columns)]
        for line in range(box_height):
            for col, pane in enumerate(panes):
                if col:
                    result.append(("", " "))
                result.extend(pane[line])
            result.append(("", "\n"))
    return result


def show_subworkers(manager, config, *, notice: str = "", view: str = "grid"):
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import ColorDepth
    from .console_ui import _low_latency_keys
    from .swarm import task_llm

    selected = [0 if manager.rows() else -1]
    mode, zoom, scroll = [view], [False], [0]
    llm = task_llm(config)
    def width():
        return max(1, app.output.get_size().columns - 2)
    def header():
        metrics = manager.usage.footer(width(), compact=True)
        return [("class:title", _fit_cells(" ◇ SUBWORKERS  " + (manager.badge or "READY"), width()) + "\n"),
                ("class:dim", _fit_cells(" " + metrics, width()))]
    def defaults():
        text = f" {'›' if selected[0] == -1 else ' '} 默认 · {llm.model} ◦ {llm.reasoning_effort} · 并发 {config.subworker.workers}"
        return [("class:chosen" if selected[0] == -1 else "class:dim", _fit_cells(text, width()))]
    def content():
        rows = manager.rows()
        selected[0] = min(selected[0], len(rows) - 1)
        if mode[0] == "grid":
            return render_grid(manager, width(), max(1, app.output.get_size().rows - 7 - int(bool(notice))), selected[0], scroll=scroll[0], zoom=zoom[0])
        return render_workers(rows, width(), selected[0], count=max(1, (app.output.get_size().rows - 7) // 3))

    bindings = KeyBindings()
    @bindings.add("up")
    def up(event):
        selected[0] = max(-1, selected[0] - 1)
    @bindings.add("down")
    def down(event):
        selected[0] = min(len(manager.rows()) - 1, selected[0] + 1)
        scroll[0] = 0
    @bindings.add("tab")
    @bindings.add("right")
    def next_worker(event):
        selected[0] = (selected[0] + 1) % max(1, len(manager.rows()))
        scroll[0] = 0
    @bindings.add("left")
    def previous_worker(event):
        selected[0] = max(0, selected[0] - 1)
        scroll[0] = 0
    @bindings.add("g")
    def toggle(event):
        mode[0] = "list" if mode[0] == "grid" else "grid"
    @bindings.add("z")
    def focus(event):
        zoom[0] = not zoom[0]
    @bindings.add("pageup")
    def older(event):
        rows = manager.rows()
        if not rows:
            return
        height = max(1, app.output.get_size().rows - 7 - int(bool(notice)))
        wide = width() >= 96 and height >= 16 and not zoom[0]
        inner = max(1, ((width() - 1) // 2 if wide else width()) - 2)
        available = max(1, (height // 2 if wide else height) - 4)
        maximum = max(0, len(manager.dialog_lines(rows[max(0, selected[0])]["id"], inner)) - available)
        scroll[0] = min(maximum, scroll[0] + 3)
    @bindings.add("pagedown")
    def newer(event):
        scroll[0] = max(0, scroll[0] - 3)
    @bindings.add("end")
    def latest(event):
        scroll[0] = 0
    @bindings.add("d")
    def default(event):
        selected[0] = -1
    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("c-d")
    def close(event):
        event.app.exit(result=None)
    def action(name, *, with_task=False):
        def dispatch(event):
            rows = manager.rows()
            task = rows[selected[0]]["id"] if with_task and 0 <= selected[0] < len(rows) else ""
            if name == "inspect" and not task:
                return
            event.app.exit(result=(name, task))
        return dispatch
    for key, name in (("m", "model"), ("t", "thinking"), ("p", "provider"), ("enter", "inspect"), ("f", "message"), ("c", "cancel")):
        bindings.add(key)(action(name, with_task=True))
    for key, name in (("n", "new"), ("s", "settings"), ("x", "stop"), ("space", "continue" if manager.runtime and manager.runtime.pause_event.is_set() else "pause")):
        bindings.add(key)(action(name))
    class DashboardControl(FormattedTextControl):
        def mouse_handler(self, event):
            from prompt_toolkit.mouse_events import MouseEventType
            if event.event_type == MouseEventType.SCROLL_UP:
                older(None)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                newer(None)
            else:
                return NotImplemented
            app.invalidate()
            return None
    body = DashboardControl(content, focusable=True, show_cursor=False)
    app = Application(layout=Layout(HSplit([
        Window(FormattedTextControl(header), height=2, wrap_lines=False),
        Window(FormattedTextControl(defaults), height=1, wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:rail", "─" * width())]), height=1),
        Window(body, wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:title", _fit_cells(notice, width()))]), height=1 if notice else 0, wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:dim", _fit_cells(" Tab 切换 · G 列表/四格 · Z 放大 · PgUp/Dn 历史 · F 补充 · C 取消 · M 模型 · T 思考 · N 新建 · Esc 返回", width()))]), height=1),
    ]), focused_element=body), key_bindings=bindings, full_screen=True, erase_when_done=True,
        refresh_interval=.25, min_redraw_interval=1 / 60, max_render_postpone_time=.01,
        color_depth=ColorDepth.DEPTH_24_BIT, mouse_support=True,
        style=Style.from_dict({"title": "bold " + THEME["rose"], "chosen": "bold " + THEME["rose"],
                              "name": THEME["text"], "model": THEME["lilac"], "dim": THEME["muted"], "rail": THEME["violet"], "focusborder": THEME["rose"]}))
    return _low_latency_keys(app).run()
