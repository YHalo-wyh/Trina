"""人机协同会话原语：审批、实时介入（steer）、暂停/停止。

设计要点
--------
* **失败关闭（fail-closed）**：审批超时或通道异常一律判为「拒绝」，不会因为没人值守就放行高危动作。
* **审批按动作等级**：`off` 全自动 / `exploit` 只审命令执行与爆破 / `verify` 审 verify 及以上 / `all` 每步都审。
* 会话对象只做「状态 + 同步」，不负责解析命令行；控制台（`trina.interactive`）负责人机界面。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import ACTION_LEVELS

APPROVAL_MODES = ("off", "exploit", "verify", "all")

#: 审批等待上限（秒）；超时视为拒绝
DEFAULT_APPROVAL_TIMEOUT = 300.0


def level_needs_approval(mode: str, level: str) -> bool:
    """按模式判断某个动作等级是否需要人工批准。"""
    mode = (mode or "off").strip().lower()
    if mode == "off":
        return False
    if mode == "all":
        return True
    if level not in ACTION_LEVELS:
        # 未知等级按最保守处理：只有 all 才自动放行，其他模式需人工确认
        return mode in ("exploit", "verify")
    threshold = ACTION_LEVELS.index(mode) if mode in ACTION_LEVELS else len(ACTION_LEVELS)
    return ACTION_LEVELS.index(level) >= threshold


@dataclass
class Session:
    """一次人机协同运行的共享状态。"""

    approval_mode: str = "exploit"
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    emit: Callable[[str], None] = field(default_factory=lambda: (lambda _msg: None))
    steers: "queue.Queue[str]" = field(default_factory=queue.Queue)
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)

    #: 运行态
    state: Any = None
    thread: threading.Thread | None = None
    started_at: float = 0.0

    #: 审批通道
    pending: dict[str, Any] | None = None
    _answer_event: threading.Event = field(default_factory=threading.Event)
    _answer_value: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ 审批
    def needs_approval(self, level: str) -> bool:
        return level_needs_approval(self.approval_mode, level)

    def request_approval(self, request: dict[str, Any]) -> bool:
        """阻塞等待人工批准；超时/异常一律返回 False（fail-closed）。"""
        level = str(request.get("level", ""))
        if not self.needs_approval(level):
            return True
        with self._lock:
            self.pending = request
            self._answer_event.clear()
            self._answer_value = False
        self.emit(
            "[审批] 请求执行 "
            f"{request.get('tool')}（等级 {level}，参数 {request.get('params')}）→ 允许？"
            "  输入 y 允许 / n 拒绝"
        )
        answered = self._answer_event.wait(self.approval_timeout)
        with self._lock:
            self.pending = None
            allowed = bool(self._answer_value) if answered else False
        record = {
            "tool": request.get("tool"),
            "level": level,
            "allowed": allowed,
            "timeout": not answered,
            "params": request.get("params"),
            "ts": time.time(),
        }
        self.decisions.append(record)
        if not answered:
            self.emit(f"[审批] {request.get('tool')} 超时未答复 → 按拒绝处理（fail-closed）")
        return allowed

    def answer(self, allow: bool) -> bool:
        """控制台回答当前挂起的审批；返回是否确有挂起审批。"""
        with self._lock:
            if self.pending is None:
                return False
            self._answer_value = bool(allow)
            tool = self.pending.get("tool")
        self._answer_event.set()
        self.emit(f"[审批] 已{'允许' if allow else '拒绝'}：{tool}")
        return True

    # ------------------------------------------------------------------ 介入
    def steer(self, text: str) -> None:
        """把人工提示投递给运行中的 Agent（下一步生效）。"""
        note = (text or "").strip()
        if not note:
            return
        self.steers.put(note)
        self.emit(f"[介入] 已投递提示：{note}")

    def request_stop(self) -> None:
        self.stop_event.set()
        self.emit("[停止] 已请求停止当前任务（当前工具调用结束后生效）")

    def toggle_pause(self) -> bool:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.emit("[暂停] 已恢复运行")
            return False
        self.pause_event.set()
        self.emit("[暂停] 已暂停（输入 resume 继续、stop 停止）")
        return True

    # ------------------------------------------------------------------ 查询
    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def status_line(self) -> str:
        if not self.running:
            return "空闲"
        state = self.state
        if state is None:
            return "运行中（尚未进入决策循环）"
        flags = [
            f"阶段={getattr(state, 'phase', '?')}",
            f"步数={state.counters.get('steps', 0)}",
            f"漏洞={len(getattr(state, 'findings', []))}",
            f"flag={len(getattr(state, 'flags', []))}",
            f"被拒={state.counters.get('denied', 0)}",
        ]
        if self.pause_event.is_set():
            flags.append("已暂停")
        if self.pending:
            flags.append(f"待审批={self.pending.get('tool')}")
        return " | ".join(flags)

    def reset_for_run(self) -> None:
        self.stop_event.clear()
        self.pause_event.clear()
        while not self.steers.empty():
            try:
                self.steers.get_nowait()
            except queue.Empty:  # pragma: no cover
                break


__all__ = ["APPROVAL_MODES", "DEFAULT_APPROVAL_TIMEOUT", "Session", "level_needs_approval"]
