"""交互 Subworker 生命周期：复用 Swarm，UI 只读最近一次提交的快照。"""
from __future__ import annotations

import copy
import threading
import time
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
            if event.kind == "tool.finished":
                row["tools"] = row.get("tools", 0) + 1
            elif event.kind == "model.finished":
                usage = event.payload.get("usage", {})
                row["calls"] = row.get("calls", 0) + 1
                if usage.get("tokens_reported"):
                    row["tokens"] = row.get("tokens", 0) + usage.get("total_tokens", 0)
                if usage.get("cost_reported"):
                    row["cost"] = row.get("cost", 0) + usage.get("cost_usd", 0)

    def _update(self, snapshot: dict[str, Any]) -> None:
        # 只在 Commander 提交检查点时投影；每个渲染帧不复制 DAG 或访问磁盘。
        with self._lock:
            self._snapshot = snapshot
            tasks = snapshot["tasks"]
            for task_id, item in tasks.items():
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
            self._snapshot = {"run_id": runtime.run_id, "checkpoint": str(runtime.checkpoint), "tasks": {}}
            self._badge = "启动中"

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
