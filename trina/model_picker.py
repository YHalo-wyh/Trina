"""终端模型目录，按连接和本地能力筛选。"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from .models import CatalogModel
from .model_names import base_name
from .model_picker_view import columns, directory_header, key_hints, marked_text, padded, tokens
from .providers import ProviderPreset


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
    source: str = "已配置"
    meta: CatalogModel = CatalogModel()


def _subsequence(term: str, text: str) -> bool:
    chars = iter(text)
    return all(char in chars for char in term)


def select_model(choices: list[ModelChoice], *, plain: bool = False, discover=None,
                 recent: list[tuple[str, str]] | None = None,
                 presets: tuple[ProviderPreset, ...] = ()) -> ModelChoice | ProviderPreset | Literal["settings"] | None:
    """在搜索框内直接导航，确认时才修改会话。"""
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
    matches = None
    recent_order = {key: i for i, key in enumerate(recent or ())}
    divider = 0
    help_open = False

    def directory(items):
        providers = {item.connection_id: clean_text(item.connection_label) for item in items}
        counts = Counter(item.connection_id for item in items)
        groups = [(None, "所有模型", len(items)), *((key, label, counts[key]) for key, label in providers.items()),
                  *((preset, preset.id, None) for preset in presets)]
        index = [(item, " ".join((item.connection_id, item.connection_label, item.model, base_name(item.model))).casefold())
                 for item in items]
        return groups, index

    groups, index = directory(choices)
    browsing_providers = False
    load_task = None
    notice = ""
    active = next((item for item in choices if item.current), None)

    def filter_names(items):
        kinds = sorted({item.meta.kind for item in items})
        return ["全部模型", "可调思考", "窗口 ≥500K"] + (kinds if kinds != ["chat"] else [])

    filters = filter_names(choices)
    group = kind = selected = 0
    visible = choices[:]
    highlight = "reverse" if plain else "class:model.row"
    active_tab = "reverse" if plain else "class:model.tab"
    keys = KeyBindings()

    def refresh(_buffer=None):
        nonlocal visible, selected, browsing_providers, matches, divider
        if _buffer is not None:
            browsing_providers = False
        terms = search.text.casefold().split()
        matches = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE) if terms else None
        provider = groups[group][0]
        direct, fuzzy = [], []
        for item, text in index:
            if (provider is not None and item.connection_id != provider
                    or kind == 1 and len(item.levels) <= 1 or kind == 2 and item.context_window < 500_000
                    or kind >= 3 and item.meta.kind != filters[kind]):
                continue
            if all(term in text for term in terms):
                direct.append(item)
            elif all(_subsequence(term, text) for term in terms):
                fuzzy.append(item)
        visible = direct + fuzzy
        divider = 0
        if not terms and kind == 0:
            def rank(item):
                return -1 if item.current else recent_order.get((item.connection_id, item.model), len(recent_order))
            visible.sort(key=rank)
            count = sum(rank(item) < len(recent_order) for item in visible)
            if 1 < count < len(visible):
                divider = count
        selected = 0

    refresh()
    search.on_text_changed += refresh

    def width():
        return max(1, get_app().output.get_size().columns - 4)

    wide = Condition(lambda: width() >= 70)
    roomy = Condition(lambda: get_app().output.get_size().rows >= 15)
    side_width = 24

    def list_width():
        return max(1, width() - (side_width + 3 if wide() else 0))

    def header():
        if notice:
            value, style = notice, "class:dim"
        elif active:
            value, style = "● 当前  " + base_name(active.model), "class:model.live"
        else:
            value, style = "", "class:dim"
        return directory_header("✧", "模型目录", value, width(), style=style)

    def sidebar_title():
        style = "class:model.accent" if browsing_providers else "class:dim"
        return [(style, " 供应商"), ("class:dim", "  ·  " + str(len(groups) - 1))]

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

    def choose_provider(pos):
        nonlocal browsing_providers
        browsing_providers = True
        pick_group(pos)

    class ProviderRows(UIControl):
        def create_content(self, width: int, height: int) -> UIContent:
            # 清单仍按视口绘制。预设排在已连接之后，标记使用不同字形。
            def row(pos):
                key, label, count = groups[pos]
                selected_group = pos == group
                focused = selected_group and browsing_providers
                base = highlight if focused else ""
                status = "未连接" if count is None and width >= 29 else "" if count is None else str(count)
                marker = "◦" if isinstance(key, ProviderPreset) else "●" if pos else "·"
                prefix = ("▸" if selected_group else " ") + f" {marker} "
                name = padded(label, max(0, width - _cells(prefix) - _cells(status) - 2))
                style = "class:model.accent" if selected_group else "class:dim" if count is None else ""
                return [(base + " " + style, prefix + name), (base + " class:dim", status + " ")]
            return UIContent(get_line=row, line_count=len(groups), cursor_position=Point(x=0, y=group), show_cursor=False)

        def mouse_handler(self, event):
            if event.event_type == MouseEventType.MOUSE_UP and 0 <= event.position.y < len(groups):
                choose_provider(event.position.y)
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                choose_provider(max(0, min(len(groups) - 1, group +
                                (-1 if event.event_type == MouseEventType.SCROLL_UP else 1))))
            else:
                return NotImplemented

    def model_to_row(index: int) -> int:
        return index + int(bool(divider and index >= divider))

    def row_to_model(index: int) -> int:
        return index - int(bool(divider and index > divider))

    class ColumnHeadings(UIControl):
        def create_content(self, width: int, height: int) -> UIContent:
            first, *rest = columns(width)
            labels = "    " + padded(first[0], first[1])
            for name, size in rest:
                labels += "  " + padded(name, size, right=name in ("窗口", "输出"))
            return UIContent(get_line=lambda _: [("class:model.heading", labels)], line_count=1, show_cursor=False)

    class ModelRows(UIControl):
        def create_content(self, width: int, height: int) -> UIContent:
            first, *rest = columns(width)
            def row(y):
                if divider and y == divider:
                    label = " ── 其他模型 "
                    return [("class:model.rule", _fit_cells(label, width) + "─" * max(0, width - _cells(label)))]
                pos = row_to_model(y)
                if not visible:
                    if isinstance(groups[group][0], ProviderPreset):
                        message = "此供应商尚未连接 · Enter 添加"
                    else:
                        message = "没有匹配的模型 · 修改关键词或筛选项" if choices else "尚无模型 · F2 配置连接"
                    return [("class:dim", _fit_cells(message, width))]
                item = visible[pos]
                focused = pos == selected and not browsing_providers
                base = highlight if focused else ""
                title = padded(item.model, first[1])
                parts = [(base + " class:model.accent", "▸ " if focused else "  "),
                         (base + " class:model.live", "● " if item.current else "  ")]
                parts.extend(marked_text(title, matches, base, "class:model.accent underline"))
                values = {"连接": item.connection_label, "类型": item.meta.kind,
                          "窗口": tokens(item.meta.context), "输出": tokens(item.meta.output)}
                for name, size in rest:
                    style = base + (" class:model.accent" if name == "类型" and item.meta.kind != "chat" else " class:dim")
                    parts.append((style, "  " + padded(values[name], size, right=name in ("窗口", "输出"))))
                return parts
            return UIContent(get_line=row, line_count=max(1, len(visible) + bool(divider)),
                             cursor_position=Point(x=0, y=model_to_row(selected)), show_cursor=False)

        def mouse_handler(self, event):
            nonlocal browsing_providers
            if event.event_type == MouseEventType.MOUSE_UP:
                browsing_providers = False
                if not divider or event.position.y != divider:
                    pick_row(row_to_model(event.position.y))
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                pick_row(selected + (-1 if event.event_type == MouseEventType.SCROLL_UP else 1))
            else:
                return NotImplemented

    def tabs():
        # 从选中的筛选项向两侧展开，终端缩窄时仍能看到当前项。
        compact = {"全部模型": "全部", "窗口 ≥500K": "≥500K 窗口"}
        labels = [f" {compact.get(name, name)} " for name in filters]
        lo, hi = kind, kind + 1
        room = list_width() - _cells(labels[kind]) - 4
        while lo and room >= _cells(labels[lo - 1]) + 1:
            lo -= 1
            room -= _cells(labels[lo]) + 1
        while hi < len(labels) and room >= _cells(labels[hi]) + 1:
            room -= _cells(labels[hi]) + 1
            hi += 1
        result = [("class:dim", "‹ ")] if lo else []
        for pos in range(lo, hi):
            result.append((active_tab if pos == kind else "class:dim", labels[pos],
                           mouse(pos, pick_kind, pick_kind, lambda: kind)))
            result.append(("", " "))
        if hi < len(labels):
            result.append(("class:dim", "›"))
        return result

    def details():
        preset = groups[group][0]
        if isinstance(preset, ProviderPreset):
            return [("bold", _fit_cells(preset.name, width())), ("class:dim", "\n按 Enter 打开连接表单")]
        if not visible:
            return []
        item = visible[selected]
        name = clean_text(base_name(item.model))
        marker = "● 使用中" if item.current else "预览"
        title = _fit_cells(name, width() - _cells(marker) - 2)
        facts = [item.connection_label, item.meta.kind]
        if item.meta.context:
            facts.append(f"供应商窗口 {item.meta.context:,}" if wide() else f"窗口 {tokens(item.meta.context)}")
        if item.meta.output:
            facts.append(f"最大输出 {item.meta.output:,}" if wide() else f"输出 {tokens(item.meta.output)}")
        if item.meta.vision:
            facts.append("图像输入")
        if item.meta.kind == "chat":
            settings = [item.source, item.api, f"本地窗口 {tokens(item.context_window)}（{'手动' if item.manual_context else '自动'}）"]
            if len(item.levels) > 1:
                settings.append("思考 " + "/".join(item.levels))
            detail = "  ·  ".join(filter(None, settings))
        else:
            detail = "仅供浏览 · 当前会话需要聊天模型"
        return [("bold", title + "  "), ("class:model.live" if item.current else "class:dim", marker),
                ("", "\n" + _fit_cells("  ·  ".join(facts), width())),
                ("class:dim", "\n" + _fit_cells(detail, width()))]

    @keys.add("up")
    @keys.add("down")
    @keys.add("pageup")
    @keys.add("pagedown")
    def move(event):
        window = sidebar if browsing_providers else model_window
        page = max(1, window.render_info.window_height if window.render_info else 1)
        delta = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}[event.key_sequence[-1].key.value]
        if browsing_providers:
            pick_group(max(0, min(len(groups) - 1, group + delta)))
        else:
            pick_row(selected + delta)

    @keys.add("left", filter=Condition(lambda: not search.text))
    def focus_providers(event):
        nonlocal browsing_providers
        browsing_providers = True

    @keys.add("right", filter=Condition(lambda: browsing_providers))
    def focus_models(event):
        nonlocal browsing_providers
        browsing_providers = False

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
        nonlocal browsing_providers, notice
        preset = groups[group][0]
        if isinstance(preset, ProviderPreset):
            event.app.exit(result=preset)
        elif browsing_providers:
            browsing_providers = False
        elif visible and visible[selected].meta.kind == "chat":
            event.app.exit(result=visible[selected])
        elif visible:
            notice = f"{visible[selected].meta.kind} · 当前会话需要聊天模型"

    @keys.add("f2")
    def settings(event):
        event.app.exit(result="settings")

    @keys.add("f1")
    def show_help(event):
        nonlocal help_open
        help_open = not help_open

    @keys.add("escape", filter=Condition(lambda: help_open))
    def hide_help(event):
        nonlocal help_open
        help_open = False

    @keys.add("escape", filter=Condition(lambda: not help_open))
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    async def load_catalog(force):
        nonlocal choices, groups, group, index, selected, notice, filters, kind, active
        try:
            async for updated, error in discover(force):
                old_group = groups[group][0]
                old = visible[selected] if visible else None
                active_filter = filters[kind]
                choices = updated
                active = next((item for item in updated if item.current), None)
                filters = filter_names(choices)
                kind = filters.index(active_filter) if active_filter in filters else 0
                groups, index = directory(choices)
                group = next((i for i, (key, _, _) in enumerate(groups) if key == old_group), 0)
                refresh()
                if old:
                    selected = next((i for i, row in enumerate(visible)
                                     if (row.connection_id, row.model) == (old.connection_id, old.model)), 0)
                notice = error
                app.invalidate()
        except (OSError, ValueError):
            notice = "目录读取失败 · F5 重试"
        finally:
            app.invalidate()

    def start_load(force=False):
        nonlocal load_task, notice
        if discover and (load_task is None or load_task.done()):
            notice = "正在读取目录…"
            load_task = app.create_background_task(load_catalog(force))
            app.invalidate()

    @keys.add("f5")
    def reload_catalog(event):
        start_load(True)

    def search_label():
        show_scope = not wide() and (group != 0 or browsing_providers)
        scope = _fit_cells(groups[group][1], max(4, list_width() // 4)) + " / " if show_scope else ""
        label = "连接 › " if browsing_providers and not wide() else "搜索 › "
        return [("class:model.accent", " " + scope + label)]

    def placeholder():
        taken = _cells(search_label()[0][1]) + _cells(counter()[0][1])
        if search.text or list_width() - taken < _cells("模型 ID 或连接名") + 1:
            return []
        return [("class:dim", "模型 ID 或连接名")]

    field = BufferControl(buffer=search, input_processors=[BeforeInput(search_label), AfterInput(placeholder)])
    model_window = Window(ModelRows(),
                          wrap_lines=False, always_hide_cursor=True)

    def viewport() -> tuple[int, int, int]:
        height = model_window.render_info.window_height if model_window.render_info else 1
        return height, max(0, len(visible) + bool(divider) - height), model_window.vertical_scroll

    def seek(offset: int, _maximum: int) -> None:
        height, maximum, first = viewport()
        offset = min(offset, maximum)
        row = offset + min(height - 1, max(0, model_to_row(selected) - first))
        pick_row(row_to_model(row))
        model_window.vertical_scroll = offset

    scrollbar = ScrollbarControl(viewport, seek, lambda step: pick_row(selected - step),
                                 thumb_style="class:panel.border", track_style="class:dim", hide_when_fits=True)
    track = Window(scrollbar, width=1)
    sidebar = Window(ProviderRows(), wrap_lines=False, always_hide_cursor=True)
    def line(content, height=1):
        return Window(FormattedTextControl(content), height=height, wrap_lines=False)

    def footer():
        if isinstance(groups[group][0], ProviderPreset):
            accept = "连接"
        elif browsing_providers:
            accept = "打开"
        else:
            accept = "切换" if visible and visible[selected].meta.kind == "chat" else "浏览"
        pairs = [("Enter", accept), ("Esc", "返回"), ("F1", "按键"), ("↑↓", "浏览"),
                 ("←→", "分栏"), ("F5", "刷新"), ("F2", "设置")]
        return key_hints(pairs, width())

    def shortcuts():
        return [
            ("class:model.key", "Tab / Shift+Tab"), ("", " 轮换连接\n"),
            ("class:model.key", "Alt+←/→"), ("", " 切换筛选    "), ("class:model.key", "PgUp/Dn"), ("", " 翻页\n"),
            ("class:model.key", "F5"), ("", " 刷新目录    "), ("class:model.key", "F2"), ("", " 连接设置")]

    def counter():
        return [("class:dim", f" {selected + 1 if visible else 0} / {len(visible)} ")]

    search_bar = VSplit([Window(field, height=1, wrap_lines=False),
                         Window(FormattedTextControl(counter), dont_extend_width=True, height=1)],
                        style="class:model.search")
    def rule():
        return Window(height=1, char="─", style="class:model.rule")

    def breathing_room():
        return ConditionalContainer(Window(height=1), filter=Condition(lambda: get_app().output.get_size().rows >= 20))
    table = VSplit([HSplit([Window(ColumnHeadings(), height=1, style="class:model.heading"), model_window]),
                    HSplit([Window(height=1, style="class:model.heading"), track])])
    providers_visible = Condition(lambda: wide() or browsing_providers)
    models_visible = Condition(lambda: wide() or not browsing_providers)
    body = HSplit([
        breathing_room(), line(header), rule(),
        VSplit([
            ConditionalContainer(HSplit([line(sidebar_title), sidebar], width=lambda: side_width if wide() else None),
                                 filter=providers_visible),
            ConditionalContainer(VSplit([Window(width=1), Window(width=1, char="│", style="class:model.rule"),
                                         Window(width=1)]), filter=wide),
            ConditionalContainer(HSplit([search_bar, line(tabs), breathing_room(), table]), filter=models_visible),
        ]),
        rule(),
        ConditionalContainer(line(details, 3), filter=roomy & Condition(lambda: not help_open)),
        ConditionalContainer(line(shortcuts, 3), filter=Condition(lambda: help_open)),
        line(footer), breathing_room(),
    ])
    app = _panel_app(body, "切换模型", keys, field, plain=plain, expanded=True)
    app.before_render += lambda _: scrollbar.begin_frame()
    app.after_render += lambda current: scrollbar.finish_frame(current, track, _REDRAW_INTERVAL)
    if discover:
        app.pre_run_callables.append(start_load)
    return run_panel(app)
