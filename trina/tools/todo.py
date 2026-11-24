"""让 Agent 维护用户可见的会话任务清单。"""
from __future__ import annotations

from typing import Any

from ..todo import TodoList
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


class TodoTool(Tool):
    spec = ToolSpec(
        name="todo",
        summary="维护会话任务清单；复杂多步任务先建立清单，开始、完成或受阻时及时更新，普通问答无需调用",
        category="coordination",
        action_level="detect",
        params={
            "action": ParamSpec("str", "操作", enum=["replace", "add", "update", "remove", "clear", "list"]),
            "items": ParamSpec("list", "replace 的完整任务数组；每项含 text，可含 id/status/phase/note", required=False, default=[]),
            "task_id": ParamSpec("str", "稳定任务 ID，例如 T1", required=False, default=""),
            "text": ParamSpec("str", "任务文本", required=False, default=""),
            "status": ParamSpec("str", "任务状态", required=False, default="",
                                enum=["", "pending", "in_progress", "completed", "blocked", "abandoned"]),
            "phase": ParamSpec("str", "可选阶段名", required=False, default=""),
            "note": ParamSpec("str", "可选简短说明；传空字符串不会覆盖旧说明", required=False, default=""),
        },
    )

    def run(self, ctx, action, items=None, task_id="", text="", status="", phase="", note=""):
        todo = ctx.extra.get("todo_list")
        if not isinstance(todo, TodoList):
            todo = TodoList()
            ctx.extra["todo_list"] = todo
        try:
            changed: dict[str, Any] | None = None
            if action == "replace":
                snapshot = todo.replace(items or [])
            elif action == "add":
                changed = todo.add(text, task_id=task_id, status=status or "pending", phase=phase, note=note)
                snapshot = todo.snapshot()
            elif action == "update":
                if not task_id:
                    raise ValueError("update 必须提供 task_id")
                if not any((status, text, phase, note)):
                    raise ValueError("update 至少提供 status、text、phase 或 note 之一")
                changed = todo.update(task_id, status=status, text=text, phase=phase, note=note)
                snapshot = todo.snapshot()
            elif action == "remove":
                if not task_id:
                    raise ValueError("remove 必须提供 task_id")
                changed = todo.remove(task_id)
                snapshot = todo.snapshot()
            elif action == "clear":
                snapshot = todo.clear()
            elif action == "list":
                snapshot = todo.snapshot()
            else:
                raise ValueError(f"未知操作：{action}")
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))

        counts = snapshot["counts"]
        if action != "list":
            ctx.emit("todo.updated", snapshot=snapshot, action=action,
                     item_id=(changed or {}).get("id", ""))
        summary = (f"任务清单：{counts['in_progress']} 进行中 · {counts['pending']} 待处理"
                   f" · {counts['blocked']} 阻塞 · {counts['completed']} 已完成")
        data: dict[str, Any] = {"revision": snapshot["revision"], "counts": counts}
        if action in {"replace", "list"}:
            data["tasks"] = snapshot["tasks"]
        elif changed is not None:
            data["task"] = changed
        return ToolResult(summary=summary, data=data)


TOOLS = [TodoTool()]
