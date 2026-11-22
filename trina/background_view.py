"""控制台的后台任务视图和事件等待，任务状态仍由原执行器提供。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


ACTIVE = frozenset({"pending", "running", "stopping"})
STATUS_LABELS = {
    "pending": "排队",
    "running": "运行中",
    "stopping": "停止中",
    "completed": "完成",
    "failed": "Error",
    "cancelled": "已取消",
    "timed_out": "超时",
    "interrupted": "已中断",
    "blocked": "受阻",
    "skipped": "跳过",
}


@dataclass(frozen=True)
class BackgroundRow:
    id: str
    owner: str
    status: str
    elapsed: float
    detail: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.owner, self.id


@dataclass
class _Wait:
    mode: str
    keys: frozenset[tuple[str, str]]
    started: float
    timeout: float
    changed: threading.Event = field(default_factory=threading.Event)


class BackgroundWait:
    """一次只保持一个人工等待；任务通知只唤醒等待线程。"""

    def __init__(self, read: Callable[[], list[BackgroundRow]],
                 on_done: Callable[[str, list[BackgroundRow]], None]) -> None:
        self._read = read
        self._on_done = on_done
        self._lock = threading.Lock()
        self._current: _Wait | None = None
        self._closed = False

    def start(self, mode: str, rows: list[BackgroundRow], timeout: float) -> None:
        if mode not in {"any", "all"} or not 0 <= timeout <= 3600 or not rows:
            raise ValueError("等待需要 any 或 all、至少一项任务和 0 到 3600 秒的超时")
        wait = _Wait(mode, frozenset(row.key for row in rows), time.monotonic(), timeout)
        with self._lock:
            if self._closed or self._current is not None:
                raise ValueError("当前无法开始等待，请先退出已有等待")
            self._current = wait
            thread = threading.Thread(target=self._run, args=(wait,), name="trina-background-wait", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self._current = None
                raise

    def activity(self) -> tuple[int, float] | None:
        with self._lock:
            if self._current is not None:
                return len(self._current.keys), time.monotonic() - self._current.started
        return None

    def changed(self) -> None:
        """由原执行器在发布新状态后调用。"""
        with self._lock:
            if self._current is not None:
                self._current.changed.set()

    def cancel(self) -> bool:
        with self._lock:
            wait, self._current = self._current, None
            if wait is not None:
                wait.changed.set()
            return wait is not None

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()

    def _run(self, wait: _Wait) -> None:
        while True:
            # 清除信号后再读原执行器。先到的通知已体现在快照里，后到的通知会唤醒等待。
            wait.changed.clear()
            try:
                rows = [row for row in self._read() if row.key in wait.keys]
            except (ValueError, OSError) as exc:
                reason, rows = f"读取后台任务失败 · {exc}", []
            else:
                remaining = wait.started + wait.timeout - time.monotonic()
                finished = sum(row.status not in ACTIVE for row in rows)
                if len(rows) != len(wait.keys):
                    reason = "已退出等待 · 任务视图已更新"
                elif finished == len(rows) or (wait.mode == "any" and finished):
                    reason = "已有任务结束" if wait.mode == "any" else "所选任务均已结束"
                elif remaining <= 0:
                    reason = "等待超时"
                else:
                    reason = ""
            with self._lock:
                if self._closed or self._current is not wait:
                    return
                if reason:
                    self._current = None
            if reason:
                self._on_done(reason, rows)
                return
            wait.changed.wait(remaining)
