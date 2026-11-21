"""终端会话视口：稳定块缓存、按行读取与鼠标选择。"""
from __future__ import annotations

import base64
import os
import re
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, fragment_list_to_text, fragment_list_width, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth


class TranscriptControl(UIControl):
    """每帧只索引已有行；只有变动块或窗口宽度改变才重新解析 ANSI。"""

    def __init__(self, owner: Any):
        self.owner = owner
        self.rows: list[list[tuple[str, str]]] = []
        self.starts: list[int] = []
        self.width = 0
        self.viewport_height = 1
        self._last_height = 1
        self.dirty = 0
        self._blocks: dict[int, tuple[Any, list]] = {}
        self._changed: set[int] = set()
        self.anchor: tuple[int, int] | None = None
        self.head: tuple[int, int] | None = None
        self.dragging = False

    def invalidate_from(self, index: int) -> None:
        self.dirty = min(self.dirty, index)
        self._changed.add(index)
        self.owner._fragments = None

    def sync(self, width: int) -> None:
        owner = self.owner
        with owner._content_lock:
            can_shift = self.width == width and self._last_height == self.viewport_height
            self._last_height = self.viewport_height
            if self.width != width:
                self.width, self.dirty = width, 0
                self._blocks.clear()
                self.clear_selection()
            index = min(self.dirty, len(owner._chunks))
            if index == len(owner._chunks) and len(self.starts) == index:
                return
            owner._fragments = None
            previous = owner._rendered_lines
            old_start = self.viewport_start()
            old_visible = self.rows[old_start:old_start + self.viewport_height]
            offset = self.starts[index] if index < len(self.starts) else len(self.rows)
            del self.rows[offset:]
            del self.starts[index:]
            active_blocks = {id(chunk) for chunk in owner._chunks}
            self._blocks = {key: value for key, value in self._blocks.items() if key in active_blocks}
            for position, chunk in enumerate(owner._chunks[index:], index):
                self.starts.append(len(self.rows))
                cached = self._blocks.get(id(chunk))
                if cached is not None and cached[0] is chunk and position not in self._changed:
                    self.rows.extend(cached[1])
                    continue
                if isinstance(chunk, StreamingAnswer):
                    block_rows = chunk.rows_at_width(width)
                else:
                    fragments = chunk.fragments_at_width(width) if hasattr(chunk, "fragments_at_width") else None
                    if fragments is None:
                        fragments = _ansi_fragments(owner._chunk_text(chunk, width))
                    block_rows = _fragment_rows(fragments, width)
                self._blocks[id(chunk)] = (chunk, block_rows)
                self.rows.extend(block_rows)
            self._changed.clear()
            self.dirty = len(owner._chunks)
            owner._rendered_lines = owner._line_count = max(1, len(self.rows))
            if owner._follow_latest and not self.dragging:
                owner._scrollback = 0
            elif owner._scrollback or self.anchor is not None:
                owner._scrollback = max(0, owner._scrollback + owner._rendered_lines - previous)
            owner._scrollback = min(owner._scrollback, self.max_scrollback())
            new_start = self.viewport_start()
            delta = new_start - old_start
            # 尾随新增行时先搬运稳定视口，差分只写新露出的行。
            # 宽度/高度变化或旧行改写时由普通屏幕差分处理。
            if (can_shift and not owner._scrollback and self.anchor is None and 0 < delta < self.viewport_height
                    and len(old_visible) == self.viewport_height
                    and old_visible[delta:] == self.rows[new_start:new_start + self.viewport_height - delta]):
                self.precondition_scroll(old_start, new_start)

    def create_content(self, width: int, height: int) -> UIContent:
        self.viewport_height = max(1, height)
        self.sync(max(1, width))
        return UIContent(
            get_line=self.get_line, line_count=max(1, len(self.rows)),
            cursor_position=Point(x=0, y=self.owner._cursor_line()), show_cursor=False,
        )

    def viewport_start(self) -> int:
        """当前可见区首行；排版后滚动只改变这个整数。"""
        return max(0, self.owner._rendered_lines - self.viewport_height - self.owner._scrollback)

    def max_scrollback(self) -> int:
        """只允许滚到首屏，避免顶部仍有一整屏无画面变化的空行程。"""
        return max(0, self.owner._rendered_lines - self.viewport_height)

    def precondition_scroll(self, old_start: int, new_start: int) -> bool:
        """用终端滚动区域搬运不变行，并同步 prompt_toolkit 的旧屏。

        下一帧差分只需绘制新露出的行。缺少真实 VT 屏幕、首次渲染、
        跨越整屏或旧式 Win32 Console 时安全退回普通差分重绘。
        """
        delta = new_start - old_start
        info = getattr(getattr(self.owner, "output_window", None), "render_info", None)
        renderer = getattr(getattr(self.owner, "app", None), "renderer", None)
        screen = getattr(renderer, "_last_screen", None)
        output = getattr(getattr(self.owner, "app", None), "output", None)
        if not delta or info is None or screen is None or output is None or not hasattr(output, "write_raw"):
            return False
        height = int(getattr(info, "window_height", 0) or 0)
        top = int(getattr(info, "y_offset", 0) or 0)
        count = abs(delta)
        if height <= 1 or count >= height or top < 0 or type(output).__module__.endswith(".win32"):
            return False
        bottom = top + height - 1
        direction = "S" if delta > 0 else "T"  # SU 向最新；SD 向历史。
        try:
            output.write_raw(
                f"\x1b[?2026h\x1b7\x1b[{top + 1};{bottom + 1}r"
                f"\x1b[{top + 1};1H\x1b[{count}{direction}\x1b[r\x1b8\x1b[?2026l"
            )
        except Exception:
            return False
        _shift_screen_rows(screen.data_buffer, top, bottom, delta)
        _shift_screen_rows(screen.zero_width_escapes, top, bottom, delta)
        return True

    def get_line(self, index: int):
        row = self.rows[index] if 0 <= index < len(self.rows) else [("", "")]
        if self.anchor is None or self.head is None or self.anchor == self.head:
            return row
        start, end = sorted((self.anchor, self.head))
        if not start[0] <= index <= end[0]:
            return row
        left = start[1] if index == start[0] else 0
        right = end[1] if index == end[0] else len(fragment_list_to_text(row))
        selected, offset = [], 0
        for style, text, *_ in row:
            before, after = max(0, left - offset), max(0, right - offset)
            for content, active in ((text[:before], False), (text[before:after], True), (text[max(before, after):], False)):
                if content:
                    selected.append((style + (" class:selection" if active else ""), content))
            offset += len(text)
        return selected

    def clear_selection(self) -> None:
        self.anchor = self.head = None
        self.dragging = False

    def selected_text(self) -> str:
        if self.anchor is None or self.head is None:
            return ""
        start, end = sorted((self.anchor, self.head))
        selected = []
        for index in range(start[0], min(len(self.rows), end[0] + 1)):
            text = fragment_list_to_text(self.rows[index])
            selected.append(text[start[1] if index == start[0] else 0:end[1] if index == end[0] else len(text)].rstrip())
        return "\n".join(selected)

    def copy_selection(self) -> bool:
        text = self.selected_text()
        if not text:
            return False
        self.owner.app.clipboard.set_text(text)
        copy_to_terminal(text, self.owner.app.output)
        return True

    def mouse_handler(self, event):
        if event.event_type == MouseEventType.SCROLL_UP:
            self.owner._scroll_by(3)
            return None
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self.owner._scroll_by(-3)
            return None
        if event.button == MouseButton.RIGHT and event.event_type == MouseEventType.MOUSE_UP:
            self.copy_selection()
            return None
        if event.button == MouseButton.LEFT:
            row = max(0, min(event.position.y, len(self.rows) - 1))
            text = fragment_list_to_text(self.rows[row]) if self.rows else ""
            position = (row, max(0, min(event.position.x, len(text))))
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self.anchor = self.head = position
                self.dragging = True
                self.owner._follow_latest = False
            elif event.event_type == MouseEventType.MOUSE_MOVE and self.dragging:
                self.head = position
            elif event.event_type == MouseEventType.MOUSE_UP and self.dragging:
                self.head, self.dragging = position, False
                self.copy_selection()
                if self.owner._scrollback == 0:
                    self.owner._follow_latest = True
            else:
                return NotImplemented
            self.owner.app.invalidate()
            return None
        return NotImplemented


