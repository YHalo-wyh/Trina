"""退出阶段延迟 Ctrl+C 异常，重复中断只缩短线程等待。"""
from __future__ import annotations

import signal
import threading
import time
from typing import Callable


class Shutdown:
    """保留资源清理机会，并在同一截止时间内等待工作线程。"""

    def __init__(self, report: Callable[[str], None], timeout: float = 5):
        self.interrupted = False
        self.deadline = time.monotonic() + timeout
        self.report = report
        self._previous = None
        self._installed = False

    def __enter__(self) -> Shutdown:
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._interrupt)
            self._installed = True
        return self

    def _interrupt(self, _signum, _frame) -> None:
        # 信号处理器只赋值，不取锁，避免重复信号重入 Event.set 时死锁。
        self.interrupted = True

    def __exit__(self, *_exc) -> None:
        if self._installed:
            signal.signal(signal.SIGINT, self._previous)

    def call(self, label: str, action: Callable[[], object]) -> bool:
        """单个资源失败不阻断其他资源的回收。"""
        try:
            action()
        except KeyboardInterrupt:
            self.interrupted = True
        except Exception as exc:
            self.report(f"退出清理 {label} 失败：{exc}")
        else:
            return True
        return False

    def join(self, thread: threading.Thread | None) -> None:
        if thread is None or thread is threading.current_thread():
            return
        while not self.interrupted and thread.is_alive():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                return
            if not self.call("工作线程", lambda: thread.join(timeout=min(.1, remaining))):
                return
