"""Commander：任务 DAG、通用 Worker、Capability Fabric 与任务级恢复。"""

from __future__ import annotations

import json
import copy
import os
import queue
import re
import threading
import time
import uuid
from html import escape
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .audit import AuditLog
from .brain import PentestAgent
from .capabilities import CapabilityCatalog, CapabilityFabric, CapabilityRuntime
from .config import LLMConfig, TrinaConfig
from .coordination import CoordinationRuntime, task_signature
from .events import EventBus
from .context import BRIEF_VERSION
from .intent_planner import FactIntentPlanner
from .llm import make_backend
from .state import RunState
from .strategy_memory import StrategyMemory
from .profiling import AgentProfiler
from .tools import ToolRegistry, build_registry


_TASK_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
TERMINAL = {"completed", "blocked", "failed", "skipped", "timed_out", "cancelled"}
WORKER_RESULT_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class TaskSpec:
    """跨 Worker 调度的最小研究单元。"""

    id: str
    target: str
    goal: str
    # 可选的 Capability ID / provides 约束；为空时由 Fabric 按目标和意图发现。
    capabilities: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    max_steps: int = 0
    max_seconds: int = 0
    model: str = ""
    reasoning_effort: str = ""
    profile: str = ""
    max_attempts: int = 1
    context_key: str = ""
    deliverable: str = ""
    aggregate: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        payload = dict(data)
        for key in ("capabilities", "depends_on"):
            if key in payload:
                payload[key] = tuple(payload[key])
        return cls(**payload)