class StreamingAnswer:
    """草稿较长时复用已排入的物理行，按到达顺序追加正文。"""

    MARKDOWN_PREVIEW_CHARS = 2048

    def __init__(self, text: str, *, color: bool, palette: str):
        self.color, self.palette = color, palette
        self.text = ""
        self._width = 0
        self._reset_rows()
        self.replace(text)

    def __len__(self) -> int:
        return len(self.text)

    def __str__(self) -> str:
        return self.text

    def replace(self, text: str) -> None:
        if not text.startswith(self.text):
            self._reset_rows()
        self.text = text

    def at_width(self, width: int) -> str:
        return "\n".join(fragment_list_to_text(row) for row in self.rows_at_width(width)) + "\n"

    def _reset_rows(self) -> None:
        self._rows: list[list[tuple[str, str]]] = []
        self._offset = 0
        self._tail = ""
        self._columns = 0

    def rows_at_width(self, width: int) -> list[list[tuple[str, str]]]:
        from .markdown_view import render_markdown_fragments
        from .output_styles import output_style
        from .text import clean_text

        if len(self.text) <= self.MARKDOWN_PREVIEW_CHARS:
            fragments = render_markdown_fragments(self.text or " ", color=self.color, palette=self.palette)
            return _fragment_rows([*fragments, ("", "\n\n")], width)
        if self._width != width:
            self._width = width
            self._reset_rows()
        style = output_style("text", self.palette, color=self.color)
        # ANSI 序列可能跨增量或行边界。出现时净化完整草稿，分块仍不解释命令。
        if "\x1b" in self.text:
            clean = clean_text(self.text.replace("\r\n", "\n").replace("\r", "\n"))
            return _fragment_rows([(style, clean + "\n\n")], width)
        end = len(self.text) - int(self.text.endswith("\r"))
        new_text = self.text[self._offset:end].replace("\r\n", "\n").replace("\r", "\n")
        for char in clean_text(new_text):
            cells = get_cwidth(char)
            if char == "\n" or self._tail and cells > 0 and self._columns + cells > width:
                self._rows.append([(style, self._tail)])
                self._tail = ""
                self._columns = 0
            if char != "\n":
                self._tail += char
                self._columns += cells
        self._offset = end
        rows = self._rows.copy()
        if self._tail:
            rows.append([(style, self._tail)])
        rows.append([])
        return rows


