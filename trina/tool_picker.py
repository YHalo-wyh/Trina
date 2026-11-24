"""OMP 风格的终端工具目录；只浏览真实 ToolRegistry 投影。"""
from __future__ import annotations

from collections import Counter

from .model_picker_view import key_hints, padded
from .tool_catalog import ToolCatalogEntry, filter_tool_catalog


def select_tool(entries: list[ToolCatalogEntry], *, plain: bool = False) -> ToolCatalogEntry | None:
    """搜索并查看工具解析详情，确认后返回所选行。"""
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
    from prompt_toolkit.layout.processors import AfterInput, BeforeInput
    from prompt_toolkit.mouse_events import MouseEventType

    from .console_state import _cells, _fit_cells
    from .console_ui import _REDRAW_INTERVAL, _panel_app, run_panel
    from .scrollbar import ScrollbarControl
    from .terminal import clean_text

    search = Buffer(multiline=False)
    help_open = False
    browsing_groups = False
    counts = Counter(item.spec.category for item in entries)
    sources = Counter(item.source_kind for item in entries)
    groups = [
        ("all", "全部工具", len(entries)),
        ("visible", "当前可见", sum(item.visible for item in entries)),
        ("hidden", "按需隐藏", sum(not item.visible for item in entries)),
        ("unavailable", "当前不可用", sum(not item.available for item in entries)),
        *((f"category:{name}", name, count) for name, count in sorted(counts.items())),
        *((f"source:{name}", name, count) for name, count in sorted(sources.items()) if name != "System"),
    ]
    group = selected = 0
    visible: list[ToolCatalogEntry] = []
    keys = KeyBindings()
    highlight = "reverse" if plain else "class:model.row"

    def width() -> int:
        return max(1, get_app().output.get_size().columns - 4)

    wide = Condition(lambda: width() >= 76)
    roomy = Condition(lambda: get_app().output.get_size().rows >= 15)
    side_width = 22

    def list_width() -> int:
        return max(1, width() - (side_width + 3 if wide() else 0))

    def refresh(_buffer=None) -> None:
        nonlocal visible, selected, browsing_groups
        visible = filter_tool_catalog(entries, query=search.text, group=groups[group][0])
        selected = min(selected, max(0, len(visible) - 1))
        if _buffer is not None:
            browsing_groups = False

    refresh()
    search.on_text_changed += refresh

    def choose_group(pos: int) -> None:
        nonlocal group, browsing_groups
        group = pos % len(groups)
        browsing_groups = True
        refresh()

    def choose_row(pos: int) -> None:
        nonlocal selected, browsing_groups
        selected = max(0, min(len(visible) - 1, pos))
        browsing_groups = False

    class GroupRows(UIControl):
        def create_content(self, width: int, height: int) -> UIContent:
            def row(pos: int):
                _key, label, count = groups[pos]
                current = pos == group
                focused = current and browsing_groups
                base = highlight if focused else ""
                prefix = "▸ " if current else "  "
                name = padded(clean_text(label), max(0, width - _cells(prefix) - len(str(count)) - 2))
                style = "class:model.accent" if current else "class:dim" if not count else ""
                return [(base + " " + style, prefix + name), (base + " class:dim", f"{count} ")]
            return UIContent(get_line=row, line_count=len(groups),
                             cursor_position=Point(x=0, y=group), show_cursor=False)

        def mouse_handler(self, event):
            if event.event_type == MouseEventType.MOUSE_UP and 0 <= event.position.y < len(groups):
                choose_group(event.position.y)
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                choose_group(group + (-1 if event.event_type == MouseEventType.SCROLL_UP else 1))
            else:
                return NotImplemented

    def columns(table_width: int) -> list[tuple[str, int]]:
        fields: list[tuple[str, int]] = []
        if table_width >= 48:
            fields.append(("类别", 11))
        if table_width >= 61:
            fields.append(("动作", 8))
        if table_width >= 74:
            fields.append(("状态", 10))
        if table_width >= 88:
            fields.append(("调用", 6))
        return [("工具", max(8, table_width - 4 - sum(size + 2 for _, size in fields))), *fields]

    class Headings(UIControl):
        def create_content(self, table_width: int, height: int) -> UIContent:
            first, *rest = columns(table_width)
            value = "    " + padded(first[0], first[1])
            for name, size in rest:
                value += "  " + padded(name, size, right=name == "调用")
            return UIContent(get_line=lambda _: [("class:dim", value)], line_count=1, show_cursor=False)

    class ToolRows(UIControl):
        def create_content(self, table_width: int, height: int) -> UIContent:
            first, *rest = columns(table_width)

            def row(pos: int):
                if not visible:
                    return [("class:dim", _fit_cells("没有匹配工具 · 修改关键词或左侧筛选", table_width))]
                item = visible[pos]
                focused = pos == selected and not browsing_groups
                base = highlight if focused else ""
                state = "不可用" if not item.available else "模型可见" if item.visible else "按需隐藏"
                state_style = "class:model.live" if item.visible and item.available else "class:dim"
                parts = [(base + " class:model.accent", "▸ " if focused else "  "),
                         (base + " " + ("class:model.live" if item.visible else "class:dim"),
                          "● " if item.visible else "○ "),
                         (base, padded(item.name, first[1]))]
                values = {"类别": item.spec.category, "动作": item.spec.action_level,
                          "状态": state, "调用": str(item.stats.calls)}
                for name, size in rest:
                    style = base + (" " + state_style if name == "状态" else " class:dim")
                    parts.append((style, "  " + padded(values[name], size, right=name == "调用")))
                return parts

            return UIContent(get_line=row, line_count=max(1, len(visible)),
                             cursor_position=Point(x=0, y=selected), show_cursor=False)

        def mouse_handler(self, event):
            if event.event_type == MouseEventType.MOUSE_UP:
                choose_row(event.position.y)
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                choose_row(selected + (-1 if event.event_type == MouseEventType.SCROLL_UP else 1))
            else:
                return NotImplemented

    def header():
        route = entries[0].route if entries else "general"
        role = entries[0].role if entries else "commander"
        title = "TRINA  /  工具控制中心" if width() >= 48 else "工具控制中心"
        right = f"{sum(item.visible for item in entries)} 可见 / {len(entries)} 注册  ·  {route} · {role}"
        right = _fit_cells(right, max(0, width() - _cells(title) - 3))
        return [("class:model.accent", title), ("", " " * max(1, width() - _cells(title + right))),
                ("class:model.live", right)]

    def details():
        if not visible:
            return [("class:dim", "没有可显示的工具")]
        item = visible[selected]
        params = ", ".join(
            name + ("*" if spec.required else "") + ":" + spec.type
            for name, spec in item.spec.params.items()
        ) or "无"
        p95 = "—" if item.stats.p95_seconds is None else f"{item.stats.p95_seconds:.3f}s"
        availability = "可用" if item.available else "不可用 · " + item.availability_reason
        lines = [
            ("bold", _fit_cells(f"{item.name}  ·  {item.source_kind}  ·  {availability}", width())),
            ("", "\n" + _fit_cells(item.spec.summary, width())),
            ("class:dim", "\n" + _fit_cells("参数  " + params, width())),
            ("class:model.live" if item.visible else "class:dim",
             "\n" + _fit_cells("解析  " + " → ".join(item.resolution), width())),
            ("class:dim", "\n" + _fit_cells(
                f"原因  {item.visibility_reason}  ·  来源 {item.source}  ·  本会话 {item.stats.calls} 次 / {item.stats.failed} 失败 / p95 {p95}",
                width(),
            )),
        ]
        return lines

    def help_rows():
        return [
            ("class:model.key", "↑↓ / PgUp PgDn"), ("", " 浏览工具    "),
            ("class:model.key", "←→"), ("", " 切换分栏\n"),
            ("class:model.key", "Tab / Shift+Tab"), ("", " 轮换筛选    "),
            ("class:model.key", "输入"), ("", " 搜索名称、说明、参数或来源"),
        ]

    @keys.add("up")
    @keys.add("down")
    @keys.add("pageup")
    @keys.add("pagedown")
    def move(event):
        window = sidebar if browsing_groups else tool_window
        page = max(1, window.render_info.window_height if window.render_info else 1)
        delta = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}[event.key_sequence[-1].key.value]
        choose_group(group + delta) if browsing_groups else choose_row(selected + delta)

    @keys.add("left", filter=Condition(lambda: not search.text))
    def focus_groups(event):
        nonlocal browsing_groups
        browsing_groups = True

    @keys.add("right", filter=Condition(lambda: browsing_groups))
    def focus_tools(event):
        nonlocal browsing_groups
        browsing_groups = False

    @keys.add("tab")
    def next_group(event):
        choose_group(group + 1)

    @keys.add("s-tab")
    def previous_group(event):
        choose_group(group - 1)

    @keys.add("enter")
    def confirm(event):
        nonlocal browsing_groups
        if browsing_groups:
            browsing_groups = False
        elif visible:
            event.app.exit(result=visible[selected])

    @keys.add("f1")
    def toggle_help(event):
        nonlocal help_open
        help_open = not help_open

    @keys.add("escape", filter=Condition(lambda: help_open))
    def close_help(event):
        nonlocal help_open
        help_open = False

    @keys.add("escape", filter=Condition(lambda: not help_open))
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    def search_label():
        scope = groups[group][1] + " / " if not wide() and group else ""
        return [("class:model.accent", " " + scope + "搜索 › ")]

    def placeholder():
        return [] if search.text else [("class:dim", "工具名、说明、参数或来源")]

    def counter():
        return [("class:dim", f" {selected + 1 if visible else 0} / {len(visible)} ")]

    field = BufferControl(buffer=search, input_processors=[BeforeInput(search_label), AfterInput(placeholder)])
    search_bar = VSplit([
        Window(field, height=1, wrap_lines=False),
        Window(FormattedTextControl(counter), height=1, dont_extend_width=True),
    ], style="class:model.search")
    tool_window = Window(ToolRows(), wrap_lines=False, always_hide_cursor=True)

    def viewport() -> tuple[int, int, int]:
        height = tool_window.render_info.window_height if tool_window.render_info else 1
        return height, max(0, len(visible) - height), tool_window.vertical_scroll

    def seek(offset: int, _maximum: int) -> None:
        height, maximum, first = viewport()
        offset = min(offset, maximum)
        choose_row(offset + min(height - 1, max(0, selected - first)))
        tool_window.vertical_scroll = offset

    scrollbar = ScrollbarControl(viewport, seek, lambda step: choose_row(selected - step),
                                 thumb_style="class:panel.border", track_style="class:dim", hide_when_fits=True)
    track = Window(scrollbar, width=1)
    sidebar = Window(GroupRows(), wrap_lines=False, always_hide_cursor=True)

    def line(content, height=1):
        return Window(FormattedTextControl(content), height=height, wrap_lines=False)

    def footer():
        return key_hints([("Enter", "保留详情"), ("Esc", "返回"), ("F1", "按键"),
                          ("↑↓", "浏览"), ("←→", "分栏"), ("Tab", "筛选")], width())

    def rule():
        return Window(height=1, char="─", style="class:model.rule")

    providers_visible = Condition(lambda: wide() or browsing_groups)
    tools_visible = Condition(lambda: wide() or not browsing_groups)
    table = VSplit([HSplit([Window(Headings(), height=1), tool_window]),
                    HSplit([Window(height=1), track])])
    body = HSplit([
        line(header), rule(),
        VSplit([
            ConditionalContainer(HSplit([sidebar], width=lambda: side_width if wide() else None),
                                 filter=providers_visible),
            ConditionalContainer(VSplit([Window(width=1), Window(width=1, char="│", style="class:model.rule"),
                                         Window(width=1)]), filter=wide),
            ConditionalContainer(HSplit([search_bar, table]), filter=tools_visible),
        ]),
        rule(),
        ConditionalContainer(line(details, 5), filter=roomy & Condition(lambda: not help_open)),
        ConditionalContainer(line(help_rows, 3), filter=Condition(lambda: help_open)),
        line(footer),
    ])
    app = _panel_app(body, "工具控制中心", keys, field, plain=plain, expanded=True)
    app.before_render += lambda _: scrollbar.begin_frame()
    app.after_render += lambda current: scrollbar.finish_frame(current, track, _REDRAW_INTERVAL)
    return run_panel(app)


__all__ = ["select_tool"]
