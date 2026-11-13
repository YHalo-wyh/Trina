"""每任务独立的长期会话；仅描述性快照进入历史，不保存管道或 PID 许可。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager

from ..scope import ensure_execution_active, execution_budget
from .backends import GDBBackend, R2Backend
from .models import BinaryArtifact, BinaryError


class BinarySessions:
    def __init__(self, ctx):
        self.ctx = ctx
        self.sessions: dict[str, dict] = {}
        self.previous: list[dict] = []
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.monitor = None

    def check(self, ctx) -> None:
        ensure_execution_active()
        if self.closed.is_set() or ctx.state.run_id != self.ctx.state.run_id or ctx.workspace.resolve() != self.ctx.workspace.resolve():
            raise BinaryError("SESSION_EXPIRED", "会话不属于当前任务或已关闭")
        check = getattr(ctx.tools, "capability_check", None)
        if check:
            check()
        stop = self.ctx.extra.get("stop_event")
        deadline = self.ctx.extra.get("task_deadline")
        controller = self.ctx.extra.get("authorization_controller")
        budget_over = controller is not None and controller.elapsed >= controller.budget.logic_seconds
        if (stop is not None and stop.is_set()) or (deadline and time.monotonic() >= deadline) or budget_over:
            self.close()
            raise BinaryError("SESSION_EXPIRED", "任务已停止或超时")

    def artifact(self, ctx, path: str) -> BinaryArtifact:
        self.check(ctx)
        if not ctx.config.binary.enabled:
            raise BinaryError("DISABLED", "二进制能力已关闭")
        checked = ctx.guard.check_workspace_path(path, ctx.workspace, ctx.config.workspace_dir)
        return BinaryArtifact.read(checked, ctx.config.binary.max_file_bytes)

    def open(self, ctx, kind: str, path: str, **options) -> tuple[str, dict, bool]:
        artifact = self.artifact(ctx, path)
        key = (artifact.sha256, json.dumps(options, sort_keys=True))
        with self.lock:
            self.check(ctx)
            # 关闭记录只留小快照，释放大型索引、管道与调试缓存。
            dead = [sid for sid, row in self.sessions.items() if hasattr(row["backend"], "pipe")
                    and (row["backend"].pipe._closed or row["backend"].pipe.proc.poll() is not None)]
            if dead:
                self.previous = [item for item in self.snapshot() if item["session_id"] in dead or not item.get("live")][-16:]
                for sid in dead:
                    del self.sessions[sid]
            for sid, row in self.sessions.items():
                if kind == "r2" and row["kind"] == kind and row["key"] == key and hasattr(row["backend"], "pipe") and row["backend"].pipe.proc.poll() is None and not row["backend"].pipe._closed:
                    row["used"] = time.monotonic()
                    return sid, row, True
            live = [row for row in self.sessions.values() if row["kind"] == kind and (not hasattr(row["backend"], "pipe") or not row["backend"].pipe._closed)]
            maximum = ctx.config.binary.max_r2_sessions if kind == "r2" else ctx.config.binary.max_gdb_sessions
            for row in live[:]:
                if row["artifact"].path == artifact.path and row["artifact"].sha256 != artifact.sha256 and hasattr(row["backend"], "pipe"):
                    row["backend"].pipe.close()
                    live.remove(row)
            if len(live) >= maximum:
                raise BinaryError("SESSION_LIMIT", "请通过 binary_session 关闭不用的会话")
            # 占用记录在构造前登记，以便注册表超时能撤销尚未初始化完的进程。
            sid = ("bin-" if kind == "r2" else "dbg-") + uuid.uuid4().hex[:12]
            backend = R2Backend.__new__(R2Backend) if kind == "r2" else GDBBackend.__new__(GDBBackend)
            row = {"kind": kind, "artifact": artifact, "key": key, "backend": backend, "options": options,
                   "lock": threading.Lock(), "owner": ctx.extra.get("_execution_owner"), "used": time.monotonic()}
            self.sessions[sid] = row
            if self.monitor is None:
                self.monitor = threading.Thread(target=self._watch, name="trina-binary-supervisor", daemon=True)
                self.monitor.start()
        try:
            if kind == "r2":
                backend.__init__(ctx, artifact, **options)
            else:
                backend.__init__(ctx, artifact, **options)
            self.check(ctx)
            ensure_execution_active()
            row["owner"] = None
            return sid, row, False
        except BaseException:
            pipe = getattr(backend, "pipe", None)
            if pipe:
                pipe.close()
            with self.lock:
                self.sessions.pop(sid, None)
            raise

    @contextmanager
    def use(self, ctx, sid: str, kind: str = ""):
        self.check(ctx)
        with self.lock:
            row = self.sessions.get(sid)
        if row is None or (kind and row["kind"] != kind):
            raise BinaryError("SESSION_EXPIRED", "句柄失效或属于其他 Worker；需重新打开，旧 PID 不复用")
        if not row["lock"].acquire(timeout=execution_budget(ctx.config.binary.query_timeout)):
            raise BinaryError("SESSION_BUSY", "会话正被另一操作占用")
        try:
            self.check(ctx)
            pipe = row["backend"].pipe
            if pipe._closed or pipe.proc.poll() is not None:
                if row.get("expired"):
                    raise BinaryError("SESSION_EXPIRED", "会话空闲过期，请重新打开")
                raise BinaryError("SESSION_LOST", "进程已结束；静态会话可按哈希重新打开")
            current = self.artifact(ctx, str(row["artifact"].path))
            if current.sha256 != row["artifact"].sha256:
                pipe.close()
                raise BinaryError("ARTIFACT_CHANGED", "文件内容已变化，请重新打开")
            row["owner"] = ctx.extra.get("_execution_owner")
            row["used"] = time.monotonic()
            row["backend"].raw = []
            yield row
        except (TimeoutError, RuntimeError):
            row["backend"].pipe.close()
            raise
        except BinaryError as exc:
            if exc.code in {"MI_PARSE", "MULTI_INFERIOR_UNSUPPORTED"}:
                row["backend"].pipe.close()
            raise
        finally:
            row["owner"] = None
            row["lock"].release()

    def cancel_owner(self, owner) -> None:
        with self.lock:
            for row in self.sessions.values():
                if row["owner"] is owner and hasattr(row["backend"], "pipe"):
                    row["backend"].pipe.close()

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            for row in self.sessions.values():
                if hasattr(row["backend"], "pipe"):
                    row["backend"].pipe.close()

    def snapshot(self) -> list[dict]:
        with self.lock:
            result = list(self.previous)
            for sid, row in self.sessions.items():
                backend = row["backend"]
                pipe = getattr(backend, "pipe", None)
                result.append({"session_id": sid, "backend": row["kind"], "path": str(row["artifact"].path),
                    "artifact_sha256": row["artifact"].sha256, "options": row["options"],
                    "state": "CLOSED" if pipe is None or pipe._closed else getattr(backend, "state", "READY"),
                    "breakpoints": getattr(backend, "breakpoints", []), "live": bool(pipe and not pipe._closed and pipe.proc.poll() is None)})
            return result[-16:]

    def _watch(self) -> None:
        while not self.closed.wait(.1):
            try:
                self.check(self.ctx)
            except Exception:
                self.close()
                return
            with self.lock:
                for row in self.sessions.values():
                    if row["owner"] is None and time.monotonic() - row["used"] > self.ctx.config.binary.idle_timeout and hasattr(row["backend"], "pipe"):
                        row["expired"] = True
                        row["backend"].pipe.close()