def _fragment_rows(fragments: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    lines = list(split_lines(fragments))
    if fragments and fragments[-1][1].endswith("\n"):
        lines.pop()
    rows = []
    for line in lines:
        rows.extend(_wrap_physical_line(line, width))
    return rows


_SGR = re.compile(r"(?:\x1b\[|\x9b)([0-9;]*)m")


def _ansi_fragments(value: str):
    """按样式段转换 Rich 的 SGR；其他控制序列仍使用原解析器。"""
    if not any(char in value for char in ("\x1b", "\x9b", "\x01", "\x02")):
        return [("", value)]
    state = ANSI("")
    if not hasattr(state, "_select_graphic_rendition") or not hasattr(state, "_create_style_string"):
        return to_formatted_text(ANSI(value))
    result, position, style = [], 0, ""
    for match in _SGR.finditer(value):
        text = value[position:match.start()]
        if any(char in text for char in ("\x1b", "\x9b", "\x01", "\x02")):
            return to_formatted_text(ANSI(value))
        if text:
            result.append((style, text))
        state._select_graphic_rendition([min(int(item or 0), 9999) for item in match.group(1).split(";")])
        style = state._create_style_string()
        position = match.end()
    tail = value[position:]
    if any(char in tail for char in ("\x1b", "\x9b", "\x01", "\x02")):
        return to_formatted_text(ANSI(value))
    if tail:
        result.append((style, tail))
    return result


def _wrap_physical_line(line, width: int):
    """把 ANSI 片段一次性折成物理行，滚动帧不再重复计算宽度。"""
    if fragment_list_width(line) <= width:
        return [line]
    rows, current, cells = [], [], 0
    for fragment in explode_text_fragments(line):
        style, text, *_ = fragment
        size = 0 if "[ZeroWidthEscape]" in style else get_cwidth(text)
        if current and size > 0 and cells + size > width:
            rows.append(current)
            current, cells = [], 0
        current.append(fragment)
        cells += size
    rows.append(current)
    return rows


def _shift_screen_rows(buffer, top: int, bottom: int, delta: int) -> None:
    """让渲染器旧屏与已发送的 SU/SD 指令保持同一行布局。"""
    count = abs(delta)
    if delta > 0:
        for row in range(top, bottom - count + 1):
            buffer[row] = buffer[row + count].copy()
        for row in range(bottom - count + 1, bottom + 1):
            buffer[row].clear()
    else:
        for row in range(bottom, top + count - 1, -1):
            buffer[row] = buffer[row - count].copy()
        for row in range(top, top + count):
            buffer[row].clear()


def copy_to_terminal(text: str, output: Any) -> None:
    """Windows 写 Unicode 剪贴板；其他终端使用 OSC 52，不启动外部命令。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
        kernel.GlobalAlloc.argtypes, kernel.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
        kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
        kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
        user.SetClipboardData.argtypes, user.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.OpenClipboard.argtypes = [wintypes.HWND]
        user.DestroyWindow.argtypes = [wintypes.HWND]
        # EmptyClipboard/SetClipboardData 需要有效 owner；隐藏 STATIC 窗口不接管焦点。
        window = user.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, None, None, None, None)
        if not window:
            return
        if not user.OpenClipboard(window):
            user.DestroyWindow(window)
            return
        handle = None
        try:
            data = (text + "\0").encode("utf-16-le")
            handle = kernel.GlobalAlloc(0x0042, len(data))
            pointer = kernel.GlobalLock(handle) if handle else None
            if not pointer:
                return
            ctypes.memmove(pointer, data, len(data))
            kernel.GlobalUnlock(handle)
            if user.EmptyClipboard() and user.SetClipboardData(13, handle):
                handle = None  # 成功后由系统管理内存。
        finally:
            if handle:
                kernel.GlobalFree(handle)
            user.CloseClipboard()
            user.DestroyWindow(window)
    else:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        output.write_raw(f"\x1b]52;c;{encoded}\x07")
        output.flush()
