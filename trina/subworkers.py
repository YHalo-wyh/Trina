"""交互 Subworker 生命周期：复用 Swarm，UI 只读最近一次提交的快照。"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any, Callable

from .config import TrinaConfig
from .console_state import ConsoleState, TrackedBackend
from .llm import make_backend
from .swarm import SwarmRuntime, TaskSpec


class Subworkers:
    def __init__(self, notify: Callable[[str], None]) -> None:
        self.notify = notify
        self.runtime: SwarmRuntime | None = None
        self.thread: threading.Thread | None = None
        self.usage = ConsoleState()
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {"tasks": {}}
        self._badge = ""
        self._rows: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._dialogs: dict[str, OrderedDict[str, tuple[str, str]]] = {}
        self._versions: dict[str, int] = {}
        self._line_cache: dict[tuple[str, int], tuple[int, list[tuple[str, str]]]] = {}
        self._block_cache: dict[tuple[str, int], dict[str, tuple[Any, list[tuple[str, str]]]]] = {}
        self._completed: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def task_result(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._snapshot["tasks"].get(task_id) or self._completed.get(task_id)
            if item is None:
                return None
            return copy.deepcopy({key: item.get(key) for key in ("goal", "target", "status", "model", "reasoning_effort", "error", "result", "evidence_refs")})

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def badge(self) -> str:
        with self._lock:
            if self.runtime is not None and self.running:
                if self.runtime.stop_event.is_set():
                    return "停止中"
                if self.runtime.pause_event.is_set():
                    return "已请求暂停"
            return self._badge

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._rows.values()]

    def _progress(self, event: Any) -> None:
        with self._lock:
            row = self._rows.get(event.task_id)
            if row is None:
                return
            row["updated_at"] = time.time()
            payload = event.payload
            key = event.call_id or str(payload.get("message_id") or payload.get("request_id") or event.seq)
            kind, text = "", ""
            if event.kind == "assistant.message":
                kind, text = "回复", str(payload.get("text", ""))
            elif event.kind == "react.action":
                kind, text = "过程", str(payload.get("thought", ""))
            elif event.kind == "tool.started":
                import json
                kind, text = "工具", str(payload.get("tool", "")) + " " + json.dumps(payload.get("params", {}), ensure_ascii=False)[:1200]
                key += ":call"
            elif event.kind == "tool.output":
                kind, text = "输出", str(payload.get("delta", ""))
                key += ":output"
                old = self._dialogs.get(event.task_id, {}).get(key, ("", ""))[1]
                text = old + text
            elif event.kind == "tool.finished":
                kind = "结果"
                text = str(payload.get("summary") or payload.get("error") or payload.get("status", ""))
                text = f"{payload.get('tool', '')} · {payload.get('status', '')} · {text}"
                key += ":result"
            elif event.kind == "model.error":
                kind, text = "错误", str(payload.get("error", ""))
            elif event.kind == "model.started":
                row["activity"] = "思考中"
            if event.kind == "tool.started":
                row["activity"] = "工具 · " + str(payload.get("tool", ""))
            if text:
                self._record(event.task_id, key, kind, text)
            if event.kind == "tool.finished":
                row["tools"] = row.get("tools", 0) + 1
            elif event.kind == "model.finished":
                usage = event.payload.get("usage", {})
                row["calls"] = row.get("calls", 0) + 1
                if usage.get("tokens_reported"):
                    row["tokens"] = row.get("tokens", 0) + usage.get("total_tokens", 0)
                if usage.get("cost_reported"):
                    row["cost"] = row.get("cost", 0) + usage.get("cost_usd", 0)

    def _record(self, task_id: str, key: str, kind: str, text: str) -> None:
        from .terminal import clean_text
        dialog = self._dialogs.setdefault(task_id, OrderedDict())
        dialog[key] = (kind, clean_text(text)[-4000:])
        while len(dialog) > 80:
            dialog.popitem(last=False)
        self._versions[task_id] = self._versions.get(task_id, 0) + 1

    def dialog_lines(self, task_id: str, width: int) -> list[tuple[str, str]]:
        """仅变化任务和宽度重新折行；其余帧复用有界文本缓存。"""
        from .console_state import _cells
        width = max(1, width)
        with self._lock:
            version = self._versions.get(task_id, 0)
            cached = self._line_cache.get((task_id, width))
            if cached is not None and cached[0] == version:
                return cached[1]
            lines = []
            dialog = self._dialogs.get(task_id, {})
            blocks = self._block_cache.setdefault((task_id, width), {})
            for key, (kind, text) in dialog.items():
                previous = blocks.get(key)
                if previous is not None and previous[0] == (kind, text):
                    wrapped = previous[1]
                else:
                    wrapped = []
                    for source in (kind + " › " + text).splitlines():
                        line, used = "", 0
                        for char in source:
                            size = _cells(char)
                            if size > width:
                                continue
                            if used + size > width:
                                wrapped.append((kind, line))
                                line, used = "", 0
                            line, used = line + char, used + size
                        wrapped.append((kind, line))
                    blocks[key] = ((kind, text), wrapped)
                lines.extend(wrapped)
            for key in list(blocks):
                if key not in dialog:
                    del blocks[key]
            # 一次调整窗口不永久保留所有旧宽度缓存。
            if len(self._line_cache) > 24:
                self._line_cache.clear()
                self._block_cache.clear()
            lines = lines[-600:]
            self._line_cache[(task_id, width)] = (version, lines)
            return lines

    def submit(self, config: TrinaConfig, task: TaskSpec, *, backend_factory=make_backend) -> bool:
        """Agent 工具的同步接收边界；不把异步排队伪装为执行完成。"""
        with self._lock:
            if task.id in self._completed:
                raise ValueError("任务名称已有历史结果，请使用新名称；旧结果仍可 result 读取")
            if not self.running:
                self.start(config, [task], backend_factory=backend_factory)
                return True
            future = self.runtime.submit_task(task)
        return future.result(timeout=5)

    def task_control(self, task_id: str, action: str, text: str = "") -> None:
        if self.runtime is None or not self.running:
            raise ValueError("没有运行中的任务")
        self.runtime.control_task(task_id, action, text).result(timeout=5)
        with self._lock:
            self._record(task_id, "control-" + str(time.monotonic_ns()), "补充" if action == "message" else "控制", text or "请求取消")

    def _update(self, snapshot: dict[str, Any]) -> None:
        # 只在 Commander 提交检查点时投影；每个渲染帧不复制 DAG 或访问磁盘。
        with self._lock:
            self._snapshot = snapshot
            tasks = snapshot["tasks"]
            for task_id, item in tasks.items():
                if item["status"] not in {"pending", "running"}:
                    self._completed[task_id] = item
                    while len(self._completed) > 128:
                        self._completed.popitem(last=False)
                row = self._rows.setdefault(task_id, {"id": task_id})
                row.update({key: item[key] for key in ("goal", "status", "model", "reasoning_effort", "profile", "started_at", "ended_at")})
                if item.get("result"):
                    result = item["result"]
                    row["tools"] = result.get("counters", {}).get("tool_calls", row.get("tools", 0))
                    ledger = result.get("ledger", {})
                    row["calls"] = ledger.get("calls", row.get("calls", 0))
                    if ledger.get("tokens_reported"):
                        row["tokens"] = ledger.get("total_tokens", 0)
                    if ledger.get("cost_reported"):
                        row["cost"] = ledger.get("cost_usd", 0)
            active = sum(item["status"] == "running" for item in tasks.values())
            waiting = sum(item["status"] == "pending" for item in tasks.values())
            self._badge = f"{active} 运行 / {waiting} 等待" if active or waiting else f"{len(tasks)} 已收尾"

    def start(self, config: TrinaConfig, tasks: list[TaskSpec] | None = None, *,
              checkpoint: str = "", backend_factory=make_backend, executor=None) -> None:
        with self._lock:
            if self.running:
                raise ValueError("Subworker 正在运行；使用 add 追加任务或 stop 后重开")
            if self._closed:
                raise ValueError("控制台已经关闭")
            cfg = copy.deepcopy(config)
            self.usage = ConsoleState()
            # 每个 Worker 各自构建后端；只汇总统计，不共用可变后端或对话。
            factory = lambda llm: TrackedBackend(backend_factory(llm), self.usage)
            if checkpoint:
                runtime = SwarmRuntime.resume(cfg, checkpoint, backend_factory=factory, executor=executor)
            else:
                runtime = SwarmRuntime(cfg, tasks or [], max_workers=min(cfg.subworker.workers, cfg.scope.max_host_concurrency),
                                       backend_factory=factory, executor=executor)
            self.runtime = runtime
            runtime.on_update = self._update
            runtime.on_progress = self._progress
            self._rows = {}
            self._dialogs, self._versions, self._line_cache, self._block_cache = {}, {}, {}, {}
            self._snapshot = {"run_id": runtime.run_id, "checkpoint": str(runtime.checkpoint), "tasks": {}}
            self._badge = "启动中"
            self._update({**self._snapshot, "tasks": {task.id: {"goal": task.goal, "target": task.target,
                          **asdict(runtime.records[task.id])} for task in runtime.tasks}})
            if checkpoint:
                cursor = 0
                kinds = ("assistant.message", "react.action", "tool.started", "tool.output", "tool.finished")
                while batch := runtime.event_bus.replay(cursor, kinds=kinds):
                    for event in batch:
                        record = runtime.records.get(event.task_id)
                        if record is not None and event.run_id == f"{runtime.run_id}-{event.task_id}-{record.attempt}":
                            # 恢复显示不重复累计请求或工具用量。
                            if event.kind == "tool.finished":
                                from dataclasses import replace
                                event = replace(event, kind="assistant.message", payload={"text": event.payload.get("summary", ""), "message_id": str(event.seq)})
                            self._progress(event)
                    cursor = batch[-1].seq

            def run() -> None:
                try:
                    runtime.run()
                    statuses = [record.status for record in runtime.records.values()]
                    message = (f"{runtime.run_id} · {statuses.count('completed')}/{len(statuses)} 项执行完成"
                               f" · /subworker 查看结果")
                except Exception as exc:
                    message = f"{runtime.run_id} · 运行失败: {type(exc).__name__}: {exc}"
                    with self._lock:
                        self._snapshot["error"] = str(exc)
                        self._badge = "运行失败"
                if not self._closed:
                    self.notify(message)

            self.thread = threading.Thread(target=run, name="trina-subworkers", daemon=True)
            self.thread.start()

    def add(self, task: TaskSpec) -> None:
        runtime = self.runtime
        if runtime is None or not self.running:
            raise ValueError("没有运行中的 Subworker；使用 start 或 run 启动")
        result = runtime.submit_task(task)

        def finished(future) -> None:
            try:
                text = f"{task.id} · " + ("已加入队列" if future.result() else "等价任务已存在，未重复执行")
            except Exception as exc:
                text = f"{task.id} · 未加入: {exc}"
            if not self._closed:
                self.notify(text)
        result.add_done_callback(finished)

    def configure(self, task_id: str, **overrides: str) -> None:
        if not self.running or self.runtime is None:
            raise ValueError("没有运行中的队列；省略任务 ID 调整下次启动的默认配置")
        future = self.runtime.configure_task(task_id, **overrides)
        def finished(result) -> None:
            try:
                result.result()
                text = f"{task_id} · 等待任务的配置已更新"
            except Exception as exc:
                text = f"{task_id} · 未修改: {exc}"
            if not self._closed:
                self.notify(text)
        future.add_done_callback(finished)

    def control(self, action: str) -> None:
        if not self.running or self.runtime is None:
            raise ValueError("没有运行中的 Subworker")
        if action == "stop":
            self.runtime.stop_event.set()
            self.runtime.pause_event.clear()
        elif action == "pause":
            self.runtime.pause_event.set()
        elif action == "continue":
            self.runtime.pause_event.clear()
        else:
            raise ValueError(f"未知控制动作: {action}")
        self.runtime.event_bus.publish("runtime.wake", source="console", action=action)

    def close(self) -> bool:
        self._closed = True
        if self.running:
            self.control("stop")
            self.thread.join(timeout=5)
        return not self.running
