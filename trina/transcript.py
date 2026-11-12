"""终端会话视口：稳定块缓存、按行读取与鼠标选择。"""
from __future__ import annotations

import base64
import os
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, fragment_list_to_text, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType


class TranscriptControl(UIControl):
    """每帧只索引已有行；只有变动块或窗口宽度改变才重新解析 ANSI。"""

    def __init__(self, owner: Any):
        self.owner = owner
        self.rows: list[list[tuple[str, str]]] = []
        self.starts: list[int] = []
        self.width = 0
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
                self.rows.extend(lines)
            self.dirty = len(owner._chunks)
            owner._rendered_lines = owner._line_count = max(1, len(self.rows))
            if owner._scrollback or self.anchor is not None:
                owner._scrollback = max(0, owner._scrollback + owner._rendered_lines - previous)
            owner._scrollback = min(owner._scrollback, owner._rendered_lines - 1)

    def create_content(self, width: int, height: int) -> UIContent:
        self.sync(max(1, width))
        return UIContent(
            get_line=self.get_line, line_count=max(1, len(self.rows)),
            cursor_position=Point(x=0, y=self.owner._cursor_line()), show_cursor=False,
        )

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
