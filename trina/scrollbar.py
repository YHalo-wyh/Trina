"""终端滚动条的视口几何和拖动合帧。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType


@dataclass(frozen=True)
class _Track:
    height: int
    thumb: int
    maximum: int

    @property
    def travel(self) -> int:
        return self.height - self.thumb

    def start(self, offset: int) -> int:
        if self.maximum == 0:
            return 0
        return round(min(self.maximum, max(0, offset)) * self.travel / self.maximum)

    def offset(self, top: int) -> int:
        return round(min(self.travel, max(0, top)) * self.maximum / self.travel)


class _DragMouseRow:
    def __init__(self, row, bar: ScrollbarControl, top: int):
        self.row, self.bar, self.top = row, bar, top

    def __getitem__(self, column: int):
        def dispatch(event: MouseEvent):
            if self.bar.dragging and event.button in (MouseButton.LEFT, MouseButton.UNKNOWN) and event.event_type in (
                    MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
                point = Point(event.position.x, event.position.y - self.top)
                return self.bar.mouse_handler(MouseEvent(point, event.event_type, event.button, event.modifiers))
            return self.row[column](event)
        return dispatch


class _DragMouseRows:
    """在终端完成坐标解码后转发已抓住滑块的移动和释放。"""

    def __init__(self, rows, bar: ScrollbarControl, top: int):
        self.rows, self.bar, self.top = rows, bar, top

    def __getitem__(self, row: int):
        return _DragMouseRow(self.rows[row], self.bar, self.top) if self.bar.dragging else self.rows[row]


class ScrollbarControl(UIControl):
    """鼠标移动只更新待绘制位置，视口与滑块由同一帧提交。"""

    def __init__(self, viewport: Callable[[], tuple[int, int, int]], seek: Callable[[int, int], None],
                 wheel: Callable[[int], None], *, thumb_style: str, track_style: str,
                 hide_when_fits: bool = False, on_press: Callable[[], object] | None = None):
        self._viewport = viewport  # 可见行数、最大偏移、首行偏移
        self._seek = seek
        self._wheel = wheel
        self._on_press = on_press
        self._track_row = [(track_style, "│")]
        self._thumb_row = [(thumb_style, "█")]
        self._empty_row = [("", "")]
        self._hide_when_fits = hide_when_fits
        self._painted: _Track | None = None
        self._drag_track: _Track | None = None
        self._last_y = self._grab = self._thumb_start = 0
        self._last_offset: int | None = None
        self._pending: tuple[int, int] | None = None

    @property
    def dragging(self) -> bool:
        return self._drag_track is not None

    def cancel_drag(self) -> None:
        self._drag_track = None
        self._last_offset = None
        self._pending = None

    def begin_frame(self) -> None:
        self.apply_pending()

    def finish_frame(self, app, window) -> None:
        """用新帧的轨道位置捕获越过滚动条的拖动事件。"""
        top = getattr(window.render_info, "_y_offset", None)
        if top is not None and window in app.layout.visible_windows:
            table = app.renderer.mouse_handlers
            if not isinstance(table.mouse_handlers, _DragMouseRows):
                table.mouse_handlers = _DragMouseRows(table.mouse_handlers, self, top)

    def apply_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            self._seek(*pending)

    def create_content(self, width: int, height: int) -> UIContent:
        visible, maximum, offset = self._viewport()
        if self._drag_track is not None and self._drag_track.height != height:
            self.cancel_drag()
        # 拖动期间固定轨道比例，实时追加的行在释放后再计入滑块大小。
        if height > 1 and maximum:
            proportional = max(1, height * visible // max(1, visible + maximum))
            minimum = min(height - 1, max(3, height // 10))
            thumb = max(proportional, minimum)
        else:
            thumb = 0
        track = self._drag_track or _Track(height, thumb, maximum)
        start = track.start(offset)
        self._painted, self._thumb_start = track, start
        end = start + track.thumb
        track_row = self._empty_row if self._hide_when_fits and not track.thumb else self._track_row
        return UIContent(get_line=lambda y: self._thumb_row if start <= y < end else track_row,
                         line_count=height, show_cursor=False)

    def _queue(self, y: int, track: _Track):
        self._last_y = y
        offset = track.offset(y - self._grab)
        if self._last_offset == offset:
            return NotImplemented
        self._last_offset = offset
        redraw = self._pending is None
        self._pending = offset, track.maximum
        # prompt_toolkit 会调度重绘。已经有一帧待绘制时只覆盖坐标。
        return None if redraw else NotImplemented

    def mouse_handler(self, event: MouseEvent):
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.cancel_drag()
            self._wheel(1 if event.event_type == MouseEventType.SCROLL_UP else -1)
            return None
        if event.button not in (MouseButton.LEFT, MouseButton.UNKNOWN):
            return NotImplemented
        y = event.position.y
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            self.cancel_drag()
            track = self._painted
            if track is None or not track.thumb or not track.travel:
                return NotImplemented
            if self._on_press is not None:
                self._on_press()
            self._drag_track = track
            self._last_y = y
            grab = y - self._thumb_start
            if 0 <= grab < track.thumb:
                self._grab = grab
                return NotImplemented
            self._grab = track.thumb // 2
            return self._queue(y, track)
        track = self._drag_track
        if track is None:
            return NotImplemented
        if event.event_type == MouseEventType.MOUSE_MOVE and y != self._last_y:
            return self._queue(y, track)
        if event.event_type == MouseEventType.MOUSE_UP:
            if y != self._last_y:
                self._queue(y, track)
            # 同一输入批次里的后续按键须能看到释放时的视口。
            self.apply_pending()
            self._drag_track = None
            return None
        return NotImplemented
