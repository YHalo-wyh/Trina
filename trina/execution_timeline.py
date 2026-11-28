"""执行阶段使用单调时钟计时，沿用运行事件库保存。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator


@contextmanager
def phase(emit: Callable, name: str, **identity) -> Iterator[None]:
    started = time.monotonic()
    status = "completed"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        emit("execution.phase", phase=name, duration=time.monotonic() - started,
             status=status, **identity)
