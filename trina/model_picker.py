"""终端模型目录，按连接和本地能力筛选。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from .model_names import base_name


@dataclass(frozen=True)
class ModelChoice:
    connection_id: str
    connection_label: str
    model: str
    current: bool = False
    api: str = ""
    context_window: int = 0
    manual_context: bool = False
    levels: tuple[str, ...] = ("default",)


def select_model(choices: list[ModelChoice], *, plain: bool = False) -> ModelChoice | Literal["settings"] | None:
    """在搜索框内直接导航，确认时才修改会话。"""
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput
    from prompt_toolkit.mouse_events import MouseEventType

    from .console_state import _cells, _fit_cells
    from .console_ui import _REDRAW_INTERVAL, _panel_app, run_panel
    from .scrollbar import ScrollbarControl
    from .terminal import clean_text

    search = Buffer(multiline=False)
    providers = dict((item.connection_id, clean_text(item.connection_label)) for item in choices)
    counts = Counter(item.connection_id for item in choices)
    groups = [(None, "全部连接", len(choices)), *((key, label, counts[key]) for key, label in providers.items())]
    index = [(item, " ".join((item.connection_id, item.connection_label, item.model, base_name(item.model))).casefold())
             for item in choices]
    filters = ("全部模型", "可调思考", "窗口 ≥500K")
    group = kind = selected = 0
    visible = choices[:]
    highlight = "reverse" if plain else "class:chosen"
    keys = KeyBindings()

    def refresh(_buffer=None):
        nonlocal visible, selected
        terms = search.text.casefold().split()
        provider = groups[group][0]
        visible = [item for item, text in index
                   if (provider is None or item.connection_id == provider)
                   and (kind != 1 or len(item.levels) > 1)
                   and (kind != 2 or item.context_window >= 500_000)
                   and all(term in text for term in terms)]
        selected = 0

    search.on_text_changed += refresh

    def width():
        return max(1, get_app().output.get_size().columns - 6)

    wide = Condition(lambda: width() >= 70)
    roomy = Condition(lambda: get_app().output.get_size().rows >= 13)
    side_width = 21

    def pick_group(pos):
        nonlocal group
        group = pos % len(groups)
        refresh()

    def pick_kind(pos):
        nonlocal kind
        kind = pos % len(filters)
        refresh()

    def pick_row(pos):
        nonlocal selected
        selected = max(0, min(len(visible) - 1, pos))

    def mouse(pos, choose, navigate, position):
        def handle(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                choose(pos)
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                step = -1 if event.event_type == MouseEventType.SCROLL_UP else 1
                navigate(position() + step)
        return handle

    def provider_rows():
        result = []
        for pos, (_, label, count) in enumerate(groups):
            if pos:
                result.append(("", "\n"))
            number = str(count)
            name = _fit_cells(label, side_width - len(number) - 4)
            line = ("› " if pos == group else "  ") + name
            line += " " * (side_width - _cells(line) - len(number) - 1) + number
            result.append((highlight if pos == group else "", line,
                           mouse(pos, pick_group, pick_group, lambda: group)))
        return result

    def model_rows():
        if not visible:
            return [("class:dim", "没有匹配的模型" if choices else "尚无模型 · F2 配置连接")]
        result = []
        available = width() - (side_width + 3 if wide() else 0) - 1
        for pos, item in enumerate(visible):
            if pos:
                result.append(("", "\n"))
            label = ("● " if item.current else "") + item.model
            provider = _fit_cells(item.connection_label, min(23, available // 3)) if available >= 38 else ""
            name = ("› " if pos == selected else "  ") + _fit_cells(label, available - _cells(provider) - 4)
            line = name + " " * max(1, available - _cells(name + provider)) + provider
            result.append((highlight if pos == selected else "", line, mouse(pos, pick_row, pick_row, lambda: selected)))
        return result

    def tabs():
        result = []
        for pos, label in enumerate(filters):
            result.append((highlight if pos == kind else "class:dim", (f"[{label}]" if pos == kind else f" {label} "),
                           mouse(pos, pick_kind, pick_kind, lambda: kind)))
        return result

    def status():
        return [("class:dim", _fit_cells(f"{groups[group][1]}   {selected + 1 if visible else 0} / {len(visible)}", width()))]

    def details():
        if not visible:
            return []
        item = visible[selected]
        title = clean_text(base_name(item.model)) + "  ·  " + clean_text(item.connection_label)
        context = f"本地窗口 {item.context_window:,}（{'手动' if item.manual_context else '自动'}）"
        levels = " / ".join(item.levels)
        return [("bold", _fit_cells(title, width())), ("class:dim", "\n" + _fit_cells(
            f"{item.api}  ·  {context}  ·  思考 {levels}", width()))]

    @keys.add("up")
    @keys.add("down")
    @keys.add("pageup")
    @keys.add("pagedown")
    def move(event):
        page = max(1, model_window.render_info.window_height if model_window.render_info else 1)
        delta = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}[event.key_sequence[-1].key.value]
        pick_row(selected + delta)

    @keys.add("tab")
    def next_provider(event):
        pick_group(group + 1)

    @keys.add("s-tab")
    def prev_provider(event):
        pick_group(group - 1)

    @keys.add("escape", "left")
    def prev_filter(event):
        pick_kind(kind - 1)

    @keys.add("escape", "right")
    def next_filter(event):
        pick_kind(kind + 1)

    @keys.add("enter")
    def confirm(event):
        if visible:
            event.app.exit(result=visible[selected])

    @keys.add("f2")
    def settings(event):
        event.app.exit(result="settings")

    @keys.add("escape")
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    field = BufferControl(buffer=search, input_processors=[BeforeInput(" 搜索 › ")])
    model_window = Window(FormattedTextControl(model_rows, get_cursor_position=lambda: Point(x=0, y=selected)),
                          wrap_lines=False, always_hide_cursor=True)

    def viewport() -> tuple[int, int, int]:
        height = model_window.render_info.window_height if model_window.render_info else 1
        return height, max(0, len(visible) - height), model_window.vertical_scroll

    def seek(offset: int, _maximum: int) -> None:
        height, maximum, first = viewport()
        offset = min(offset, maximum)
        pick_row(offset + min(height - 1, max(0, selected - first)))
        model_window.vertical_scroll = offset

    scrollbar = ScrollbarControl(viewport, seek, lambda step: pick_row(selected - step),
                                 thumb_style="class:panel.border", track_style="class:dim", hide_when_fits=True)
    track = Window(scrollbar, width=1)
    sidebar = Window(FormattedTextControl(provider_rows, get_cursor_position=lambda: Point(x=0, y=group)),
                     width=side_width, wrap_lines=False, always_hide_cursor=True)
    def line(content, height=1):
        return Window(FormattedTextControl(content), height=height, wrap_lines=False)

    def footer():
        hint = "↑↓ 模型 · Tab/Shift+Tab 连接 · Alt+←/→ 筛选 · Enter 切换 · F2 设置 · Esc 返回"
        return [("class:dim", hint if _cells(hint) <= width() else "↑↓ Enter F2 Esc · Tab 连接")]

    body = HSplit([
        VSplit([ConditionalContainer(HSplit([line("供应商连接"), sidebar]), filter=wide),
                ConditionalContainer(Window(width=1, char="│", style="class:dim"), filter=wide),
                HSplit([line(tabs), Window(field, height=1, wrap_lines=False), line(status), VSplit([model_window, track])])],
               padding=1),
        ConditionalContainer(Window(height=1, char="─", style="class:dim"), filter=roomy),
        ConditionalContainer(line(details, 2), filter=roomy), line(footer),
    ])
    app = _panel_app(body, "切换模型", keys, field, plain=plain, expanded=True)
    app.before_render += lambda _: scrollbar.begin_frame()
    app.after_render += lambda current: scrollbar.finish_frame(current, track, _REDRAW_INTERVAL)
    return run_panel(app)