@dataclass
class TaskRecord:
    """协调中心 Intent 的字段视图；本身不保存第二份任务状态。"""

    status: str = "pending"
    attempt: int = 0
    worker_id: str = ""
    lease_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    capability_lease_id: str = ""
    capability_ids: list[str] = field(default_factory=list)
    capability_snapshot: list[dict[str, str]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    model: str = ""
    reasoning_effort: str = ""
    profile: str = ""

    # 保留旧 status 值，UI 可按需读取更细的调度阶段和资源信息。
    lifecycle: str = "QUEUED"
    effective_progress: int = 0
    last_progress_at: float = 0.0
    model_requests: int = 0
    tokens: int = 0
    execution_seconds: float = 0.0
    queue_seconds: float = 0.0

    def bind(self, row: dict[str, Any]) -> "TaskRecord":
        """把属性直接绑定到权威 Intent 行，兼容原有 ``record.status`` 调用。"""
        defaults = {item.name: getattr(self, item.name) for item in fields(self)}
        for key, value in defaults.items():
            row.setdefault(key, value)
        self.__dict__ = row
        return self


@dataclass
class Worker:
    """可复用的模型运行资源；每个任务仍创建独立 Agent 与事实状态。"""

    id: str
    backend: Any
    busy: bool = False
    lease_id: str = ""
    completed: int = 0
    capability_lease_id: str = ""
    capabilities: tuple[str, ...] = ()
    llm: LLMConfig | None = None
    idle_since: float = field(default_factory=time.monotonic)
    idle_seconds: float = 0.0
    model_requests: int = 0
    tokens: int = 0


def task_llm(config: TrinaConfig, task: TaskSpec | None = None) -> LLMConfig:
    """任务覆盖 > 子 Worker 默认 > 主会话；供应商凭据仍由现有 ProfileStore 管理。"""
    llm = copy.deepcopy(config.llm)
    for source in (config.subworker, task):
        if source is None:
            continue
        if source.profile and llm.backend != "replay":
            from .providers import ProfileStore
            store = ProfileStore()
            if source.profile not in store.profiles:
                raise ValueError(f"未保存的供应商: {source.profile}；先在 /model 添加")
            store.apply(llm, source.profile)
        if source.model and source.model != llm.model:
            llm.model, llm.reasoning_effort, llm.context_window = source.model, "default", 0
        if source.reasoning_effort:
            llm.reasoning_effort = source.reasoning_effort
    llm.validate()
    from .llm.reasoning import reasoning_support
    if llm.backend != "replay" and llm.reasoning_effort not in reasoning_support(llm).levels:
        raise ValueError(f"{llm.model} 不支持思考档位 {llm.reasoning_effort}")
    return llm


def _execution_signature(task: TaskSpec, llm: LLMConfig) -> str:
    """默认值与显式同值按同一执行条件去重，恢复后身份保持一致。"""
    return task_signature(replace(task, model=llm.model, profile=llm.profile, reasoning_effort=llm.reasoning_effort))


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

    def available(self) -> int:
        """返回当前可领取槽位，保持既有并发上限。"""
        with self.lock:
            return max(0, self.max_workers - sum(worker.busy for worker in self.workers))

    def acquire(self, llm: LLMConfig | None = None) -> Worker | None:
        requested = llm or self.config.llm
        with self.lock:
            # 同配置优先复用；闲置槽切换后端时不携带前任务的历史或账本。
            for worker in sorted(self.workers, key=lambda item: item.llm != requested):
                if not worker.busy:
                    if worker.llm != requested:
                        worker.backend = self.backend_factory(requested)
                        worker.llm = copy.deepcopy(requested)
                    worker.busy = True
                    worker.idle_seconds += max(0.0, time.monotonic() - worker.idle_since)
                    worker.lease_id = uuid.uuid4().hex
                    return worker
            if len(self.workers) >= self.max_workers:
                return None
            worker = Worker(id=f"worker-{len(self.workers) + 1}", backend=self.backend_factory(requested), llm=copy.deepcopy(requested))
            worker.busy = True
            worker.lease_id = uuid.uuid4().hex
            self.workers.append(worker)
            return worker

    def release(self, worker: Worker, lease_id: str) -> None:
        with self.lock:
            if not worker.busy or worker.lease_id != lease_id:
                raise RuntimeError("Worker 租约不匹配")
            if worker.capability_lease_id:
                raise RuntimeError("Worker 的 Capability 租约尚未回收")
            worker.busy = False
            worker.lease_id = ""
            worker.completed += 1
            worker.idle_since = time.monotonic()


class _TaskStop:
    """将整场停止与当前任务停止合成 Agent 已有的 Event 接口。"""
    def __init__(self, global_event, local_event):
        self.global_event, self.local_event = global_event, local_event

    def is_set(self):
        return self.global_event.is_set() or self.local_event.is_set()

    def wait(self, timeout=None):
        end = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = end - time.monotonic() if end is not None else .05
            if remaining <= 0:
                return False
            self.local_event.wait(min(.05, remaining))
        return True


def validate_tasks(tasks: list[TaskSpec]) -> None:
    """拒绝非法依赖、环和等价任务；能力是否存在由运行时 Catalog 判定。"""
    ids = {task.id for task in tasks}
    if not tasks or len(ids) != len(tasks):
        raise ValueError("任务不能为空且 ID 必须唯一")
    signatures: set[str] = set()
    for task in tasks:
        if not _TASK_ID.fullmatch(task.id):
            raise ValueError(f"非法任务 ID: {task.id!r}")
        if (not task.target or not task.goal or
                any(type(value) is not int or value < 0 for value in (task.max_steps, task.max_seconds))):
            raise ValueError(f"任务 {task.id} 缺少目标或预算非法")
        if any(not isinstance(value, str) for value in (task.model, task.reasoning_effort, task.profile)):
            raise ValueError(f"任务 {task.id} 模型配置必须为字符串")
        if type(task.max_attempts) is not int or task.max_attempts < 1:
            raise ValueError(f"任务 {task.id} 的 max_attempts 必须为正整数")
        if any(not isinstance(value, str) for value in (task.context_key, task.deliverable)) or type(task.aggregate) is not bool:
            raise ValueError(f"任务 {task.id} 的上下文或交付约束非法")
        if task.aggregate and not task.depends_on:
            raise ValueError("汇总任务必须依赖实际工作分支")
        if any(not isinstance(name, str) or not name.strip() for name in task.capabilities):
            raise ValueError(f"任务 {task.id} 的能力约束非法")
        if any(dep not in ids or dep == task.id for dep in task.depends_on):
            raise ValueError(f"任务 {task.id} 存在无效依赖")
        signature = task_signature(task)
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


def default_tasks(target: str, goal: str, *, max_steps: int = 0, max_seconds: int = 0) -> list[TaskSpec]:
    """默认只创建一个目标任务，不预设 Recon/Web/Verify 专职 Worker。"""
    return [TaskSpec("main", target, goal or "分析目标并完成可验证结果", (), max_steps=max_steps, max_seconds=max_seconds)]


def _target_identity(value: str) -> tuple[str, int | None]:
    text = (value or "").strip()
    parsed = urlsplit(text if "://" in text else "//" + text)
    try:
        return (str(parsed.hostname or text).casefold(), parsed.port)
    except ValueError:
        return (str(parsed.hostname or text).casefold(), None)


def _goal_terms(value: str) -> set[str]:
    text = (value or "").casefold()
    terms = set(re.findall(r"[a-z0-9_.+-]{2,}", text))
    stop = {"目标", "验证", "检查", "分析", "发现", "结果", "任务", "前序"}
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return {item for item in terms if item not in stop}


def _task_similarity(source: TaskSpec, destination: TaskSpec) -> tuple[float, list[str]]:
    """Commander 使用目标、依赖和目标语义判断是否值得转发情报。"""
    score = 0.0
    reasons: list[str] = []
    source_host, source_port = _target_identity(source.target)
    destination_host, destination_port = _target_identity(destination.target)
    if source_host and source_host == destination_host:
        score = 0.95 if source_port == destination_port else 0.9
        reasons.append("目标主机相同" if source_port != destination_port else "规范化目标相同")
    if source.id in destination.depends_on:
        score = max(score, 0.75)
        reasons.append("当前任务显式依赖来源任务")
    left, right = _goal_terms(source.goal), _goal_terms(destination.goal)
    overlap = left & right
    if source.goal.strip().casefold() == destination.goal.strip().casefold():
        score = max(score, 0.7)
        reasons.append("任务目标相同")
    elif len(overlap) >= 2 and len(overlap) / max(1, min(len(left), len(right))) >= 0.5:
        score = max(score, 0.65)
        reasons.append("验证方向高度相似：" + "、".join(sorted(overlap)[:5]))
    return round(score, 2), reasons


def _dispatch_similar(source: TaskSpec, destination: TaskSpec) -> bool:
    """识别不应同时派发的近似工作，不阻止同一目标上的独立方向。"""
    if source.context_key != destination.context_key or source.depends_on != destination.depends_on:
        return False
    if _target_identity(source.target) != _target_identity(destination.target):
        return False
    if source.capabilities and destination.capabilities and not set(source.capabilities) & set(destination.capabilities):
        return False
    left, right = _goal_terms(source.goal), _goal_terms(destination.goal)
    if not left or not right:
        return False
    return len(left & right) / max(1, min(len(left), len(right))) >= 0.75


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
        fabric: CapabilityFabric | None = None,
        capability_dirs: list[str | Path] | None = None,
        write_report: bool = True,
        auto_expand: bool = False,
        max_generated_tasks: int | None = None,
        strategy_memory: StrategyMemory | None = None,
        _restoring: bool = False,
    ):
        validate_tasks(tasks)
        planner = FactIntentPlanner()
        original_tasks = list(tasks)
        if not _restoring:
            tasks = [part for task in tasks for part in self._decompose_task(task, planner)]
            validate_tasks(tasks)
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        self.config = config
        self.tasks = list(tasks)
        self._task_llms = {task.id: task_llm(config, task) for task in tasks}
        signatures = [_execution_signature(task, self._task_llms[task.id]) for task in tasks]
        if len(set(signatures)) != len(signatures):
            raise ValueError("任务与已有任务等价")
        self.records = {task.id: TaskRecord(model=self._task_llms[task.id].model,
                       reasoning_effort=self._task_llms[task.id].reasoning_effort,
                       profile=self._task_llms[task.id].profile) for task in tasks}
        self.max_workers = max_workers
        self.run_id = run_id or f"swarm-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if not _TASK_ID.fullmatch(self.run_id):
            raise ValueError("非法 swarm run_id")
        self.workspace = config.workspace_dir / self.run_id
        self.checkpoint = self.workspace / "swarm.json"
        self.audit = AuditLog.open(config.audit_dir, run_id=self.run_id, redact_keys=config.audit.redact_keys, resume=True)
        self.registry = build_registry()
        if fabric is None:
            catalog = CapabilityCatalog.from_registry(self.registry)
            configured = [item for item in os.environ.get("TRINA_CAPABILITY_DIRS", "").split(os.pathsep) if item]
            directories = capability_dirs or [config.runtime_root / "capabilities", Path.home() / ".trina" / "capabilities", *configured]
            catalog.discover(directories)
            fabric = CapabilityFabric(catalog, CapabilityRuntime(self.registry))
        self.fabric = fabric
        self.pool = WorkerPool(config, max_workers, backend_factory=backend_factory)
        self.executor = executor or self._execute_agent
        self.write_report = bool(write_report)
        self.auto_expand = bool(auto_expand)
        self.max_generated_tasks = None if max_generated_tasks is None else max(0, int(max_generated_tasks))
        self.intent_planner = planner
        self.strategy_memory = (strategy_memory or StrategyMemory(config.workspace_dir / ".strategy-memory.sqlite3")) if config.run.strategy_memory else None
        self._memory_cache: dict[str, tuple[int, list[dict]]] = {}
        self._memory_lock = threading.Lock()
        self.memory_metrics = {"queries": 0, "hits": 0, "stored": 0, "query_seconds": 0.0, "errors": 0}
        self._last_assessment = 0.0
        self._run_finished_at = 0.0
        self._state_projections: dict[str, tuple[str, dict[str, Any]]] = {}
        self._published_results: dict[str, tuple[str, int, float]] = {}
        self.stop_event = threading.Event()
        self._task_stops = {task.id: threading.Event() for task in tasks}
        self._task_steers = {task.id: queue.Queue() for task in tasks}
        self.pause_event = threading.Event()
        self.on_update: Callable[[dict[str, Any]], None] | None = None
        self.on_progress: Callable[[Any], None] | None = None
        self._inbox: queue.Queue[tuple[Any, Future[bool]]] = queue.Queue()
        self._lifecycle_lock = threading.Lock()
        self._accepting = True
        self.events: list[dict[str, Any]] = []
        self.event_bus = EventBus(self.workspace / "events.sqlite3", self.run_id, redact_keys=config.audit.redact_keys)
        profile_name = Path(str(getattr(config.run, "profile_output", "performance.json"))).name or "performance.json"
        self.profiler = AgentProfiler.from_env(
            self.run_id,
            worker_id="commander",
            enabled=bool(getattr(config.run, "profiling", True)),
        )
        self.profile_path = self.workspace / f"commander-{profile_name}"
        self.profile_subscription = self.event_bus.subscribe(
            self.profiler.consume_event,
            after=self.event_bus.last_seq,
            kinds=("model.*", "tool.*", "task.*", "worker.*"),
        )
        self.coordination = CoordinationRuntime(self.tasks, event_bus=None if _restoring else self.event_bus)
        self._bind_records()
        self._commander_events = self.event_bus.subscribe(name="commander", after=0)
        if not _restoring:
            for task in original_tasks:
                group = next((item for item in tasks if item.id == task.id and item.aggregate), None)
                if group is not None:
                    self.coordination.emit("plan.decomposed", intent_id=task.id,
                        reason="明确工作方向与交付条件，一次性建立依赖图", tasks=[asdict(item) for item in tasks if item.id in group.depends_on])

    @staticmethod
    def _decompose_task(task: TaskSpec, planner: FactIntentPlanner) -> list[TaskSpec]:
        if task.aggregate:
            return [task]
        proposals = planner.decompose(task)
        if not proposals or (task.max_steps and task.max_steps < len(proposals)):
            return [task]
        children = [replace(task, **proposal, context_key=task.context_key,
                            max_steps=max(1, task.max_steps // len(proposals)) if task.max_steps else 0)
                    for proposal in proposals]
        group = replace(task, depends_on=tuple(item.id for item in children), aggregate=True,
                        deliverable=task.deliverable or "汇总各分支的已验证结果和未解决问题")
        # 依赖外部节点由调用方完整图校验；内部环则保守退回单任务。
        try:
            validate_tasks([replace(item, depends_on=tuple(dep for dep in item.depends_on if dep not in task.depends_on)) for item in children] + [group])
        except ValueError:
            return [task]
        return children + [group]

    def _bind_records(self) -> None:
        """所有 TaskRecord 绑定同一 Intent 字典；Todo/图仅是只读投影视图。"""
        for task_id, record in self.records.items():
            record.bind(self.coordination.intents[task_id])
        for task in self.tasks:
            self.coordination.intents[task.id]["signature"] = _execution_signature(task, self._task_llms[task.id])

    @classmethod
    def resume(
        cls,
        config: TrinaConfig,
        checkpoint: str | Path,
        *,
        executor=None,
        backend_factory=make_backend,
        fabric: CapabilityFabric | None = None,
        capability_dirs: list[str | Path] | None = None,
    ) -> "SwarmRuntime":
        data = json.loads(Path(checkpoint).read_text(encoding="utf-8"))
        version = int(data.get("version", 0))
        if version not in (1, 2, 3, 4):
            raise ValueError("不支持的 swarm 检查点版本")
        tasks = [TaskSpec.from_dict(item) for item in data["tasks"]]
        runtime = cls(
            config,
            tasks,
            max_workers=int(data["max_workers"]),
            run_id=data["run_id"],
            executor=executor,
            backend_factory=backend_factory,
            fabric=fabric,
            capability_dirs=capability_dirs,
            write_report=bool(data.get("write_report", True)),
            auto_expand=bool(data.get("auto_expand", False)),
            max_generated_tasks=data.get("max_generated_tasks"),
            _restoring=True,
        )
        if Path(checkpoint).resolve() != runtime.checkpoint.resolve():
            raise ValueError("检查点路径与当前工作区不匹配")
        # 事件事务先于 JSON 检查点提交，崩溃留下的旧 JSON 可从事件快照补齐。
        after = int(data.get("checkpoint_seq", 0))
        while batch := runtime.event_bus.replay(after, kinds=("runtime.checkpoint",)):
            data = runtime.event_bus.runtime_payload(batch[-1])["snapshot"]
            after = batch[-1].seq
        tasks = [TaskSpec.from_dict(item) for item in data["tasks"]]
        validate_tasks(tasks)
        runtime.tasks = tasks
        runtime._task_stops = {task.id: threading.Event() for task in tasks}
        runtime._task_steers = {task.id: queue.Queue() for task in tasks}
        runtime._task_llms = {task.id: task_llm(config, task) for task in tasks}
        runtime._commander_events.cursor = int(data.get("commander_cursor", 0))
        runtime.events = list(data.get("events", []))
        runtime.coordination = CoordinationRuntime.restore(tasks, data.get("coordination"))
        runtime.coordination.event_bus = runtime.event_bus
        if version < 4:
            legacy = data.get("records") or {}
            unknown = set(legacy) - {task.id for task in tasks}
            if unknown:
                raise ValueError(f"检查点包含未知任务: {sorted(unknown)[0]}")
            runtime.coordination.import_legacy_records(legacy)
        runtime.records = {task.id: TaskRecord() for task in tasks}
        runtime._bind_records()
        for task_id, record in runtime.records.items():
            llm = runtime._task_llms[task_id]
            record.model = record.model or llm.model
            record.reasoning_effort = record.reasoning_effort or llm.reasoning_effort
            record.profile = record.profile or llm.profile
        runtime._consume_events()
        runtime.event_bus.interrupt_open_calls()
        runtime.coordination.recover_inflight()
        for record in runtime.records.values():
            if record.status == "pending":
                record.worker_id = record.lease_id = record.capability_lease_id = ""
        return runtime

    def add_task(self, task: TaskSpec) -> bool:
        """接收新研究分支；等价任务返回 False。运行中仅由 Commander 线程调用。"""
        proposed = self._decompose_task(task, self.intent_planner)
        llms = {item.id: task_llm(self.config, item) for item in proposed}
        for candidate in proposed:
            signature = _execution_signature(candidate, llms[candidate.id])
            if any(signature == item["signature"] for item in self.coordination.intents.values()):
                self.coordination.duplicate_tasks += 1
                self.coordination.emit("intent.duplicate", intent_id=task.id, reason="目标、输入上下文及执行条件相同")
                return False
        validate_tasks(self.tasks + proposed)
        for item in proposed:
            llm = llms[item.id]
            self.tasks.append(item)
            self.coordination.add_intent(item)
            self.coordination.intents[item.id]["signature"] = _execution_signature(item, llm)
            self.records[item.id] = TaskRecord().bind(self.coordination.intents[item.id])
            self._task_llms[item.id] = llm
            self._task_stops[item.id] = threading.Event()
            self._task_steers[item.id] = queue.Queue()
            self.records[item.id].model, self.records[item.id].reasoning_effort = llm.model, llm.reasoning_effort
            self.records[item.id].profile = llm.profile
            self.audit.append("swarm.task.add", task=item.id, depends_on=item.depends_on)
        if len(proposed) > 1:
            self.coordination.emit("plan.decomposed", intent_id=task.id, reason="新任务包含明确工作方向", tasks=[asdict(item) for item in proposed])
        self._save()
        return True

    def _expand_from_facts(self, source_id: str, fact_ids: list[str]) -> list[str]:
        """Commander 根据新事实生成有界后继任务；Worker 不直接修改任务图。"""
        if not self.auto_expand or not fact_ids or self.max_generated_tasks == 0:
            return []
        facts = self.coordination.unplanned_facts(fact_ids)
        if not facts:
            return []
        source = next((item for item in self.tasks if item.id == source_id), None)
        if source is None:
            return []
        existing_generated = sum(item.id.startswith("auto-") for item in self.tasks)
        remaining = (max(0, self.max_generated_tasks - existing_generated)
                     if self.max_generated_tasks is not None else None)
        accepted: list[str] = []
        # 自动后继共用显式任务的规划键，重观测同一资产不能递归生成新任务。
        by_id = {task.id: task for task in self.tasks}
        origin = source
        seen: set[str] = set()
        while origin.id.startswith("auto-") and origin.depends_on and origin.id not in seen:
            seen.add(origin.id)
            parent = by_id.get(origin.depends_on[0])
            if parent is None:
                break
            origin = parent
        try:
            for proposal in self.intent_planner.propose(source, facts, planning_scope=origin.id):
                if remaining is not None and len(accepted) >= remaining:
                    break
                task = TaskSpec(
                    proposal.id, proposal.target, proposal.goal,
                    capabilities=proposal.capabilities, depends_on=proposal.depends_on,
                    priority=proposal.priority, max_steps=proposal.max_steps,
                    max_seconds=proposal.max_seconds, model=source.model,
                    reasoning_effort=source.reasoning_effort, profile=source.profile,
                    context_key=source.context_key, deliverable=proposal.goal,
                )
                if self.add_task(task):
                    accepted.append(task.id)
                    self.audit.append("planner.intent.generated", source=source_id, task=task.id,
                                      facts=list(proposal.fact_ids), goal=task.goal)
        finally:
            # 无适用提案和已被等价任务去重都属于已处理，避免每个事件重复规划。
            self.coordination.mark_facts_planned(str(item["id"]) for item in facts)
        return accepted

    def submit_task(self, task: TaskSpec) -> Future[bool]:
        """跨线程提交，只有 Commander 可修改 DAG；收尾时明确拒绝，避免丢任务。"""
        return self._submit(task)

    def configure_task(self, task_id: str, **overrides: str) -> Future[bool]:
        return self._submit((task_id, overrides))

    def control_task(self, task_id: str, action: str, text: str = "") -> Future[bool]:
        return self._submit((task_id, {"_control": action, "text": text}))

    def _submit(self, command: Any) -> Future[bool]:
        result: Future[bool] = Future()
        with self._lifecycle_lock:
            if not self._accepting or self.stop_event.is_set():
                result.set_exception(RuntimeError("Subworker 已收尾；请启动新任务"))
                return result
            self._inbox.put((command, result))
        self.event_bus.publish("runtime.wake", source="console")
        return result

    def _drain_submissions(self) -> None:
        while True:
            try:
                task, result = self._inbox.get_nowait()
            except queue.Empty:
                break
            if not result.set_running_or_notify_cancel():
                continue
            try:
                if self.stop_event.is_set():
                    raise RuntimeError("Subworker 已停止接收任务")
                if isinstance(task, tuple):
                    task_id, overrides = task
                    if "_control" in overrides:
                        record = self.records.get(task_id)
                        if record is None or record.status not in {"pending", "running"}:
                            raise ValueError("任务不存在或已结束")
                        action, text = overrides["_control"], overrides.get("text", "")
                        if action == "message":
                            if not text.strip() or len(text) > 8000:
                                raise ValueError("补充必须为 1..8000 字")
                            self._task_steers[task_id].put(text)
                        elif action == "cancel":
                            self._task_stops[task_id].set()
                            if record.status == "pending":
                                self.coordination.terminate_unclaimed(task_id, "cancelled", "用户取消，尚未执行")
                            spec = next(item for item in self.tasks if item.id == task_id)
                            if spec.aggregate:
                                for child_id in spec.depends_on:
                                    self._task_stops[child_id].set()
                                    if self.records[child_id].status == "pending":
                                        self.coordination.terminate_unclaimed(child_id, "cancelled", "用户取消父任务")
                        else:
                            raise ValueError("未知任务控制")
                        self.audit.append("swarm.task." + action, task=task_id, text=text)
                        self.event_bus.publish("task.message" if action == "message" else "task.cancel_requested",
                                               source="commander", task_id=task_id, text=text)
                        self._save()
                        result.set_result(True)
                        continue
                    if set(overrides) - {"model", "reasoning_effort", "profile"}:
                        raise ValueError("只能调整模型、思考强度和供应商")
                    if task_id not in self.records or self.records[task_id].status != "pending":
                        raise ValueError("只可调整等待中的任务；运行中配置保持不变")
                    index = next(i for i, item in enumerate(self.tasks) if item.id == task_id)
                    updated = replace(self.tasks[index], **overrides)
                    validate_tasks([updated if item.id == task_id else item for item in self.tasks])
                    llm = task_llm(self.config, updated)
                    signature = _execution_signature(updated, llm)
                    if any(key != task_id and item["signature"] == signature for key, item in self.coordination.intents.items()):
                        raise ValueError("配置后与已有任务等价")
                    self.tasks[index], self._task_llms[task_id] = updated, llm
                    record = self.records[task_id]
                    record.model, record.reasoning_effort, record.profile = llm.model, llm.reasoning_effort, llm.profile
                    self.coordination.intents[task_id]["signature"] = signature
                    self.audit.append("swarm.task.configure", task=task_id, **overrides)
                    self._save()
                    result.set_result(True)
                else:
                    result.set_result(self.add_task(task))
            except Exception as exc:
                result.set_exception(exc)

    def _task_registry(self, task: TaskSpec, worker: Worker) -> ToolRegistry:
        """按意图给通用 Worker 建立有限期能力租约。"""
        record = self.records[task.id]
        constraints = {"selectors": task.capabilities, "max_count": 6}
        if not task.capabilities:
            from urllib.parse import unquote
            local = unquote(urlsplit(task.target).path) if task.target.startswith("file://") else task.target
            if Path(local).is_file():
                # 通用 Worker 根据附件领取语义能力，保留自由选择静态、动态或脚本的权利。
                constraints["selectors"] = ("category.binary", "tool.terminal_start", "tool.terminal_status", "tool.terminal_stop")
        lease = self.fabric.lease(
            worker.id,
            task.id,
            f"{task.goal}\n目标: {task.target}",
            constraints=constraints,
            ttl=task.max_seconds + 30 if task.max_seconds else 0,
            pinned=record.capability_snapshot or None,
        )
        record.capability_lease_id = lease.id
        record.capability_ids = [item.id for item in lease.capabilities]
        record.capability_snapshot = lease.snapshot
        worker.capability_lease_id = lease.id
        worker.capabilities = tuple(record.capability_ids)
        self.audit.append(
            "capability.lease",
            task=task.id,
            worker=worker.id,
            lease=lease.id,
            capabilities=record.capability_snapshot,
        )
        # 编码能力和协作读取属于所有通用 Worker 的基础能力，不随领域能力租约裁掉。
        from .tools.coordination import TOOLS as COORDINATION_TOOLS
        from .tools.workspace import TOOLS as WORKSPACE_TOOLS
        from .tools.casebook import TOOLS as CASEBOOK_TOOLS

        for tool in [*COORDINATION_TOOLS, *WORKSPACE_TOOLS, *CASEBOOK_TOOLS]:
            if tool.spec.name in {"worker_tasks", "add_intent", "steer_work", "kill_work"}:
                continue  # 委派宿主只属于 Commander，不向普通 Worker 加载递归分工 schema。
            if lease.registry.get(tool.spec.name) is None:
                lease.registry.register(tool)
        return lease.registry

    def _hints(self, task: TaskSpec) -> list[str]:
        """给自定义 executor 的兼容入口，只传 Commander 选中的情报简报。"""
        return [json.dumps(item, ensure_ascii=False, default=str) for item in self._briefs(task)]

    def _briefs(self, task: TaskSpec) -> list[dict[str, Any]]:
        """主动发现已完成题目与当前题目的相似性，并生成定向情报。"""
        by_id = {item.id: item for item in self.tasks}
        briefs: list[dict[str, Any]] = self._memory_briefs(task)
        for source_id, record in self.records.items():
            if source_id == task.id or record.status not in TERMINAL:
                continue
            intelligence = record.result.get("intelligence")
            source = by_id.get(source_id)
            if source is None:
                continue
            score, reasons = _task_similarity(source, task)
            if score < 0.6:
                continue
            experience = self.coordination.experience_for(source_id)
            if not isinstance(intelligence, dict):
                intelligence = {}
            if not intelligence and not any(experience.values()) and not record.error:
                continue
            briefs.append({
                "version": BRIEF_VERSION,
                "source_task": source_id,
                "source_run_id": record.result.get("run_id", ""),
                "source_target": source.target,
                "destination_task": task.id,
                "similarity": score,
                "reasons": reasons,
                "evidence_refs": list(record.evidence_refs),
                "intelligence": intelligence,
                "experience": experience,
                "source_status": record.status,
                "source_error": record.error[:300],
            })
        return sorted(briefs, key=lambda item: (-float(item["similarity"]), str(item["source_task"])))

    def _memory_briefs(self, task: TaskSpec) -> list[dict[str, Any]]:
        """复用 Commander 简报通道；同一任务/经验库版本只检索一次。"""
        memory = self.strategy_memory
        if memory is None or not memory.revision:
            return []
        with self._memory_lock:
            revision = memory.revision
            cached = self._memory_cache.get(task.id)
            if cached is not None and cached[0] == revision:
                return list(cached[1])
            started = time.perf_counter()
            try:
                briefs = memory.retrieve(task)
            except Exception as exc:  # 经验库异常不能阻止真实任务执行。
                self.memory_metrics["errors"] += 1
                self.audit.append("strategy_memory.error", action="retrieve", error=str(exc)[:200])
                briefs = []
            elapsed = time.perf_counter() - started
            self.memory_metrics["queries"] += 1
            self.memory_metrics["hits"] += len(briefs)
            self.memory_metrics["query_seconds"] += elapsed
            self._memory_cache[task.id] = (revision, briefs)
            self.event_bus.publish("memory.retrieved", task_id=task.id, count=len(briefs), duration=elapsed,
                                   source="commander", references=[item["source_task"] for item in briefs])
            return list(briefs)

    def _remember_strategy(self, task: TaskSpec, state: RunState) -> None:
        if self.strategy_memory is None:
            return
        try:
            key = self.strategy_memory.remember(task, state, workspace=self.config.workspace_dir / state.run_id)
            if key:
                self.memory_metrics["stored"] += 1
                self.event_bus.publish("memory.recorded", source="commander", task_id=task.id, memory_id=key)
        except Exception as exc:
            self.memory_metrics["errors"] += 1
            self.audit.append("strategy_memory.error", action="store", error=str(exc)[:200])

    def _execute_agent(self, worker: Worker, task: TaskSpec, run_id: str, hints: list[str], registry: ToolRegistry) -> RunState:
        # 各 Worker 的限额按并发数分配，避免并行后总请求速率扩大。
        worker_config = self.config.with_overrides(
            scope__max_qps=self.config.scope.max_qps / self.max_workers,
            scope__max_host_concurrency=max(1, self.config.scope.max_host_concurrency // self.max_workers),
        )
        worker_config.llm = copy.deepcopy(worker.llm or self.config.llm)
        agent = PentestAgent(
            worker_config,
            backend=worker.backend,
            registry=registry,
            stop_event=_TaskStop(self.stop_event, self._task_stops[task.id]),
            steer_queue=self._task_steers[task.id],
            pause_event=self.pause_event,
            shared_context={"coordination_runtime": self.coordination, "intent_id": task.id},
            event_bus=self.event_bus,
        )
        agent.shared_context["worker_id"] = worker.id
        usage_before = worker.backend.ledger.to_dict()
        state = agent.run(
            task.target,
            goal=task.goal,
            max_steps=task.max_steps,
            max_seconds=task.max_seconds,
            commander_briefs=self._briefs(task),
            commander_brief_provider=lambda: self._briefs(task),
            extra_rules=("本次只完成指定意图，旁支线索及时用 record_intelligence 登记交主控，不自行扩大任务。"
                         "发现时立即用 report_finding/record_fact 写回证据；收尾前独立复核成功结果。\n"
                         "Worker 收尾必须让结果可交接：返回真实 status、verified_facts、evidence_refs、failed_paths、"
                         "remaining_questions 和 artifacts；未完成、超时、取消或证据不足要保留对应状态。\n"
                         + (f"本任务交付与验收条件：{task.deliverable}\n" if task.deliverable else "")
                         + getattr(registry, "capability_guidance", "")),
            run_id=run_id,
        )
        if state.ledger:
            state.ledger = worker.backend.ledger.delta(usage_before).to_dict()
        from .report import build_report

        paths = build_report(state, worker_config, meta={"swarm": self.run_id, "task": task.id}).save()
        state.artifacts["report_paths"] = ", ".join(str(path) for path in paths)
        agent._save_state(state, Path(state.artifacts["workspace"]))
        return state

    def _execute_worker(self, worker: Worker, task: TaskSpec, run_id: str, hints: list[str], registry: ToolRegistry) -> RunState:
        """在执行线程记录实际起点，区分领取、检查点落盘和线程排队。"""
        with self.event_bus.keep_open():
            return self._execute_in_scope(worker, task, run_id, hints, registry)

    def _execute_in_scope(self, worker: Worker, task: TaskSpec, run_id: str, hints: list[str], registry: ToolRegistry) -> RunState:
        self.event_bus.publish("worker.executing", source="executor", run_id=run_id,
                               task_id=task.id, worker_id=worker.id,
                               queued_at=self.coordination.intents[task.id].get("queued_at"))
        try:
            return self.executor(worker, task, run_id, hints, registry)
        finally:
            # Future 变成 done 之前记录执行终点，避免 Commander 抢先提交结果造成时间线倒置。
            self.event_bus.publish("worker.finished", source="executor", run_id=run_id,
                                   task_id=task.id, worker_id=worker.id)

    @staticmethod
    def _result(state: RunState) -> dict[str, Any]:
        """生成 Commander 与后续 Worker 共用的紧凑结果契约。"""
        facts = {
            "hosts": [{"host": host, "ports": sorted(hs.ports), "technologies": hs.technologies[:8], "web_paths": hs.web_paths[:20]} for host, hs in state.hosts.items()],
            "findings": [{"title": f.title, "class": f.vuln_class, "target": f.url or f.target, "status": f.status} for f in state.findings],
            "flags": [flag.get("value", "") for flag in state.flags],
        }
        evidence_refs: list[str] = []
        for item in state.evidence:
            if not isinstance(item, dict):
                continue
            for key in ("id", "evidence_id", "evidence_ref", "traffic_id"):
                value = str(item.get(key) or "").strip()
                if value and value not in evidence_refs:
                    evidence_refs.append(value)
                    break
        verified_facts: list[dict[str, Any]] = []
        for host, host_state in sorted(state.hosts.items()):
            if host_state.alive is not None:
                verified_facts.append({"kind": "asset", "host": host, "alive": host_state.alive})
            for port, info in sorted(host_state.ports.items()):
                verified_facts.append({"kind": "port", "host": host, "port": port,
                                       "service": info.get("service", ""), "version": info.get("version", "")})
            for technology in host_state.technologies[:20]:
                verified_facts.append({"kind": "technology", "host": host, "value": technology})
            for path in host_state.web_paths[:50]:
                verified_facts.append({"kind": "path", "host": host, "value": path})
        for finding in state.findings:
            if finding.status not in {"verified", "exploited"} or not finding.evidence:
                continue
            finding_refs = []
            for item in finding.evidence:
                if isinstance(item, dict):
                    value = str(item.get("id") or item.get("evidence_id") or item.get("evidence_ref") or "").strip()
                    if value and value not in finding_refs:
                        finding_refs.append(value)
            verified_facts.append({"kind": "finding", "id": finding.id, "title": finding.title,
                                   "class": finding.vuln_class, "target": finding.url or finding.target,
                                   "status": finding.status, "evidence_refs": finding_refs})
            for value in finding_refs:
                if value not in evidence_refs:
                    evidence_refs.append(value)
        for credential in state.credentials:
            if credential.verified:
                verified_facts.append({"kind": "credential", "target": credential.target,
                                       "service": credential.service, "username": credential.username,
                                       "verified": True})
        for flag in state.flags:
            value = str(flag.get("value") or "").strip()
            if value:
                verified_facts.append({"kind": "flag", "value": value, "source": flag.get("source", "")})
        failed_paths: list[str] = []
        for step in state.steps:
            if not isinstance(step, dict) or step.get("ok", True):
                continue
            text = str(step.get("error") or step.get("summary") or step.get("action") or "未知失败").strip()
            action = str(step.get("action") or "tool")
            item = f"{action}: {text}"[:500]
            if item not in failed_paths:
                failed_paths.append(item)
        remaining_questions: list[dict[str, Any]] = []
        for item in state.hypotheses:
            if not isinstance(item, dict) or item.get("status") in {"confirmed", "refuted"}:
                continue
            remaining_questions.append({key: item.get(key, "") for key in ("id", "statement", "status", "next_step")})
        if not remaining_questions and state.status not in {"success", "completed"} and state.goal:
            remaining_questions.append({"id": "goal", "statement": state.goal[:500], "status": "unresolved", "next_step": ""})
        return {
            "contract_version": WORKER_RESULT_CONTRACT_VERSION,
            "run_id": state.run_id,
            "status": state.status,
            "verdict": state.verdict,
            "facts": facts,
            "verified_facts": verified_facts[:200],
            "evidence_refs": evidence_refs[:200],
            "failed_paths": failed_paths[:100],
            "remaining_questions": remaining_questions[:40],
            "counters": state.counters,
            "performance": state.performance,
            "ledger": state.ledger,
            "artifacts": state.artifacts,
            "duration": state.duration,
            "intelligence": {
                "assets": [
                    {
                        "host": host,
                        "alive": hs.alive,
                        "ports": {str(port): info for port, info in sorted(hs.ports.items())},
                        "technologies": list(hs.technologies),
                        "web_paths": list(hs.web_paths),
                    }
                    for host, hs in state.hosts.items()
                ],
                "verified_findings": [
                    finding.to_dict()
                    for finding in state.findings
                    if finding.status in ("verified", "exploited") and finding.evidence
                ],
                "verified_credentials": [
                    credential.to_dict() for credential in state.credentials if credential.verified
                ],
                "artifacts": dict(state.artifacts),
                "verdict": state.verdict,
            },
        }

    @staticmethod
    def _failed_result(error: str) -> dict[str, Any]:
        """即使 Worker 在返回 RunState 前异常，也保持结果契约完整。"""
        return {
            "contract_version": WORKER_RESULT_CONTRACT_VERSION,
            "run_id": "",
            "status": "failed",
            "verdict": "",
            "facts": {"hosts": [], "findings": [], "flags": []},
            "verified_facts": [],
            "evidence_refs": [],
            "failed_paths": [str(error)[:500]],
            "remaining_questions": [],
            "counters": {},
            "performance": {},
            "ledger": {},
            "artifacts": {},
            "duration": 0.0,
            "intelligence": {},
        }

    def _save(self) -> None:
        # 取消通知要等分支安全收尾；全局停止也必须保留已得到的分支结果。
        pending_groups = [task for task in self.tasks if task.aggregate
                          and self.records[task.id].status == "cancelled" and not self.records[task.id].result]
        while pending_groups:
            ready = [task for task in pending_groups if all(self.records[key].status in TERMINAL for key in task.depends_on)
                     and not any(child.id in task.depends_on for child in pending_groups)]
            if not ready:
                break
            for task in ready:
                self._finish_group(task)
                pending_groups.remove(task)
        self.workspace.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 4,
            "run_id": self.run_id,
            "max_workers": self.max_workers,
            "tasks": [{**asdict(task), "model": self._task_llms[task.id].model,
                       "reasoning_effort": self._task_llms[task.id].reasoning_effort,
                       "profile": self._task_llms[task.id].profile} for task in self.tasks],
            "events": self.events,
            "capability_memory": self.fabric.memory_snapshot(),
            "coordination": self.coordination.snapshot(),
            "commander_cursor": self._commander_events.cursor,
            "events_path": str(self.event_bus.path),
            "write_report": self.write_report,
            "auto_expand": self.auto_expand,
            "max_generated_tasks": self.max_generated_tasks,
        }
        event = self.event_bus.publish("runtime.checkpoint", source="commander", snapshot=data)
        data["checkpoint_seq"] = event.seq
        temporary = self.checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.checkpoint)
        if self.on_update is not None:
            try:
                self.on_update({"run_id": self.run_id, "checkpoint": str(self.checkpoint),
                                "tasks": {task.id: {"goal": task.goal, "target": task.target,
                                          **asdict(self.records[task.id])} for task in self.tasks}})
            except Exception as exc:
                self.audit.append("swarm.observer.error", error=str(exc)[:300])
        # 任务完成先提交结果与 UI 快照，再通知等待者，避免读到旧 running 快照。
        waiting_groups = {task.id for task in self.tasks if task.aggregate and not self.records[task.id].result}
        for task_id, record in self.records.items():
            if task_id in waiting_groups:
                continue
            version = (record.status, record.attempt, record.ended_at)
            if record.status in TERMINAL and self._published_results.get(task_id) != version:
                self.event_bus.publish("task.result_ready", source="commander", task_id=task_id,
                    worker_id=record.worker_id, status=record.status, attempt=record.attempt,
                    checkpoint=str(self.checkpoint))
                self._published_results[task_id] = version

    def _consume_events(self) -> None:
        """实时事实和工具终态在 Commander 线程消费，不由 Worker 修改任务记录。"""
        high_water = self.event_bus.last_seq
        while self._commander_events.cursor < high_water:
            batch = self._commander_events.poll()
            self._consume_batch(batch)
            if not batch:
                break

    def _consume_batch(self, batch: list[Any]) -> None:
        for event in batch:
            record = self.records.get(event.task_id)
            if record is None or record.status != "running" or record.worker_id != event.worker_id:
                continue
            expected_run = f"{self.run_id}-{event.task_id}-{record.attempt}"
            if event.run_id != expected_run:
                continue
            if event.kind == "worker.executing":
                row = self.coordination.intents[event.task_id]
                row["execution_started_at"] = event.time
                record.queue_seconds += max(0.0, event.time - record.started_at)
            elif event.kind == "worker.finished":
                self.coordination.intents[event.task_id]["execution_finished_at"] = event.time
            elif event.kind == "state.updated":
                raw = self.event_bus.runtime_payload(event)["state"]
                # 仅事实字段影响 Planner。时间、输出和步骤变化继续留在事件库，
                # 不重复重建相同事实；使用保真载荷，避免脱敏凭据被误判为相同。
                projection = {key: raw.get(key) for key in ("hosts", "findings", "credentials", "flags")}
                projection["goal_verifications"] = (raw.get("casebook") or {}).get("goal_verifications")
                current = (expected_run, projection)
                if self._state_projections.get(event.task_id) == current:
                    continue
                state = RunState.from_dict(raw)
                if state.run_id == expected_run:
                    added = self.coordination.publish_state(event.task_id, state, include_steps=False)
                    self._expand_from_facts(event.task_id, added)
                    self._state_projections[event.task_id] = current
            elif event.kind == "tool.finished":
                self.coordination.observe_tool(event)
            elif event.kind in {"model.started", "model.finished"}:
                self.coordination.observe_usage(event)
            elif event.kind == "direction.invalid":
                self.coordination.invalidate_direction(event.task_id, str(event.payload.get("reason") or ""),
                                                       list(event.payload.get("evidence") or []))
            if self.on_progress is not None and event.kind in {"tool.started", "tool.output", "tool.finished", "model.started", "model.text_delta", "model.thinking_delta", "model.finished", "model.error", "model.retry", "assistant.message", "react.action"}:
                try:
                    self.on_progress(event)
                except Exception as exc:
                    self.audit.append("swarm.observer.error", error=str(exc)[:300])

    def _assess_workers(self) -> None:
        """评估不占模型请求；只在明确失效且有替代任务时请求协作式收尾。"""
        now = time.monotonic()
        if now - self._last_assessment < 1.0 or self.pause_event.is_set():
            return
        self._last_assessment = now
        stalled = self.coordination.assess_progress()
        for task_id in stalled:
            self.coordination.emit("worker.assessed", intent_id=task_id,
                                   reason="保留事实与租约，等待有效进展或确认替代方向")
        alternatives = [item for item in self.tasks if not item.aggregate and self.records[item.id].status == "pending"
                        and all(self.records[dep].status == "completed" for dep in item.depends_on)]
        for key, row in self.coordination.intents.items():
            if (row["status"] != "running" or row.get("lifecycle") != "STALLED" or not row.get("direction_invalid")
                    or row.get("stop_loss_requested") or not alternatives):
                continue
            row["stop_loss_requested"] = True
            self._task_stops[key].set()
            self.coordination.emit("worker.stop_loss", intent_id=key,
                reason="持续停滞，执行条件已确认无效且存在就绪替代方向；等待 Agent 安全边界释放资源",
                alternatives=[item.id for item in alternatives], evidence=row.get("invalid_evidence", []))
        if stalled:
            self._save()

    def _finish_group(self, task: TaskSpec) -> None:
        """汇总真实分支结果，不创建汇总 Agent，也不重发完整历史。"""
        record = self.records[task.id]
        children = [self.records[key] for key in task.depends_on]
        successful = all(child.status == "completed" for child in children)
        cancelled = record.status == "cancelled"
        result = self._failed_result("")
        result.update(status="cancelled" if cancelled else "completed" if successful else "blocked",
                      verdict="各工作分支已收尾；详见分支证据与未解决问题", failed_paths=[])
        for name in ("verified_facts", "evidence_refs", "failed_paths", "remaining_questions"):
            merged = [value for child in children for value in child.result.get(name, [])]
            result[name] = list({json.dumps(value, sort_keys=True, ensure_ascii=False): value for value in merged}.values())
        result["facts"] = {name: [value for child in children for value in child.result.get("facts", {}).get(name, [])]
                           for name in ("hosts", "findings", "flags")}
        result["artifacts"] = {key: self.records[key].result.get("artifacts", {}) for key in task.depends_on}
        result["intelligence"] = {name: [value for child in children for value in child.result.get("intelligence", {}).get(name, [])]
                                  for name in ("assets", "verified_findings", "verified_credentials")}
        result["branches"] = list(task.depends_on)
        record.result, record.evidence_refs = result, list(result["evidence_refs"])
        record.ended_at = time.time()
        self.coordination.terminate_unclaimed(task.id, "completed" if successful else "blocked",
                                             "所有交付分支完成" if successful else "部分交付分支未完成，保留已有结果")
        if successful and not cancelled:
            record.error = ""
        self.coordination.emit("plan.completed", intent_id=task.id, reason="分支收尾，无需额外模型汇总", branches=list(task.depends_on))

    def run(self) -> dict[str, Any]:
        try:
            # 复用对话入口已有的 WAL 保持机制；不持有写事务或全局模型锁。
            with self.event_bus.keep_open():
                return self._run()
        finally:
            report = self.profiler.finish(self.profile_path)
            self.profile_subscription.close()
            self._profile_report = report
            with self._lifecycle_lock:
                self._accepting = False
            while not self._inbox.empty():
                _, result = self._inbox.get_nowait()
                if not result.done():
                    result.set_exception(RuntimeError("Subworker 已收尾，任务未被接受"))

    def _run(self) -> dict[str, Any]:
        """执行所有可运行任务；异常保留在任务记录，其他分支继续。"""
        self._save()
        futures: dict[Future[RunState], tuple[TaskSpec, Worker, str, str]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                self._drain_submissions()
                self._consume_events()
                self._assess_workers()
                if self.stop_event.is_set() and not futures:
                    for task_id, record in self.records.items():
                        if record.status == "pending":
                            self.coordination.terminate_unclaimed(task_id, "cancelled", "用户停止，尚未执行")
                    self._save()
                    break
                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    # 先传播失败依赖，再让 Planner 对同一批状态变化只重算一次。
                    for task in list(self.tasks):
                        record = self.records[task.id]
                        if record.status != "pending":
                            if (task.aggregate and record.status == "cancelled" and not record.result
                                    and all(self.records[dep].status in TERMINAL for dep in task.depends_on)):
                                self._finish_group(task)
                                self._save()
                            continue
                        deps = [self.records[dep].status for dep in task.depends_on]
                        if task.aggregate:
                            if not all(status in TERMINAL for status in deps):
                                continue
                            self._finish_group(task)
                            self._save()
                            continue
                        if any(status in TERMINAL - {"completed"} for status in deps):
                            self.coordination.terminate_unclaimed(task.id, "skipped", "前序任务未完成")
                            self.audit.append("swarm.task.skip", task=task.id, reason=record.error)
                            self._save()
                    previous_rounds = self.coordination.planner_rounds
                    ready_intents = self.coordination.plan() if self.pool.available() else []
                    if self.coordination.planner_rounds != previous_rounds:
                        self.audit.append(
                            "planner.round", round=self.coordination.planner_rounds,
                            revision=self.coordination.revision, ready=sorted(ready_intents),
                        )
                    launches: list[tuple[TaskSpec, Worker, str, ToolRegistry, str]] = []
                    by_id = {task.id: task for task in self.tasks}
                    active = [item[0] for item in futures.values()]
                    for task_id in ready_intents:
                        task = by_id[task_id]
                        record = self.records[task.id]
                        if record.status != "pending":
                            continue
                        if task.aggregate:
                            self._finish_group(task)
                            self._save()
                            continue
                        if any(_dispatch_similar(task, other) for other in [*active, *(item[0] for item in launches)]):
                            continue
                        try:
                            worker = self.pool.acquire(self._task_llms[task.id])
                        except Exception as exc:
                            self.coordination.terminate_unclaimed(task.id, "failed", f"BackendError: {exc}")
                            self._save()
                            continue
                        if worker is None:
                            break
                        try:
                            task_registry = self._task_registry(task, worker)
                        except Exception as exc:  # noqa: BLE001 - 单任务能力解析失败不拖垮其余任务
                            self.coordination.terminate_unclaimed(task.id, "failed", f"CapabilityError: {exc}")
                            self.audit.append("capability.lease.error", task=task.id, worker=worker.id, error=record.error)
                            worker.capability_lease_id = ""
                            worker.capabilities = ()
                            self.pool.release(worker, worker.lease_id)
                            self._save()
                            continue
                        self.coordination.claim(
                            task.id, worker.id, lease_id=worker.lease_id,
                            ttl=task.max_seconds + 30 if task.max_seconds else 0,
                        )
                        record.started_at = time.time()
                        row = self.coordination.intents[task.id]
                        row.pop("execution_started_at", None)
                        row.pop("execution_finished_at", None)
                        task_run_id = f"{self.run_id}-{task.id}-{record.attempt}"
                        self.events.append({"event": "task.start", "task": task.id, "worker": worker.id, "lease": worker.lease_id, "time": record.started_at})
                        self.event_bus.publish("task.started", source="commander", task_id=task.id,
                                               worker_id=worker.id, attempt=record.attempt, lease_id=worker.lease_id,
                                               queued_at=row.get("queued_at"))
                        self.audit.append("swarm.task.start", task=task.id, worker=worker.id, lease=worker.lease_id, attempt=record.attempt)
                        # 同一调度批次先领取所有就绪任务，再一次性提交检查点，
                        # 避免每个 Worker 启动前都被磁盘 I/O 阻塞。
                        launches.append((task, worker, task_run_id, task_registry, record.capability_lease_id))
                    if launches:
                        self._save()
                        for task, worker, task_run_id, task_registry, capability_lease in launches:
                            future = executor.submit(self._execute_worker, worker, task, task_run_id, self._hints(task), task_registry)
                            futures[future] = (task, worker, worker.lease_id, capability_lease)
                            future.add_done_callback(lambda _future, task_id=task.id, worker_id=worker.id, run_id=task_run_id:
                                                     self.event_bus.publish("worker.available", source="executor",
                                                         run_id=run_id, task_id=task_id, worker_id=worker_id))
                if not futures:
                    if all(record.status in TERMINAL for record in self.records.values()):
                        with self._lifecycle_lock:
                            if self._inbox.empty():
                                self._accepting = False
                                break
                        continue
                    if self.pause_event.is_set():
                        time.sleep(0.1)
                        continue
                    if self.stop_event.is_set():
                        continue
                    if self.coordination.schedule_revision != self.coordination.planned_schedule_revision:
                        # 确定性汇总也能解锁后继；先消费新状态，再判断任务图死锁。
                        continue
                    raise RuntimeError("任务图无法继续调度")
                # Future、Intent 和事实共用同一唤醒条件；不再只等待任务终态轮询。
                cursor = self._commander_events.cursor
                done = [future for future in futures if future.done()]
                if not done:
                    self.event_bus.wait(cursor, timeout=.2)
                    done = [future for future in futures if future.done()]
                # Future 完成前已提交的尾部必须在任务变成终态前全部投影。
                self._consume_events()
                for future in done:
                    task, worker, lease, capability_lease = futures.pop(future)
                    record = self.records[task.id]
                    if record.lease_id != lease:
                        raise RuntimeError("任务租约不匹配")
                    state: RunState | None = None
                    retry_reason = ""
                    retry_requested = False
                    try:
                        state = future.result()
                        self._remember_strategy(task, state)
                        record.result = self._result(state)
                        final_status = "cancelled" if state.status == "stopped" else (
                            state.status if state.status in TERMINAL else "completed")
                        if state.status == "stopped" and self.coordination.intents[task.id].get("stop_loss_requested"):
                            final_status = "blocked"
                            record.error = "方向已确认失效，保留已有结果并释放资源"
                        try:
                            self.fabric.publish_task(self.fabric.get_lease(capability_lease), record.result)
                        except Exception as exc:  # noqa: BLE001 - 索引故障不能改写任务真实状态
                            self.audit.append(
                                "capability.publish.error",
                                task=task.id,
                                worker=worker.id,
                                error=f"{type(exc).__name__}: {exc}"[:300],
                            )
                        record.evidence_refs = self.fabric.refs_for_task(task.id)
                        record.result["evidence_refs"] = list(record.evidence_refs)
                        record.result["capabilities"] = list(record.capability_snapshot)
                        added = self.coordination.publish_state(task.id, state, evidence_refs=record.evidence_refs)
                        self._expand_from_facts(task.id, added)
                    except Exception as exc:  # noqa: BLE001 - 单任务失败不影响其余分支
                        record.error = f"{type(exc).__name__}: {exc}"
                        record.result = self._failed_result(record.error)
                        retry_requested = (record.attempt < task.max_attempts and not self.stop_event.is_set()
                                           and not self._task_stops[task.id].is_set())
                        retry_reason = record.error
                        if self.coordination.intents[task.id].get("stop_loss_requested"):
                            final_status = "blocked"
                        elif self.stop_event.is_set() or self._task_stops[task.id].is_set():
                            final_status = "cancelled"
                        else:
                            final_status = "retry" if retry_requested else "failed"
                    finally:
                        record.ended_at = time.time()
                        row = self.coordination.intents[task.id]
                        record.execution_seconds += max(0.0, row.get("execution_finished_at", record.ended_at)
                                                        - row.get("execution_started_at", record.started_at))
                        ledger = record.result.get("ledger") or {}
                        # 终态账本补齐未发事件的执行器；与实时事件取较大值，避免双计费。
                        attempt_requests = max(0, record.model_requests - row.get("previous_requests", 0))
                        attempt_tokens = max(0, record.tokens - row.get("previous_tokens", 0))
                        requests = max(attempt_requests, int(ledger.get("calls", 0)))
                        tokens = max(attempt_tokens, int(ledger.get("total_tokens", 0)))
                        record.model_requests += requests - attempt_requests
                        record.tokens += tokens - attempt_tokens
                        row["previous_requests"], row["previous_tokens"] = record.model_requests, record.tokens
                        worker.model_requests += requests
                        worker.tokens += tokens
                        if retry_requested:
                            self.coordination.retry(task.id, lease, retry_reason)
                            self.events.append({"event": "task.retry", "task": task.id, "worker": worker.id,
                                                "attempt": record.attempt, "time": record.ended_at})
                            self.event_bus.publish("task.retry", source="commander", task_id=task.id,
                                                   worker_id=worker.id, attempt=record.attempt, error=retry_reason)
                            self.audit.append("swarm.task.retry", task=task.id, worker=worker.id,
                                              attempt=record.attempt, error=retry_reason)
                        else:
                            self.coordination.settle(task.id, lease, final_status, error=record.error)
                            self.events.append({"event": "task.end", "task": task.id, "worker": worker.id,
                                                "status": record.status, "time": record.ended_at})
                            self.event_bus.publish("task.finished", source="commander", task_id=task.id,
                                                   worker_id=worker.id, status=record.status, error=record.error)
                            self.audit.append("swarm.task.end", task=task.id, worker=worker.id,
                                              status=record.status, error=record.error)
                        self.fabric.release(capability_lease)
                        self.audit.append("capability.release", task=task.id, worker=worker.id, lease=capability_lease)
                        worker.capability_lease_id = ""
                        worker.capabilities = ()
                        record.capability_lease_id = ""
                        self.pool.release(worker, lease)
                        self.coordination.emit("worker.idle", intent_id=task.id, worker_id=worker.id)
                        record.lease_id = ""
                        if retry_requested:
                            record.ended_at = 0.0
                if done:
                    # 同时完成的任务共用一个检查点，租约和结果仍在同一 Commander 线程提交。
                    self._save()
                    self._commander_events.ack()
        self.audit.append("swarm.run.end", statuses={key: value.status for key, value in self.records.items()})
        self.event_bus.publish("runtime.finished", source="commander",
                               statuses={key: value.status for key, value in self.records.items()},
                               reason="用户停止" if self.stop_event.is_set() else "所有可执行方向均已完成或明确收尾")
        self._run_finished_at = time.monotonic()
        if self.write_report:
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
            "| 任务 | 状态 | Worker | 能力 | 尝试 | 发现 | Flag |",
            "|---|---|---|---|---:|---:|---:|",
        ]
        for task in self.tasks:
            record = self.records[task.id]
            facts = record.result.get("facts", {})
            lines.append(
                f"| {task.id} | {record.status} | {record.worker_id or '-'} | {', '.join(record.capability_ids) or '-'} | {record.attempt} "
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
            "events_path": str(self.event_bus.path),
            "audit_path": str(self.audit.path),
            "performance_path": str(getattr(self, "profile_path", "")),
            "performance": getattr(self, "_profile_report", self.profiler.snapshot()),
            "report_paths": [str(self.config.report_dir / f"{self.run_id}_report.md"), str(self.config.report_dir / f"{self.run_id}_report.html")],
            "tasks": {
                task.id: {
                    "status": self.records[task.id].status,
                    "worker": self.records[task.id].worker_id,
                    "attempt": self.records[task.id].attempt,
                    "capabilities": list(self.records[task.id].capability_snapshot),
                    "evidence_refs": list(self.records[task.id].evidence_refs),
                    "result": self.records[task.id].result,
                    "error": self.records[task.id].error,
                    "lifecycle": self.records[task.id].lifecycle,
                }
                for task in self.tasks
            },
            "workers_created": len(self.pool.workers),
            "worker_reuses": sum(max(0, worker.completed - 1) for worker in self.pool.workers),
            "strategy_memory": dict(self.memory_metrics),
            "generated_tasks": sum(task.id.startswith("auto-") for task in self.tasks),
            "capability_memory": self.fabric.memory_snapshot(),
            "coordination": self.coordination.overview(),
            "resources": {**self.coordination.resource_stats(), "workers": {
                worker.id: {"busy": worker.busy, "completed": worker.completed,
                            "model_requests": worker.model_requests, "tokens": worker.tokens,
                            "idle_seconds": worker.idle_seconds + (max(0.0, (self._run_finished_at or time.monotonic()) - worker.idle_since) if not worker.busy else 0.0)}
                for worker in self.pool.workers}},
            "graphs": {
                "facts": list(self.coordination.facts.values()),
                "exploration": self.coordination.exploration,
            },
        }
