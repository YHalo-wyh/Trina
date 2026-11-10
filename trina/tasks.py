"""项目目标与一次性定时任务；原子持久化、跨进程领取和过期租约回收。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


def parse_start(value: str, *, now: datetime | None = None) -> datetime:
    """支持本地 HH:MM、ISO 时间和 +10m；绝对时间必须在未来。"""
    now = now or datetime.now().astimezone()
    value = value.strip()
    relative = re.fullmatch(r"\+(\d+)([smhd])", value)
    if relative:
        seconds = int(relative[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[relative[2]]
        result = now + timedelta(seconds=seconds)
    elif re.fullmatch(r"\d{2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if result <= now:
            result += timedelta(days=1)
    else:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=now.tzinfo)
    if result <= now:
        raise ValueError("开始时间必须在未来")
    return result


@dataclass
class ScheduledTask:
    id: str
    project: str
    run_at: str
    target: str
    goal: str = ""
    max_steps: int = 12
    approve: str = "exploit"
    config_path: str = ""
    status: str = "queued"
    owner: str = ""
    lease_until: float = 0
    run_id: str = ""
    outcome: str = ""
    error: str = ""


class TaskStore:
    """只保存目标、任务参数和状态，不保存供应商 Key 或模型响应。"""
    def __init__(self, path: Path | None = None, *, project: Path | None = None):
        self.path = path or Path(os.environ.get("TRINA_TASKS_FILE") or Path.home() / ".trina" / "tasks.json")
        self.project = str((project or Path.cwd()).resolve())

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "goals": {}, "tasks": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("tasks"), list) or not isinstance(data.get("goals"), dict):
            raise ValueError("目标和定时任务文件格式无效")
        return data

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            until = time.monotonic() + 5
            while True:
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= until:
                        raise ValueError("任务文件正被其他进程更新，请稍后重试")
                    time.sleep(.02)
            try:
                data = self._read()
                yield data
                fd, name = tempfile.mkstemp(prefix=".tasks-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as output:
                        json.dump(data, output, ensure_ascii=False, indent=2)
                        output.write("\n")
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(name, 0o600)
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def goal(self) -> str:
        return str(self._read()["goals"].get(self.project, ""))

    def set_goal(self, value: str) -> None:
        if len(value) > 8000:
            raise ValueError("目标最多 8000 字符")
        with self._transaction() as data:
            if value.strip():
                data["goals"][self.project] = value.strip()
            else:
                data["goals"].pop(self.project, None)

    def add(self, target: str, when: str, *, goal: str | None = None, max_steps: int = 12,
            approve: str = "exploit", config=None, now: datetime | None = None) -> ScheduledTask:
        from .scope import ScopeGuard
        from .config import TrinaConfig, DEFAULT_CONFIG_PATH
        from .session import APPROVAL_MODES
        config = config or TrinaConfig.load()
        if not target or max_steps < 1 or approve not in APPROVAL_MODES:
            raise ValueError("定时任务需要目标、正数步数和有效审批模式")
        decision = ScopeGuard(config.scope).check_target(target)
        if decision.denied:
            raise ValueError(decision.reason)
        start = parse_start(when, now=now)
        task = ScheduledTask(uuid.uuid4().hex[:12], self.project, start.isoformat(), target,
                             self.goal() if goal is None else goal, max_steps, approve,
                             str(config.path) if config.path and config.path.resolve() != DEFAULT_CONFIG_PATH.resolve() else "")
        with self._transaction() as data:
            data["tasks"].append(asdict(task))
        return task

    def list(self) -> list[ScheduledTask]:
        return [ScheduledTask(**item) for item in self._read()["tasks"] if item["project"] == self.project]

    def cancel(self, task_id: str) -> None:
        with self._transaction() as data:
            for item in data["tasks"]:
                if item["id"] == task_id and item["project"] == self.project:
                    if item["status"] != "queued":
                        raise ValueError("只能取消尚未启动的任务；运行中使用 /stop")
                    item["status"] = "cancelled"
                    return
            raise ValueError("定时任务不存在")

    def claim(self, owner: str, *, now: float | None = None) -> ScheduledTask | None:
        now = time.time() if now is None else now
        relevant = [row for row in self._read()["tasks"] if row["project"] == self.project]
        if not any((row["status"] == "queued" and datetime.fromisoformat(row["run_at"]).timestamp() <= now)
                   or (row["status"] == "running" and row["lease_until"] < now) for row in relevant):
            return None
        with self._transaction() as data:
            due = []
            for item in data["tasks"]:
                if item["project"] != self.project:
                    continue
                if item["status"] == "running" and item["lease_until"] < now:
                    item.update(status="interrupted", error="执行进程未续约；请检查运行记录后重新安排")
                if item["status"] == "queued" and datetime.fromisoformat(item["run_at"]).timestamp() <= now:
                    due.append(item)
            if not due:
                return None
            item = min(due, key=lambda row: row["run_at"])
            item.update(status="running", owner=owner, lease_until=now + 60)
            return ScheduledTask(**item)

    def finish(self, task_id: str, owner: str, *, run_id: str = "", outcome: str = "", error: str = "") -> None:
        with self._transaction() as data:
            for item in data["tasks"]:
                if item["id"] == task_id and item["owner"] == owner and item["status"] == "running":
                    item.update(status="failed" if error else "completed", run_id=run_id, outcome=outcome,
                                error=error[:400], lease_until=0)
                    return
            raise ValueError("任务租约已失效，不能重复完成")

    def renew(self, owner: str) -> None:
        with self._transaction() as data:
            for item in data["tasks"]:
                if item["owner"] == owner and item["status"] == "running":
                    item["lease_until"] = time.time() + 60


def execute_scheduled(task: ScheduledTask, *, log: Callable[[str], None] = print):
    """到点后重新载入配置并复用正常执行链；无人值守审批默认拒绝。"""
    from .config import TrinaConfig
    from .play import build_plan, run_plan
    from .scope import ScopeGuard
    from .session import level_needs_approval
    config = TrinaConfig.load(task.config_path or None)
    decision = ScopeGuard(config.scope).check_target(task.target)
    if decision.denied:
        raise ValueError(decision.reason)
    plan = build_plan(config, target=task.target, goal=task.goal, autostart_lab=False)
    return run_plan(config, plan, max_steps=task.max_steps, log=log,
                    approval=lambda request: not level_needs_approval(task.approve, str(request.get("level", "detect"))))


class TaskScheduler:
    """每个项目串行领取；租约防重复，重启不自动重试已运行任务。"""
    def __init__(self, store: TaskStore, execute=execute_scheduled, *, ready=lambda: True, log=print):
        self.store, self.execute, self.ready, self.log = store, execute, ready, log
        self.owner = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def tick(self, *, now: float | None = None) -> bool:
        if not self.ready():
            return False
        task = self.store.claim(self.owner, now=now)
        if task is None:
            return False
        keepalive = threading.Event()
        def renew():
            while not keepalive.wait(10):
                self.store.renew(self.owner)
        self._heartbeat = threading.Thread(target=renew, daemon=True)
        self._heartbeat.start()
        try:
            self.log(f"定时任务 {task.id} 开始：{task.target}")
            state = self.execute(task)
            self.store.finish(task.id, self.owner, run_id=getattr(state, "run_id", ""), outcome=getattr(state, "status", ""))
        except Exception as exc:
            try:
                self.store.finish(task.id, self.owner, error=f"{type(exc).__name__}: {exc}")
            except ValueError as lease_error:
                self.log(f"定时任务 {task.id} 的租约已失效：{lease_error}")
            self.log(f"定时任务 {task.id} 失败：{exc}")
        finally:
            keepalive.set()
            self._heartbeat.join(1)
        return True

    def serve(self, *, interval: float = 1) -> None:
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(interval)
