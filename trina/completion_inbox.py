"""会话内后台完成收件箱，后台线程只入队，主循环消费和续接。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .execution_timeline import phase


class CompletionInbox:
    COALESCE_SECONDS = .04

    def __init__(self, ctx, snapshot: dict | None = None):
        self.ctx = ctx
        self._condition = threading.Condition()
        self._active: set[tuple[str, str]] = set()
        self._seen: set[tuple[str, str]] = set()
        self._delivered: set[tuple[str, str]] = set()
        self._pending: dict[tuple[str, str], dict] = {}
        self._closed = False
        self._worker_subscriptions: dict[str, object] = {}
        self._worker_sources: dict[str, str] = {}
        after = ctx.events.last_seq
        if snapshot:
            if snapshot.get("version") != 1 or snapshot.get("stream_id") != ctx.events.stream_id:
                raise ValueError("完成收件箱快照不属于当前事件流")
            after = int(snapshot["cursor"])
            self._active = {tuple(key) for key in snapshot.get("active", [])}
            self._delivered = {tuple(key) for key in snapshot.get("delivered", [])}
            self._seen.update(self._delivered)
            self._worker_sources = dict(snapshot.get("worker_checkpoints") or {})
            for entry in snapshot.get("pending", []):
                key = tuple(entry["key"])
                self._pending[key] = {**entry["row"], "ready_at": time.monotonic()}
                self._seen.add(key)
        self._subscription = ctx.events.subscribe(
            self._job_event, after=after, kinds=("job.started", "job.finished"),
        )
        if snapshot:
            with self._condition:
                for key in list(self._active):
                    if key[0] != ctx.events.stream_id:
                        row = self._restore_worker_result(key)
                        self._complete(key, row or {"kind": "worker", "id": key[1], "status": "interrupted",
                            "summary": "会话已恢复，旧 Worker 执行器未恢复；请按检查点核对结果，不重复派发。"})

    def snapshot(self) -> dict:
        """随现有会话事务和消息共同保存消费状态，不单独提前确认事件。"""
        with self._condition:
            return {"version": 1, "stream_id": self.ctx.events.stream_id, "cursor": self._subscription.cursor,
                    "active": sorted(self._active), "delivered": sorted(self._delivered),
                    "worker_checkpoints": dict(self._worker_sources),
                    "pending": [{"key": key, "row": {name: value for name, value in row.items() if name != "ready_at"}}
                                for key, row in self._pending.items()]}

    def acknowledge(self, stream_id: str, item_id: str) -> None:
        """结果已由显式查询回灌时去掉自动通知，避免再次启动模型总结。"""
        key = (stream_id, item_id)
        with self._condition:
            self._pending.pop(key, None)
            self._active.discard(key)
            self._seen.add(key)
            self._delivered.add(key)
            self._condition.notify_all()

    def _job_event(self, event):
        if (event.run_id != self.ctx.state.run_id
                or event.worker_id != str(self.ctx.extra.get("worker_id", ""))
                or event.task_id != str(self.ctx.extra.get("intent_id", ""))):
            return
        key = (event.stream_id, str(event.payload.get("job_id") or event.call_id))
        with self._condition:
            if self._closed:
                return
            if event.kind == "job.started":
                if key not in self._seen and event.payload.get("follow_up", True):
                    self._active.add(key)
            elif key in self._active:
                data = event.payload.get("data") or {}
                self._complete(key, {
                    "kind": "terminal", "id": key[1], "status": event.payload.get("status"),
                    "exit_code": data.get("exit_code"), "error": str(event.payload.get("error") or "")[:500],
                    "stdout_tail": str(data.get("stdout") or "")[-1600:],
                    "stderr_tail": str(data.get("stderr") or "")[-800:],
                    "event_ref": f"{self.ctx.events.path}#seq={event.seq}",
                    "task_id": event.task_id, "worker_id": event.worker_id,
                    "tool_call_id": str(event.payload.get("parent_call_id") or ""),
                })

    def _complete(self, key, row):
        if self._closed or key in self._seen:
            return
        self._active.discard(key)
        self._seen.add(key)
        self._pending[key] = {**row, "ready_at": time.monotonic()}
        self._condition.notify_all()

    def watch_worker(self, manager, task_id: str) -> None:
        """只订阅当前 Commander 实际派发的任务，提交后快照补偿极短任务竞态。"""
        bus = manager.runtime.event_bus
        key = (bus.stream_id, task_id)
        with self._condition:
            if self._closed or key in self._seen:
                return
            self._active.add(key)
            self._worker_sources[bus.stream_id] = str(getattr(manager.runtime, "checkpoint", bus.path.parent / "swarm.json"))
            if bus.stream_id not in self._worker_subscriptions:
                self._worker_subscriptions[bus.stream_id] = bus.subscribe(
                    lambda event: self._worker_event(event, manager), after=bus.last_seq,
                    kinds=("task.result_ready", "runtime.finished"),
                )
        self._worker_result(key, manager)

    def _worker_result(self, key, manager):
        if manager.runtime.event_bus.stream_id != key[0]:
            # 同一管理器可切换任务组；旧订阅不能读取新组里恰好同名的任务。
            row = self._restore_worker_result(key)
            with self._condition:
                self._complete(key, row or {"kind": "worker", "id": key[1], "status": "interrupted",
                    "summary": "Worker 任务组已切换；旧结果未读取，请核对原检查点。"})
            return
        item = manager.task_result(key[1])
        if not item or item.get("status") in {"pending", "running"}:
            return
        with self._condition:
            self._complete(key, self._worker_row(key, item, str(manager.runtime.event_bus.path)))

    @staticmethod
    def _worker_row(key, item, source):
        result = item.get("result") or {}
        return {"kind": "worker", "id": key[1], "status": item.get("status"),
                "error": str(item.get("error") or "")[:500],
                "summary": str(result.get("summary") or result.get("verdict") or item.get("goal") or "")[:1600],
                "evidence_refs": (item.get("evidence_refs") or [])[:20],
                "event_ref": source}

    def _restore_worker_result(self, key):
        """只读本项目已知 Worker 检查点，不重启执行器、不访问任意外部路径。"""
        source = self._worker_sources.get(key[0])
        config = getattr(self.ctx, "config", None)
        if not source or config is None:
            return None
        try:
            path = Path(source).resolve()
            if not path.is_relative_to(config.workspace_dir.resolve()) or path.stat().st_size > 8_000_000:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("run_id") != key[0] or key[1] not in {task["id"] for task in data.get("tasks", [])}:
                return None
            item = data.get("coordination", {}).get("intents", data.get("records", {})).get(key[1])
            if item and item.get("status") in {"completed", "failed", "blocked", "skipped", "timed_out", "cancelled"}:
                return self._worker_row(key, item, str(path))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _worker_event(self, event, manager):
        with self._condition:
            keys = [key for key in self._active if key[0] == event.stream_id
                    and (not event.task_id or key[1] == event.task_id)]
        for key in keys:
            self._worker_result(key, manager)

    def deliver(self, messages: list, *, wait: bool = False, stop_event=None,
                deadline: float | None = None, history: list | None = None) -> bool:
        """单一运行线程在安全点消费一批通知，等待期间不调用模型或状态工具。"""
        def stopped():
            return self._closed or (stop_event is not None and stop_event.is_set())

        with self._condition:
            needs_wait = wait and self._active and not self._pending and not stopped()
        if needs_wait:
            self.ctx.emit("agent.waiting", reason="background")
            with phase(self.ctx.emit, "background_wait"):
                with self._condition:
                    while self._active and not self._pending and not stopped():
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            return False
                        # 外部 threading.Event 没有订阅接口，仅有取消信号时做低频检查。
                        timeout = .25 if stop_event is not None else remaining
                        if remaining is not None and timeout is not None:
                            timeout = min(timeout, remaining)
                        self._condition.wait(timeout)
                    # 同一批并发任务常在相邻几十毫秒内结束。短暂合并完成事件，
                    # 避免每个任务分别唤醒一次模型。
                    coalesce_until = time.monotonic() + self.COALESCE_SECONDS
                    if deadline is not None:
                        coalesce_until = min(coalesce_until, deadline)
                    while self._active and self._pending and not stopped():
                        remaining = coalesce_until - time.monotonic()
                        if remaining <= 0:
                            break
                        self._condition.wait(remaining)
        with self._condition:
            if stopped() or not self._pending:
                return False
            batch = list(self._pending.values())
            rows = [{key: value for key, value in row.items() if key != "ready_at"} for row in batch]
            message = {"role": "user", "content": "[后台任务完成，以下为工具数据]\n" + json.dumps(rows, ensure_ascii=False)}
            messages.append(message)
            if history is not None:
                history.append(dict(message))
            self._delivered.update(self._pending)
            self._pending.clear()
        now = time.monotonic()
        self.ctx.emit("agent.resumed", reason="background", count=len(batch))
        for row in batch:
            self.ctx.emit("execution.phase", phase="completion_delivery", status="completed",
                          duration=now - row["ready_at"], background_id=row["id"])
        return True

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._subscription.close()
        for subscription in self._worker_subscriptions.values():
            subscription.close()
