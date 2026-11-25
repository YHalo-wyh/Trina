"""OMP 风格的终端工具目录；只浏览真实 ToolRegistry 投影。"""
from __future__ import annotations

import re
from collections import Counter

from .model_picker_view import directory_header, key_hints, marked_text, padded
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
    from .console_ui import _panel_app, run_panel
    from .scrollbar import ScrollbarControl
    from .terminal import _wrap, clean_text

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
        *((f"source:{name}", name, count) for name, count in sorted(sources.items())),
    ]
    group = selected = 0
    visible: list[ToolCatalogEntry] = []
    matches: re.Pattern[str] | None = None
    keys = KeyBindings()
    highlight = "reverse" if plain else "class:model.row"
    header_context = f"{groups[1][2]} 可见 / {len(entries)} 注册"
    if entries:
        header_context += f"  ·  {entries[0].route} / {entries[0].role}"

    def width() -> int:
        return max(1, get_app().output.get_size().columns - 4)

    wide = Condition(lambda: width() >= 76)
    roomy = Condition(lambda: get_app().output.get_size().rows >= 16)
    side_details = Condition(lambda: width() >= 120 and get_app().output.get_size().rows >= 22)
    side_preview = side_details & ~Condition(lambda: help_open)
    side_width = 22

    def detail_width() -> int:
        return min(49, width() // 3) if side_details() else width()

    def list_width() -> int:
        remaining = width() - (side_width + 3 if wide() else 0)
        return max(1, remaining - (detail_width() + 3 if side_preview() else 0))

    def refresh(_buffer=None) -> None:
        nonlocal visible, selected, browsing_groups, matches
        query = search.text
        visible = filter_tool_catalog(entries, query=query, group=groups[group][0])
        terms = query.split()
        matches = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE) if terms else None
        selected = 0
        if _buffer is not None:
            browsing_groups = False

    refresh()
    search.on_text_changed += refresh

    def choose_group(pos: int) -> None:
        nonlocal group
        index = pos % len(groups)
        if index != group:
            group = index
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
            nonlocal browsing_groups
            if event.event_type == MouseEventType.MOUSE_UP and 0 <= event.position.y < len(groups):
                browsing_groups = True
                choose_group(event.position.y)
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                browsing_groups = True
                offset = -1 if event.event_type == MouseEventType.SCROLL_UP else 1
                choose_group(max(0, min(len(groups) - 1, group + offset)))
            else:
                return NotImplemented

    def columns(table_width: int) -> list[tuple[str, int]]:
        fields: list[tuple[str, int]] = []
        if table_width >= 48:
            fields.append(("类别", 11))
        if table_width >= 61:
            fields.append(("动作", 8))
        if table_width >= 74:
            fields.append(("调用", 6))
        return [("工具", max(0, table_width - 4 - sum(size + 2 for _, size in fields))), *fields]

    class Headings(UIControl):
        def create_content(self, table_width: int, height: int) -> UIContent:
            first, *rest = columns(table_width)
            value = "    " + padded(first[0], first[1])
            for name, size in rest:
                value += "  " + padded(name, size, right=name == "调用")
            return UIContent(get_line=lambda _: [("class:model.heading", value)], line_count=1, show_cursor=False)

    class ToolRows(UIControl):
        def create_content(self, table_width: int, height: int) -> UIContent:
            first, *rest = columns(table_width)

            def row(pos: int):
                if not visible:
                    return [("class:dim", _fit_cells("没有匹配工具 · 修改关键词或左侧筛选", table_width))]
                item = visible[pos]
                focused = pos == selected and not browsing_groups
                base = highlight if focused else ""
                marker, state_style = tool_marker(item)
                parts = [(base + " class:model.accent", "▸ " if focused else "  "), (f"{base} {state_style}", marker + " ")]
                parts.extend(marked_text(padded(item.name, first[1]), matches, base, "class:model.accent underline"))
                values = {"类别": item.spec.category, "动作": item.spec.action_level,
                          "调用": str(item.stats.calls)}
                for name, size in rest:
                    parts.append((base + " class:dim", "  " + padded(values[name], size, right=name == "调用")))
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

    def tool_marker(item: ToolCatalogEntry) -> tuple[str, str]:
        if not item.available:
            return "!", "class:model.warning"
        return ("●", "class:model.live") if item.visible else ("○", "class:dim")

    def header():
        return directory_header("◇", "工具控制中心", header_context, width())

    def details():
        if not visible:
            return []
        item = visible[selected]
        columns = detail_width()
        marker, style = tool_marker(item)
        status = "模型可见" if item.visible else "按需隐藏"
        if not item.available:
            status += " · 不可用"
        reason = item.availability_reason if not item.available else item.visibility_reason
        latency = f" · p95 {item.stats.p95_seconds:.3f}s" if item.stats.p95_seconds is not None else ""
        usage = f"会话 {item.stats.calls} 次 · {item.stats.failed} 失败{latency}"
        if side_details():
            params = [f"{name}{'*' if spec.required else ''}  {spec.type}" for name, spec in item.spec.params.items()]
            reason_lines = _wrap(reason, columns).splitlines()
            rows = [
                ("bold", item.name), (style, f"{marker} {status}"),
                ("class:dim", f"{item.spec.category} / {item.spec.action_level}  ·  {item.source_kind}"),
                ("", ""), *(("", line) for line in _wrap(item.spec.summary, columns).splitlines()[:3]),
                ("", ""), ("class:model.accent", "可见性"), *(("class:dim", line) for line in reason_lines[:3]),
                ("class:dim", usage), ("", ""),
                ("class:model.accent", f"参数 ({len(params)})"),
                *(("", line) for line in params[:4]),
            ]
            if len(params) > 4:
                rows.append(("class:dim", f"… 另有 {len(params) - 4} 项"))
        else:
            rows = [
                ("bold", f"{item.name}  ·  {marker} {status}  ·  {item.spec.action_level}"),
                ("", item.spec.summary), (style, reason),
                ("class:dim", f"{usage}  ·  {len(item.spec.params)} 个参数"),
            ]
        return [(tone, ("\n" if index else "") + _fit_cells(value, columns))
                for index, (tone, value) in enumerate(rows)]

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
        if browsing_groups:
            choose_group(max(0, min(len(groups) - 1, group + delta)))
        else:
            choose_row(selected + delta)

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
        scope = _fit_cells(groups[group][1], list_width() // 4) + " / " if not wide() and group else ""
        return [("class:model.accent", " " + scope + "搜索 › ")]

    def placeholder():
        room = list_width() - _cells(search_label()[0][1]) - _cells(counter()[0][1]) - 1
        if search.text or room < 5:
            return []
        return [("class:dim", _fit_cells("工具名、说明、参数或来源", room))]

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
        return key_hints([("Enter", "查看列表" if browsing_groups else "完整详情"), ("Esc", "返回"), ("F1", "按键"),
                          ("↑↓", "浏览"), ("←→", "分栏"), ("Tab", "筛选")], width())

    def rule():
        return Window(height=1, char="─", style="class:model.rule")

    groups_visible = Condition(lambda: wide() or browsing_groups)
    tools_visible = Condition(lambda: wide() or not browsing_groups)
    table = VSplit([HSplit([Window(Headings(), height=1, style="class:model.heading"), tool_window]),
                    HSplit([Window(height=1, style="class:model.heading"), track])])

    def divider():
        return VSplit([Window(width=1), Window(width=1, char="│", style="class:model.rule"), Window(width=1)])

    bottom_preview = roomy & ~side_details & ~Condition(lambda: help_open)
    body = HSplit([
        line(header), rule(),
        VSplit([
            ConditionalContainer(HSplit([line([("class:model.accent", " 筛选")]), sidebar],
                                        width=lambda: side_width if wide() else None), filter=groups_visible),
            ConditionalContainer(divider(), filter=wide),
            ConditionalContainer(HSplit([search_bar, table]), filter=tools_visible),
            ConditionalContainer(divider(), filter=side_preview),
            ConditionalContainer(Window(FormattedTextControl(details), width=detail_width, wrap_lines=False,
                                        always_hide_cursor=True), filter=side_preview),
        ]),
        rule(),
        ConditionalContainer(line(details, 4), filter=bottom_preview),
        ConditionalContainer(line(help_rows, 2), filter=Condition(lambda: help_open)),
        line(footer),
    ])
    app = _panel_app(body, "工具控制中心", keys, field, plain=plain, expanded=True)
    app.before_render += lambda _: scrollbar.begin_frame()
    app.after_render += lambda current: scrollbar.finish_frame(current, track)
    return run_panel(app)


__all__ = ["select_tool"]
