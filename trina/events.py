"""统一运行事件：事务持久化、顺序订阅、游标重放与显式消费确认。"""
from __future__ import annotations

import fnmatch
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .audit import DEFAULT_REDACT_KEYS, redact


@dataclass(frozen=True)
class RuntimeEvent:
    """seq 只在所属 stream 内比较；payload 返回独立的 JSON 快照。"""

    seq: int
    stream_id: str
    run_id: str
    kind: str
    time: float
    source: str
    task_id: str
    worker_id: str
    call_id: str
    payload: dict[str, Any]
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """先提交 SQLite 再投递；订阅失败不会回滚已经执行的工具。

    每次数据库操作使用短连接，允许另一个 CLI 进程重放和续流。运行事件与
    哈希链审计分别保存；事件重放只恢复观察，不重新执行任何工具。
    """

    def __init__(self, path: str | Path, stream_id: str, *, create: bool = True, redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS):
        self.path = Path(path).resolve()
        self.stream_id = stream_id
        self.redact_keys = tuple(key.lower() for key in redact_keys)
        self._condition = threading.Condition(threading.RLock())
        self._subscribers: list[EventSubscription] = []
        self.errors: list[str] = []
        if not create and not self.path.is_file():
            raise FileNotFoundError(self.path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            if create:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS consumers (
                        name TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS runtime_state (
                        seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                """)
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('stream_id', ?)", (stream_id,))
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('version', '1')")
                db.commit()
            stored = dict(db.execute("SELECT key, value FROM metadata"))
            if stored.get("version") != "1" or stored.get("stream_id") != stream_id:
                raise ValueError("事件存储版本或 stream_id 不匹配")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @classmethod
    def open_existing(cls, path: str | Path) -> EventBus:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        with closing(sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='stream_id'").fetchone()
        if row is None:
            raise ValueError("缺少事件 stream_id")
        return cls(resolved, row[0], create=False)

    @property
    def last_seq(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])

    def publish(self, kind: str, *, run_id: str = "", source: str = "runtime", task_id: str = "",
                worker_id: str = "", call_id: str = "", **payload: Any) -> RuntimeEvent:
        if not kind or not isinstance(kind, str):
            raise ValueError("事件 kind 不能为空")
        # 在调用者继续修改对象前完成拷贝与脱敏。
        def safe(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: (item if isinstance(item, str) and item.startswith("<redacted:") else
                              redact({key: item}, list(self.redact_keys))[key])
                        if key.lower() in self.redact_keys else safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            return value

        original = json.dumps(payload, ensure_ascii=False, default=str)
        snapshot = safe(json.loads(original))
        body = dict(stream_id=self.stream_id, run_id=run_id or self.stream_id, kind=kind,
                    time=time.time(), source=source, task_id=task_id, worker_id=worker_id,
                    call_id=call_id, payload=snapshot, version=1)
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._condition:
            with closing(self._connect()) as db:
                with db:
                    cursor = db.execute("INSERT INTO events(body) VALUES (?)", (encoded,))
                    seq = int(cursor.lastrowid)
                    if kind in {"state.updated", "runtime.checkpoint"}:
                        # 恢复状态与 state.json 具有相同保真度；公开事件仍按配置脱敏。
                        db.execute("INSERT INTO runtime_state VALUES (?, ?)", (seq, original))
            subscribers = tuple(self._subscribers)
            self._condition.notify_all()
        for subscription in subscribers:
            subscription.drain()
        return RuntimeEvent(seq=seq, **json.loads(encoded))

    def runtime_payload(self, event: RuntimeEvent) -> dict[str, Any]:
        """仅供本地调度恢复读取完整状态，避免脱敏值改变事实身份或凭据。"""
        if event.stream_id != self.stream_id:
            raise ValueError("事件不属于当前流")
        if event.kind not in {"state.updated", "runtime.checkpoint"}:
            return event.payload
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM runtime_state WHERE seq=?", (event.seq,)).fetchone()
        return json.loads(row[0]) if row else event.payload

    def replay(self, after: int = 0, *, limit: int = 1000, kinds: Iterable[str] = (),
               task_id: str | None = None) -> list[RuntimeEvent]:
        """严格读取 seq > after；过滤不改变事件的原始序号。"""
        if after < 0 or limit < 1:
            raise ValueError("after 必须非负，limit 必须为正")
        patterns = tuple(kinds)
        rows: list[RuntimeEvent] = []
        with closing(self._connect()) as db:
            for seq, body in db.execute("SELECT seq, body FROM events WHERE seq > ? ORDER BY seq", (after,)):
                event = RuntimeEvent(seq=seq, **json.loads(body))
                if patterns and not any(fnmatch.fnmatchcase(event.kind, pattern) for pattern in patterns):
                    continue
                if task_id is not None and event.task_id != task_id:
                    continue
                rows.append(event)
                if len(rows) == limit:
                    break
        return rows

    def wait(self, after: int, timeout: float = 1.0) -> bool:
        """同进程用条件通知，跨进程最多 100ms 后检查提交记录。"""
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self.last_seq <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(.1, remaining))
            return True

    def subscribe(self, callback: Callable[[RuntimeEvent], None] | None = None, *, after: int | None = None,
                  name: str = "", kinds: Iterable[str] = (), task_id: str | None = None) -> EventSubscription:
        """注册与回放共用同一游标；回放到实时切换没有另一个易丢失的队列。"""
        with self._condition:
            if after is None:
                with closing(self._connect()) as db:
                    row = db.execute("SELECT cursor FROM consumers WHERE name=?", (name,)).fetchone() if name else None
                after = int(row[0]) if row else 0
            if after < 0 or after > self.last_seq:
                raise ValueError("订阅游标超出当前事件流")
            subscription = EventSubscription(self, after, name, tuple(kinds), task_id, callback)
            if callback is not None:
                self._subscribers.append(subscription)
        subscription.drain()
        return subscription

    def interrupt_open_calls(self) -> None:
        """进程恢复时标记失去执行器的调用；不假定旧工具可以自动重放。"""
        pending: dict[tuple[str, str], RuntimeEvent] = {}
        after = 0
        while batch := self.replay(after, kinds=("tool.requested", "tool.finished", "job.started", "job.finished")):
            for event in batch:
                identity = (event.kind.split(".")[0], event.call_id)
                if event.kind in {"tool.requested", "job.started"}:
                    pending[identity] = event
                else:
                    pending.pop(identity, None)
            after = batch[-1].seq
        for call in pending.values():
            self.publish(call.kind.split(".")[0] + ".finished", run_id=call.run_id, task_id=call.task_id, worker_id=call.worker_id,
                         call_id=call.call_id, source="recovery", tool=call.payload.get("tool", ""),
                         status="interrupted", ok=False, error="原执行进程已中断，未自动重放", duration=0)


class EventSubscription:
    """poll 后由消费者 ack；失败或未确认批次在重新订阅时至少投递一次。"""

    def __init__(self, bus: EventBus, after: int, name: str, kinds: tuple[str, ...], task_id: str | None,
                 callback: Callable[[RuntimeEvent], None] | None):
        self.bus, self.cursor, self.name = bus, after, name
        self.kinds, self.task_id, self.callback = kinds, task_id, callback
        self.closed = False
        self._delivery = threading.Lock()

    def poll(self, *, timeout: float = 0, limit: int = 1000) -> list[RuntimeEvent]:
        deadline = time.monotonic() + max(0, timeout)
        while not self.closed:
            high_water = self.bus.last_seq
            batch = self.bus.replay(self.cursor, limit=limit, kinds=self.kinds, task_id=self.task_id)
            if batch:
                self.cursor = batch[-1].seq
                return batch
            self.cursor = max(self.cursor, high_water)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.bus.wait(self.cursor, remaining):
                return []
        return []

    def ack(self) -> None:
        if not self.name:
            return
        with closing(self.bus._connect()) as db:
            with db:
                db.execute("INSERT INTO consumers VALUES (?, ?) ON CONFLICT(name) DO UPDATE "
                           "SET cursor=MAX(cursor, excluded.cursor)", (self.name, self.cursor))

    def drain(self) -> None:
        if self.closed or self.callback is None or not self._delivery.acquire(blocking=False):
            return
        failed = False
        try:
            while not self.closed:
                high_water = self.bus.last_seq
                batch = self.bus.replay(self.cursor, kinds=self.kinds, task_id=self.task_id)
                if not batch:
                    self.cursor = max(self.cursor, high_water)
                    break
                for event in batch:
                    self.callback(event)
                    self.cursor = event.seq
                    self.ack()
        except Exception as exc:
            failed = True
            self.bus.errors.append(f"{self.name or 'subscriber'} at {self.cursor}: {type(exc).__name__}: {exc}")
            del self.bus.errors[:-20]
        finally:
            self._delivery.release()
        # publish 恰好发生在空批次与释放投递锁之间时，补消费已提交的尾部。
        if not failed and not self.closed and self.bus.last_seq > self.cursor:
            self.drain()

    def close(self) -> None:
        self.closed = True
        with self.bus._condition:
            if self in self.bus._subscribers:
                self.bus._subscribers.remove(self)
            self.bus._condition.notify_all()


class ModelEventStream:
    """把后端 SSE 与非流式替身都规范为具有 request_id 的同一生命周期。"""

    def __init__(self, emit: Callable[..., Any], visible: Callable[[Any], None] | None = None):
        import uuid
        self.emit, self.visible = emit, visible
        self.request_id = uuid.uuid4().hex
        self._error_emitted = False
        self.emit("model.started", request_id=self.request_id)

    def on_event(self, event: Any) -> None:
        if event.type in {"text_delta", "thinking_delta"}:
            self.emit("model." + event.type, request_id=self.request_id, delta=event.delta)
        elif event.type == "start":
            self.emit("model.connected", request_id=self.request_id)
        elif event.type == "error":
            self._emit_error(event.delta or getattr(event.response, "error", "模型调用失败"))
        if self.visible is not None:
            self.visible(event)

    def finish(self, response: Any) -> None:
        if not response.ok:
            self._emit_error(response.error)
        for call in response.tool_calls:
            self.emit("model.tool_call", request_id=self.request_id, native_call_id=call.id,
                      tool=call.name, arguments=call.arguments)
        self.emit("model.finished", request_id=self.request_id, ok=response.ok, error=response.error,
                  usage=response.usage.to_dict(), finish_reason=response.finish_reason)

    def _emit_error(self, error: str) -> None:
        if not self._error_emitted:
            self.emit("model.error", request_id=self.request_id, error=error)
            self._error_emitted = True

    def invoke(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._emit_error(error)
            self.emit("model.finished", request_id=self.request_id, ok=False, error=error)
            raise
