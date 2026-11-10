"""RASC 首版：任务 DAG、能力装配、可复用 Worker 池和任务级恢复。"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from html import escape
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog
from .brain import PentestAgent
from .config import TrinaConfig
from .llm import make_backend
from .state import RunState
from .tools import ToolRegistry, build_registry


CAPABILITIES: dict[str, tuple[str, ...]] = {
    "recon": ("recon",),
    "web": ("web",),
    "verify": ("recon", "web", "vuln", "exploit"),
    "full": ("recon", "web", "vuln", "exploit"),
}
_TASK_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
TERMINAL = {"completed", "blocked", "failed", "skipped"}


@dataclass(frozen=True)
class TaskSpec:
    """跨 Worker 调度的最小研究单元。"""

    id: str
    target: str
    goal: str
    capabilities: tuple[str, ...] = ("full",)
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    max_steps: int = 12
    max_seconds: int = 600

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        payload = dict(data)
        for key in ("capabilities", "depends_on"):
            if key in payload:
                payload[key] = tuple(payload[key])
        return cls(**payload)


@dataclass
class TaskRecord:
    """任务状态仅由 Commander 写入检查点。"""

    status: str = "pending"
    attempt: int = 0
    worker_id: str = ""
    lease_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class Worker:
    """可复用的模型运行资源；任务上下文由每次新建的 Agent 隔离。"""

    id: str
    backend: Any
    busy: bool = False
    lease_id: str = ""
    completed: int = 0


class WorkerPool:
    """用锁保护领取、释放和租约，防止同一 Worker 被重复调度。"""

    def __init__(self, config: TrinaConfig, max_workers: int, *, backend_factory: Callable[..., Any] = make_backend):
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        if max_workers > config.scope.max_host_concurrency:
            raise ValueError("Worker 数不能超过 scope.max_host_concurrency")
        self.config = config
        self.max_workers = max_workers
        self.backend_factory = backend_factory
        self.workers: list[Worker] = []
        self.lock = threading.Lock()

    def acquire(self) -> Worker | None:
        with self.lock:
            for worker in self.workers:
                if not worker.busy:
                    worker.busy = True
                    worker.lease_id = uuid.uuid4().hex
                    return worker
            if len(self.workers) >= self.max_workers:
                return None
            worker = Worker(id=f"worker-{len(self.workers) + 1}", backend=self.backend_factory(self.config.llm))
            worker.busy = True
            worker.lease_id = uuid.uuid4().hex
            self.workers.append(worker)
            return worker

    def release(self, worker: Worker, lease_id: str) -> None:
        with self.lock:
            if not worker.busy or worker.lease_id != lease_id:
                raise RuntimeError("Worker 租约不匹配")
            worker.busy = False
            worker.lease_id = ""
            worker.completed += 1


def validate_tasks(tasks: list[TaskSpec]) -> None:
    """拒绝非法依赖、环、等价任务和未知能力。"""
    ids = {task.id for task in tasks}
    if not tasks or len(ids) != len(tasks):
        raise ValueError("任务不能为空且 ID 必须唯一")
    signatures: set[tuple[Any, ...]] = set()
    for task in tasks:
        if not _TASK_ID.fullmatch(task.id):
            raise ValueError(f"非法任务 ID: {task.id!r}")
        if not task.target or not task.goal or task.max_steps < 1 or task.max_seconds < 1:
            raise ValueError(f"任务 {task.id} 缺少目标或预算非法")
        if not task.capabilities or any(name not in CAPABILITIES for name in task.capabilities):
            raise ValueError(f"任务 {task.id} 存在未知能力")
        if any(dep not in ids or dep == task.id for dep in task.depends_on):
            raise ValueError(f"任务 {task.id} 存在无效依赖")
        signature = (task.target, task.goal.strip().casefold(), tuple(sorted(task.capabilities)))
        if signature in signatures:
            raise ValueError(f"任务 {task.id} 与已有任务等价")
        signatures.add(signature)
    by_id = {task.id: task for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("任务依赖存在环")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in by_id[task_id].depends_on:
            visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)


def default_tasks(target: str, goal: str, *, max_steps: int = 12, max_seconds: int = 600) -> list[TaskSpec]:
    """默认把独立的服务识别与 Web 面研究并行，再汇总验证。"""
    return [
        TaskSpec("recon", target, "识别目标服务、端口与技术栈", ("recon",), priority=20, max_steps=max_steps, max_seconds=max_seconds),
        TaskSpec("web", target, "梳理 Web 功能、路径、参数与客户端线索", ("web",), priority=10, max_steps=max_steps, max_seconds=max_seconds),
        TaskSpec("verify", target, goal or "根据前序发现验证最有价值的研究方向", ("verify",), ("recon", "web"), max_steps=max_steps, max_seconds=max_seconds),
    ]


def load_manifest(path: str | Path) -> list[TaskSpec]:
    """从 YAML/JSON 读取显式任务图。"""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("manifest 必须含 tasks 列表")
    tasks = [TaskSpec.from_dict(item) for item in data["tasks"]]
    validate_tasks(tasks)
    return tasks


class SwarmRuntime:
    """Commander：调度 DAG、提交结构化结果和任务级检查点。"""

    def __init__(
        self,
        config: TrinaConfig,
        tasks: list[TaskSpec],
        *,
        max_workers: int = 2,
        run_id: str | None = None,
        executor: Callable[[Worker, TaskSpec, str, list[str], ToolRegistry], RunState] | None = None,
        backend_factory: Callable[..., Any] = make_backend,
    ):
        validate_tasks(tasks)
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        self.config = config
        self.tasks = list(tasks)
        self.records = {task.id: TaskRecord() for task in tasks}
        self.max_workers = max_workers
        self.run_id = run_id or f"swarm-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if not _TASK_ID.fullmatch(self.run_id):
            raise ValueError("非法 swarm run_id")
        self.workspace = config.workspace_dir / self.run_id
        self.checkpoint = self.workspace / "swarm.json"
        self.audit = AuditLog.open(config.audit_dir, run_id=self.run_id, redact_keys=config.audit.redact_keys, resume=True)
        self.registry = build_registry()
        self.pool = WorkerPool(config, max_workers, backend_factory=backend_factory)
        self.executor = executor or self._execute_agent
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.events: list[dict[str, Any]] = []

    @classmethod
    def resume(cls, config: TrinaConfig, checkpoint: str | Path, *, executor=None, backend_factory=make_backend) -> "SwarmRuntime":
        data = json.loads(Path(checkpoint).read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("不支持的 swarm 检查点版本")
        tasks = [TaskSpec.from_dict(item) for item in data["tasks"]]
        runtime = cls(config, tasks, max_workers=int(data["max_workers"]), run_id=data["run_id"], executor=executor, backend_factory=backend_factory)
        if Path(checkpoint).resolve() != runtime.checkpoint.resolve():
            raise ValueError("检查点路径与当前工作区不匹配")
        for task_id, item in data["records"].items():
            if task_id not in runtime.records:
                raise ValueError(f"检查点包含未知任务: {task_id}")
            record = TaskRecord(**item)
            if record.status == "running":
                record.status = "pending"
                record.worker_id = ""
                record.lease_id = ""
            runtime.records[task_id] = record
        runtime.events = list(data.get("events", []))
        return runtime

    def add_task(self, task: TaskSpec) -> bool:
        """接收新研究分支；等价任务返回 False。运行中仅由 Commander 线程调用。"""
        signature = (task.target, task.goal.strip().casefold(), tuple(sorted(task.capabilities)))
        for current in self.tasks:
            if signature == (current.target, current.goal.strip().casefold(), tuple(sorted(current.capabilities))):
                return False
        validate_tasks(self.tasks + [task])
        self.tasks.append(task)
        self.records[task.id] = TaskRecord()
        self.audit.append("swarm.task.add", task=task.id, depends_on=task.depends_on)
        self._save()
        return True

    def _task_registry(self, task: TaskSpec) -> ToolRegistry:
        categories = {category for capability in task.capabilities for category in CAPABILITIES[capability]}
        return ToolRegistry(tool for spec in self.registry.specs() if spec.category in categories if (tool := self.registry.get(spec.name)) is not None)

    def _hints(self, task: TaskSpec) -> list[str]:
        hints = []
        for dep in task.depends_on:
            result = self.records[dep].result
            if result:
                hints.append(f"前序任务 {dep} 的结构化结果：{json.dumps(result.get('facts', {}), ensure_ascii=False)[:1800]}")
        return hints

    def _execute_agent(self, worker: Worker, task: TaskSpec, run_id: str, hints: list[str], registry: ToolRegistry) -> RunState:
        # 各 Worker 的限额按并发数分配，避免并行后总请求速率扩大。
        worker_config = self.config.with_overrides(
            scope__max_qps=self.config.scope.max_qps / self.max_workers,
            scope__max_host_concurrency=max(1, self.config.scope.max_host_concurrency // self.max_workers),
        )
        agent = PentestAgent(worker_config, backend=worker.backend, registry=registry, stop_event=self.stop_event, pause_event=self.pause_event)
        usage_before = worker.backend.ledger.to_dict()
        state = agent.run(task.target, goal=task.goal, max_steps=task.max_steps, max_seconds=task.max_seconds, seed_hints=hints, run_id=run_id)
        if state.ledger:
            state.ledger = worker.backend.ledger.delta(usage_before).to_dict()
        from .report import build_report

        paths = build_report(state, worker_config, meta={"swarm": self.run_id, "task": task.id}).save()
        state.artifacts["report_paths"] = ", ".join(str(path) for path in paths)
        agent._save_state(state, Path(state.artifacts["workspace"]))
        return state

    @staticmethod
    def _result(state: RunState) -> dict[str, Any]:
        facts = {
            "hosts": [{"host": host, "ports": sorted(hs.ports), "technologies": hs.technologies[:8], "web_paths": hs.web_paths[:20]} for host, hs in state.hosts.items()],
            "findings": [{"title": f.title, "class": f.vuln_class, "target": f.url or f.target, "status": f.status} for f in state.findings],
            "flags": [flag.get("value", "") for flag in state.flags],
        }
        return {
            "run_id": state.run_id,
            "status": state.status,
            "verdict": state.verdict,
            "facts": facts,
            "counters": state.counters,
            "ledger": state.ledger,
            "artifacts": state.artifacts,
            "duration": state.duration,
        }

    def _save(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "run_id": self.run_id,
            "max_workers": self.max_workers,
            "tasks": [asdict(task) for task in self.tasks],
            "records": {key: asdict(value) for key, value in self.records.items()},
            "events": self.events,
        }
        temporary = self.checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.checkpoint)

    def run(self) -> dict[str, Any]:
        """执行所有可运行任务；异常保留在任务记录，其他分支继续。"""
        self._save()
        futures: dict[Future[RunState], tuple[TaskSpec, Worker, str]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                if self.stop_event.is_set() and not futures:
                    break
                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    for task in sorted(list(self.tasks), key=lambda item: (-item.priority, item.id)):
                        record = self.records[task.id]
                        if record.status != "pending":
                            continue
                        deps = [self.records[dep].status for dep in task.depends_on]
                        if any(status in {"failed", "blocked", "skipped"} for status in deps):
                            record.status = "skipped"
                            record.error = "前序任务未完成"
                            self.audit.append("swarm.task.skip", task=task.id, reason=record.error)
                            self._save()
                            continue
                        if any(status != "completed" for status in deps):
                            continue
                        worker = self.pool.acquire()
                        if worker is None:
                            break
                        record.status = "running"
                        record.attempt += 1
                        record.worker_id = worker.id
                        record.lease_id = worker.lease_id
                        record.started_at = time.time()
                        task_run_id = f"{self.run_id}-{task.id}-{record.attempt}"
                        self.events.append({"event": "task.start", "task": task.id, "worker": worker.id, "lease": worker.lease_id, "time": record.started_at})
                        self.audit.append("swarm.task.start", task=task.id, worker=worker.id, lease=worker.lease_id, attempt=record.attempt)
                        self._save()
                        future = executor.submit(self.executor, worker, task, task_run_id, self._hints(task), self._task_registry(task))
                        futures[future] = (task, worker, worker.lease_id)
                if not futures:
                    if all(record.status in TERMINAL for record in self.records.values()):
                        break
                    if self.pause_event.is_set():
                        time.sleep(0.1)
                        continue
                    if self.stop_event.is_set():
                        break
                    raise RuntimeError("任务图无法继续调度")
                done, _ = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    task, worker, lease = futures.pop(future)
                    record = self.records[task.id]
                    if record.lease_id != lease:
                        raise RuntimeError("任务租约不匹配")
                    try:
                        state = future.result()
                        record.result = self._result(state)
                        record.status = state.status if state.status in {"blocked", "failed"} else "completed"
                    except Exception as exc:  # noqa: BLE001 - 单任务失败不影响其余分支
                        record.status = "failed"
                        record.error = f"{type(exc).__name__}: {exc}"
                    finally:
                        record.ended_at = time.time()
                        self.events.append({"event": "task.end", "task": task.id, "worker": worker.id, "status": record.status, "time": record.ended_at})
                        self.audit.append("swarm.task.end", task=task.id, worker=worker.id, status=record.status, error=record.error)
                        self.pool.release(worker, lease)
                        record.lease_id = ""
                        self._save()
        self.audit.append("swarm.run.end", statuses={key: value.status for key, value in self.records.items()})
        self._write_report()
        return self.summary()

    def _write_report(self) -> None:
        """生成协作运行的简要总报告；详细证据保留在 Worker 报告。"""
        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        markdown = self.config.report_dir / f"{self.run_id}_report.md"
        html = self.config.report_dir / f"{self.run_id}_report.html"
        lines = [
            f"# Trina Swarm 报告 · {self.run_id}",
            "",
            f"检查点：`{self.checkpoint}`",
            f"审计：`{self.audit.path}`",
            "",
            "| 任务 | 状态 | Worker | 尝试 | 发现 | Flag |",
            "|---|---|---|---:|---:|---:|",
        ]
        for task in self.tasks:
            record = self.records[task.id]
            facts = record.result.get("facts", {})
            lines.append(
                f"| {task.id} | {record.status} | {record.worker_id or '-'} | {record.attempt} "
                f"| {len(facts.get('findings', []))} | {len(facts.get('flags', []))} |"
            )
        lines.extend(["", "## 任务工件", ""])
        for task in self.tasks:
            record = self.records[task.id]
            artifacts = record.result.get("artifacts", {})
            lines.append(f"### {task.id}")
            lines.append("")
            lines.append(f"- 状态：{record.status}" + (f"；错误：{record.error}" if record.error else ""))
            for name in ("state_file", "audit_path", "report_paths"):
                if artifacts.get(name):
                    lines.append(f"- {name}：`{artifacts[name]}`")
            lines.append("")
        source = "\n".join(lines) + "\n"
        markdown.write_text(source, encoding="utf-8")
        html.write_text(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Trina Swarm</title>"
            "<style>body{font:15px/1.6 system-ui;max-width:1000px;margin:2rem auto;padding:1rem;background:#10151c;color:#e6edf3}pre{white-space:pre-wrap}</style>"
            f"<h1>Trina Swarm 报告</h1><pre>{escape(source)}</pre></html>",
            encoding="utf-8",
        )

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "checkpoint": str(self.checkpoint),
            "audit_path": str(self.audit.path),
            "report_paths": [str(self.config.report_dir / f"{self.run_id}_report.md"), str(self.config.report_dir / f"{self.run_id}_report.html")],
            "tasks": {task.id: {"status": self.records[task.id].status, "worker": self.records[task.id].worker_id, "attempt": self.records[task.id].attempt, "result": self.records[task.id].result, "error": self.records[task.id].error} for task in self.tasks},
            "workers_created": len(self.pool.workers),
            "worker_reuses": sum(max(0, worker.completed - 1) for worker in self.pool.workers),
        }
