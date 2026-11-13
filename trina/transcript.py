"""终端会话视口：稳定块缓存、按行读取与鼠标选择。"""
from __future__ import annotations

import base64
import os
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
        self.dirty = 0
        self.anchor: tuple[int, int] | None = None
        self.head: tuple[int, int] | None = None
        self.dragging = False

    def invalidate_from(self, index: int) -> None:
        self.dirty = min(self.dirty, index)
        self.owner._fragments = None

    def sync(self, width: int) -> None:
        owner = self.owner
        with owner._content_lock:
            if self.width != width:
                self.width, self.dirty = width, 0
                self.clear_selection()
            index = min(self.dirty, len(owner._chunks))
            if index == len(owner._chunks) and len(self.starts) == index:
                return
            owner._fragments = None
            previous = owner._rendered_lines
            offset = self.starts[index] if index < len(self.starts) else len(self.rows)
            del self.rows[offset:]
            del self.starts[index:]
            for chunk in owner._chunks[index:]:
                self.starts.append(len(self.rows))
                rendered = owner._chunk_text(chunk, width)
                lines = list(split_lines(to_formatted_text(ANSI(rendered))))
                if rendered.endswith("\n"):
                    lines.pop()
                for line in lines:
                    self.rows.extend(_wrap_physical_line(line, width))
            self.dirty = len(owner._chunks)
            owner._rendered_lines = owner._line_count = max(1, len(self.rows))
            if owner._scrollback or self.anchor is not None:
                owner._scrollback = max(0, owner._scrollback + owner._rendered_lines - previous)
            owner._scrollback = min(owner._scrollback, self.max_scrollback())

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
            elif event.event_type == MouseEventType.MOUSE_MOVE and self.dragging:
                self.head = position
            elif event.event_type == MouseEventType.MOUSE_UP and self.dragging:
                self.head, self.dragging = position, False
                self.copy_selection()
            else:
                return NotImplemented
            self.owner.app.invalidate()
            return None
        return NotImplemented


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
