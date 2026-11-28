"""子 Worker 轻量任务面板：只投影已提交状态，模型设置通过命令层处理。"""
from __future__ import annotations

import time
from typing import Any

from .console_state import _cells, _fit_cells, _number
STATUS = {"pending": ("○", "等待"), "running": ("◉", "运行"), "completed": ("✓", "完成"),
          "failed": ("×", "失败"), "blocked": ("!", "受阻"), "cancelled": ("■", "已取消"),
          "skipped": ("–", "跳过"), "timed_out": ("!", "超时")}
GROUP_ORDER = {"running": 0, "pending": 1, "completed": 2, "failed": 3, "blocked": 3,
               "timed_out": 3, "cancelled": 3, "skipped": 3}
GROUP_LABEL = {0: "RUNNING", 1: "QUEUED", 2: "COMPLETED", 3: "FAILED"}


def ordered_workers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按运行分组排序，同组保留 Commander 的原始稳定顺序。"""
    buckets: list[list[dict[str, Any]]] = [[], [], [], []]
    for row in rows:
        buckets[GROUP_ORDER.get(row["status"], 3)].append(row)
    return [row for bucket in buckets for row in bucket]


def _progress(row: dict[str, Any]) -> str:
    if row["status"] == "completed":
        return "100%"
    if row["status"] in {"failed", "blocked", "timed_out", "cancelled", "skipped"}:
        return "结束"
    if row["status"] == "pending":
        return "等待依赖"
    calls, tools = int(row.get("calls", 0)), int(row.get("tools", 0))
    return f"阶段 {max(1, calls + tools + 1)}"


def _updated(row: dict[str, Any], now: float) -> str:
    stamp = row.get("updated_at") or row.get("ended_at") or row.get("started_at")
    if not stamp:
        return "尚未更新"
    seconds = max(0, int(now - stamp))
    return "刚刚" if seconds < 2 else f"{seconds}s 前" if seconds < 60 else f"{seconds // 60}m 前"


def _grid_size(width: int, height: int, zoom: bool) -> int:
    if zoom:
        return 1
    if width >= 152 and height >= 24:
        return 3
    return 2 if width >= 96 and height >= 16 else 1


def render_workers(rows: list[dict[str, Any]], width: int, selected: int, *, now: float | None = None,
                   count: int = 8, first: int | None = None, ascii: bool = False) -> list[tuple[str, str]]:
    """每项三行，名称/模型对齐两端；窄屏保留名称、状态和思考档位。"""
    width = max(1, width)
    now = time.time() if now is None else now
    rows = ordered_workers(rows)
    start = max(0, selected - count + 1) if first is None else first
    out: list[tuple[str, str]] = []
    previous_group = None
    for index in range(start, min(len(rows), start + count)):
        row = rows[index]
        group = GROUP_ORDER.get(row["status"], 3)
        if group != previous_group:
            rule = "-" if ascii else "─"
            label = f" {GROUP_LABEL[group]} "
            out.append(("class:dim", _fit_cells(label + rule * max(0, width - _cells(label)), width) + "\n"))
            previous_group = group
        icon, status = STATUS.get(row["status"], ("?", row["status"]))
        if ascii:
            icon = {"pending": "o", "running": ">", "completed": "+", "failed": "x"}.get(row["status"], "!")
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
        stage = row.get("activity") or status
        stats.extend((_progress(row), stage, _updated(row, now)))
        out.append(("class:dim", _fit_cells("    " + " · ".join(stats), width) + "\n"))
    if not rows:
        out.append(("class:dim", _fit_cells("  暂无任务 · N 创建子 Worker", width) + "\n"))
    return out


def render_grid(manager, width: int, height: int, selected: int, *, scroll: int = 0, zoom: bool = False,
                first: int | None = None, rows: list[dict[str, Any]] | None = None, ascii: bool = False):
    """只排版视口内的四张卡片，窄屏和放大视图显示单卡。"""
    width, height = max(1, width), max(1, height)
    if width < 4 or height < 3:
        return [("class:dim", _fit_cells("Worker", width))]
    rows = ordered_workers(manager.rows() if rows is None else rows)
    columns = _grid_size(width, height, zoom)
    per_page = columns ** 2
    selected = min(selected, len(rows) - 1)
    start = max(0, selected) // per_page * per_page if first is None else first
    box_width = (width + 1) // columns - 1
    box_height = height // columns
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
        top_left, horizontal, top_right, vertical, bottom_left, bottom_right = (
            ("+", "-", "+", "|", "+", "+") if ascii else ("┌", "─", "┐", "│", "└", "┘")
        )
        lines = [[(border, top_left + title + horizontal * max(0, inner - _cells(title)) + top_right)]]
        if row:
            model = f"{row['model']} ◦ {row['reasoning_effort'] or 'default'}"
            text = manager.dialog_lines(row["id"], inner)
            available = max(1, box_height - 4)
            offset = min(max(0, scroll), max(0, len(text) - available)) if focused else 0
            end = max(0, len(text) - offset)
            body = text[max(0, end - available):end]
            if not body:
                body = [("过程", row.get("activity") or ("等待依赖或空闲 Worker" if row["status"] == "pending" else row["goal"]))]
            body = [("模型", model), ("阶段", f"{row.get('activity') or STATUS.get(row['status'], ('', row['status']))[1]} · {_progress(row)}"), *body]
            stats = []
            if row.get("started_at"):
                elapsed = max(0, int((row.get("ended_at") or time.time()) - row["started_at"]))
                stats.append(f"{elapsed // 60}m{elapsed % 60:02d}s")
            stats.extend(f"{_number(row[key])} {label}" for key, label in (("calls", "req"), ("tools", "tools"), ("tokens", "tok")) if key in row)
            if stats:
                body.append(("状态", " · ".join(stats) + " · 更新 " + _updated(row, time.time())))
            if row.get("error"):
                body.append(("错误", str(row["error"])))
        else:
            body = [("过程", "等待 Commander 分配任务")]
        for kind, text in body[:max(0, box_height - 2)]:
            tone = {"模型": "class:model", "回复": "class:name", "输出": "class:name", "思考": "class:thinking",
                    "工具": "class:running", "结果": "class:success", "错误": "class:error"}.get(kind, "class:dim")
            lines.append([(border, vertical), (tone, pad(text)), (border, vertical)])
        while len(lines) < max(1, box_height - 1):
            lines.append([(border, vertical), ("", " " * inner), (border, vertical)])
        if box_height >= 2:
            lines.append([(border, bottom_left + horizontal * inner + bottom_right)])
        return lines
    for band in range(columns):
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
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import ColorDepth
    from .console_ui import _REDRAW_INTERVAL, _low_latency_keys, run_panel
    from .scrollbar import ScrollbarControl
    from .swarm import task_llm
    from .output_styles import output_style

    from .ui_theme import ascii_mode, ui_style

    compatible = ascii_mode(config.ui.output_palette)
    rows = ordered_workers(manager.rows())
    selected = [0 if rows else -1]
    mode, zoom, scroll, first_row = [view], [False], [0], [0]
    llm = task_llm(config)
    def width():
        return max(1, app.output.get_size().columns - 2)
    def height():
        return max(1, app.output.get_size().rows - 7 - int(bool(notice)))
    def body_width():
        return max(1, width() - 1)
    def window_size():
        if mode[0] == "grid":
            columns = _grid_size(body_width(), height(), zoom[0])
            return columns, columns
        return 1, max(1, height() // 3)
    def row_limits(count):
        columns, visible = window_size()
        total = (count + columns - 1) // columns
        return columns, visible, max(0, total - visible)
    def keep_selection_visible(count):
        columns, visible, maximum = row_limits(count)
        first_row[0] = min(first_row[0], maximum)
        if selected[0] >= 0:
            active_row = selected[0] // columns
            first_row[0] = max(active_row - visible + 1, min(first_row[0], active_row))
        return first_row[0] * columns
    def select(index):
        count = len(manager.rows())
        selected[0] = max(-1, min(index, count - 1))
        scroll[0] = 0
        keep_selection_visible(count)
    def move_view(index):
        count = len(rows)
        columns, visible, maximum = row_limits(count)
        target = min(maximum, max(0, index))
        if target == first_row[0]:
            return
        first_row[0] = target
        low = first_row[0] * columns
        high = min(count - 1, low + columns * visible - 1)
        if count and not low <= selected[0] <= high:
            selected[0] = min(high, max(low, selected[0]))
            scroll[0] = 0

    def viewport():
        _, visible, maximum = row_limits(len(rows))
        return visible, maximum, first_row[0]

    def before_render(_app):
        nonlocal rows
        scrollbar.begin_frame()
        active_id = rows[selected[0]]["id"] if 0 <= selected[0] < len(rows) else ""
        rows = ordered_workers(manager.rows())
        if active_id:
            selected[0] = next((index for index, row in enumerate(rows) if row["id"] == active_id), selected[0])

    def header():
        metrics = manager.usage.footer(width(), compact=True)
        counts = [sum(GROUP_ORDER.get(row["status"], 3) == group for row in rows) for group in range(4)]
        groups = " · ".join(f"{GROUP_LABEL[group]} {count}" for group, count in enumerate(counts) if count)
        mark = "*" if compatible else "◇"
        return [("class:title", _fit_cells(f" {mark} SUBWORKERS  " + (manager.badge or "READY"), width()) + "\n"),
                ("class:dim", _fit_cells(" " + " · ".join(filter(None, (groups, metrics))), width()))]
    def defaults():
        text = f" {'›' if selected[0] == -1 else ' '} 默认 · {llm.model} ◦ {llm.reasoning_effort} · 并发 {config.subworker.workers}"
        return [("class:chosen" if selected[0] == -1 else "class:dim", _fit_cells(text, width()))]
    def content():
        selected[0] = min(selected[0], len(rows) - 1)
        first = keep_selection_visible(len(rows))
        if mode[0] == "grid":
            return render_grid(manager, body_width(), height(), selected[0], scroll=scroll[0],
                               zoom=zoom[0], first=first, rows=rows, ascii=compatible)
        return render_workers(rows, body_width(), selected[0], count=window_size()[1], first=first, ascii=compatible)

    bindings = KeyBindings()
    @bindings.add("up")
    def up(event):
        select(selected[0] - window_size()[0])
    @bindings.add("down")
    def down(event):
        select(max(0, selected[0] + window_size()[0]))
    @bindings.add("tab")
    @bindings.add("right")
    def next_worker(event):
        select((selected[0] + 1) % max(1, len(manager.rows())))
    @bindings.add("left")
    def previous_worker(event):
        select(max(0, selected[0] - 1))
    @bindings.add("g")
    def toggle(event):
        scrollbar.cancel_drag()
        mode[0] = "list" if mode[0] == "grid" else "grid"
        select(selected[0])
    @bindings.add("z")
    def focus(event):
        scrollbar.cancel_drag()
        zoom[0] = not zoom[0]
        select(selected[0])
    @bindings.add("pageup")
    def older(event):
        rows = ordered_workers(manager.rows())
        if not rows:
            return
        columns = _grid_size(body_width(), height(), zoom[0])
        inner = max(1, (body_width() + 1) // columns - 3)
        available = max(1, height() // columns - 4)
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
                scrollbar.cancel_drag()
                move_view(first_row[0] - 1)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                scrollbar.cancel_drag()
                move_view(first_row[0] + 1)
            else:
                return NotImplemented
            return None
    body = DashboardControl(content, focusable=True, show_cursor=False)
    scrollbar = ScrollbarControl(
        viewport, lambda offset, _maximum: move_view(offset), lambda direction: move_view(first_row[0] - direction),
        thumb_style="class:focusborder", track_style="class:rail", hide_when_fits=True,
    )
    scrollbar_window = Window(scrollbar, width=1, wrap_lines=False)
    app = Application(layout=Layout(HSplit([
        Window(FormattedTextControl(header), height=2, wrap_lines=False),
        Window(FormattedTextControl(defaults), height=1, wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:rail", ("-" if compatible else "─") * width())]), height=1),
        VSplit([Window(body, wrap_lines=False), scrollbar_window],
               width=width),
        Window(FormattedTextControl(lambda: [("class:title", _fit_cells(notice, width()))]), height=1 if notice else 0, wrap_lines=False),
        Window(FormattedTextControl(lambda: [("class:dim", _fit_cells(" Tab 切换 · G 列表/网格 · Z 放大 · PgUp/Dn 对话 · F 补充 · C 取消 · Esc 返回", width()))]), height=1),
    ]), focused_element=body), key_bindings=bindings, full_screen=True, erase_when_done=True,
        refresh_interval=.25, min_redraw_interval=_REDRAW_INTERVAL, max_render_postpone_time=0, before_render=before_render,
        after_render=lambda app: scrollbar.finish_frame(app, scrollbar_window),
        color_depth=ColorDepth.DEPTH_24_BIT, mouse_support=True,
        style=Style.from_dict({**ui_style(config.ui.output_palette, plain=compatible),
                              "name": output_style("text", config.ui.output_palette),
                              "model": output_style("code", config.ui.output_palette),
                              "focusborder": output_style("heading", config.ui.output_palette)}))
    return run_panel(_low_latency_keys(app))
