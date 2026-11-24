"""会话级任务清单：为 Agent、持久化和终端视图提供同一个状态源。"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any, Iterable


TODO_STATUSES = ("pending", "in_progress", "completed", "blocked", "abandoned")
OPEN_TODO_STATUSES = frozenset({"pending", "in_progress", "blocked"})


@dataclass
class TodoItem:
    """一条可持久化、可独立更新的任务。"""

    id: str
    text: str
    status: str = "pending"
    phase: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "phase": self.phase,
            "note": self.note,
        }


class TodoList:
    """线程安全的有序任务清单。

    工具、TUI 与 ``/resume`` 都读取这里，避免 UI 和模型各维护一份状态。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: list[TodoItem] = []
        self._seq = 0
        self._revision = 0
        self._view_cache: dict[str, Any] | None = None

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.view())

    def view(self) -> dict[str, Any]:
        """返回只读用途的稳定投影；未变更时复用对象，供 TUI 差分渲染。"""
        with self._lock:
            if self._view_cache is None:
                counts = {status: 0 for status in TODO_STATUSES}
                for item in self._items:
                    counts[item.status] += 1
                self._view_cache = {
                    "revision": self._revision,
                    "seq": self._seq,
                    "counts": counts,
                    "open": sum(counts[status] for status in OPEN_TODO_STATUSES),
                    "tasks": [item.to_dict() for item in self._items],
                }
            return self._view_cache

    def restore(self, data: dict[str, Any] | None) -> None:
        """恢复旧快照；忽略未知状态和损坏条目。"""
        source = data if isinstance(data, dict) else {}
        rows = source.get("tasks", [])
        restored: list[TodoItem] = []
        seen: set[str] = set()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                task_id = str(row.get("id") or "").strip()
                text = str(row.get("text") or "").strip()
                status = str(row.get("status") or "pending")
                if not task_id or not text or task_id in seen or status not in TODO_STATUSES:
                    continue
                seen.add(task_id)
                restored.append(TodoItem(task_id, text, status,
                                         str(row.get("phase") or "").strip(),
                                         str(row.get("note") or "").strip()))
        with self._lock:
            self._items = restored
            self._seq = max(int(source.get("seq") or 0), self._largest_numeric_id(restored))
            self._revision = max(0, int(source.get("revision") or 0))
            self._view_cache = None

    def replace(self, rows: Iterable[dict[str, Any] | str]) -> dict[str, Any]:
        prepared: list[TodoItem] = []
        seen_ids: set[str] = set()
        seen_text: set[tuple[str, str]] = set()
        with self._lock:
            next_seq = self._seq
            for raw in rows:
                row = {"text": raw} if isinstance(raw, str) else raw
                if not isinstance(row, dict):
                    raise ValueError("items 必须是字符串或任务对象数组")
                text = str(row.get("text") or "").strip()
                if not text:
                    raise ValueError("任务文本不能为空")
                status = self._status(row.get("status", "pending"))
                phase = str(row.get("phase") or "").strip()
                key = (phase.casefold(), text.casefold())
                if key in seen_text:
                    raise ValueError(f"同一阶段存在重复任务：{text}")
                seen_text.add(key)
                task_id = str(row.get("id") or "").strip()
                if not task_id:
                    next_seq += 1
                    task_id = f"T{next_seq}"
                if task_id in seen_ids:
                    raise ValueError(f"任务 ID 重复：{task_id}")
                seen_ids.add(task_id)
                prepared.append(TodoItem(task_id, text, status, phase,
                                         str(row.get("note") or "").strip()))
            self._items = prepared
            self._seq = max(next_seq, self._largest_numeric_id(prepared))
            self._revision += 1
            self._view_cache = None
            return self.snapshot()

    def add(self, text: str, *, phase: str = "", status: str = "pending", note: str = "",
            task_id: str = "") -> dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("任务文本不能为空")
        phase = str(phase or "").strip()
        with self._lock:
            if any(item.text.casefold() == clean.casefold() and item.phase.casefold() == phase.casefold()
                   for item in self._items):
                raise ValueError(f"同一阶段存在重复任务：{clean}")
            task_id = str(task_id or "").strip()
            if task_id and any(item.id == task_id for item in self._items):
                raise ValueError(f"任务 ID 重复：{task_id}")
            if not task_id:
                self._seq += 1
                task_id = f"T{self._seq}"
            else:
                self._seq = max(self._seq, self._numeric_id(task_id))
            self._items.append(TodoItem(task_id, clean, self._status(status), phase,
                                        str(note or "").strip()))
            self._revision += 1
            self._view_cache = None
            return copy.deepcopy(self._items[-1].to_dict())

    def update(self, task_id: str, *, status: str = "", text: str = "", phase: str = "",
               note: str = "", replace_note: bool = False) -> dict[str, Any]:
        with self._lock:
            item = self._find(task_id)
            if status:
                item.status = self._status(status)
            if text:
                candidate = text.strip()
                if not candidate:
                    raise ValueError("任务文本不能为空")
                if any(other is not item and other.text.casefold() == candidate.casefold()
                       and other.phase.casefold() == (phase or item.phase).casefold()
                       for other in self._items):
                    raise ValueError(f"同一阶段存在重复任务：{candidate}")
                item.text = candidate
            if phase:
                item.phase = phase.strip()
            if replace_note or note:
                item.note = note.strip()
            self._revision += 1
            self._view_cache = None
            return copy.deepcopy(item.to_dict())

    def remove(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._find(task_id)
            self._items.remove(item)
            self._revision += 1
            self._view_cache = None
            return copy.deepcopy(item.to_dict())

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._items = []
            self._revision += 1
            self._view_cache = None
            return self.snapshot()

    def _find(self, task_id: str) -> TodoItem:
        clean = str(task_id or "").strip()
        for item in self._items:
            if item.id == clean:
                return item
        raise ValueError(f"未找到任务：{clean or '(空 ID)'}")

    @staticmethod
    def _status(value: Any) -> str:
        status = str(value or "pending")
        if status not in TODO_STATUSES:
            raise ValueError("status 必须是 " + " / ".join(TODO_STATUSES))
        return status

    @staticmethod
    def _numeric_id(task_id: str) -> int:
        return int(task_id[1:]) if task_id.startswith("T") and task_id[1:].isdigit() else 0

    @classmethod
    def _largest_numeric_id(cls, items: Iterable[TodoItem]) -> int:
        return max((cls._numeric_id(item.id) for item in items), default=0)


__all__ = ["OPEN_TODO_STATUSES", "TODO_STATUSES", "TodoItem", "TodoList"]
