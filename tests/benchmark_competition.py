"""A/B/C/D 工程对照：真实 Agent/工具/独立验收，固定 Replay 决策，不代表竞赛成绩。

python tests/benchmark_competition.py --baseline-dir <会话前调度源码> --output-dir <全新目录> --repeats 3
保留全部任务工件、事件库与独立 checker；经验库在测试前冻结，测试结果不回流训练库。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_commander import load_baseline
from trina.brain import PentestAgent
from trina.config import TrinaConfig
from trina.event_latency import summarize_latency
from trina.events import EventBus
from trina.llm.replay_backend import ReplayBackend
from trina.strategy_memory import StrategyMemory
from trina.tools import build_registry
import trina.swarm as current


SOLUTIONS = {
    "prefix": """def solve(values, target):
    seen, total, answer = {0: 1}, 0, 0
    for value in values:
        total += value
        answer += seen.get(total - target, 0)
        seen[total] = seen.get(total, 0) + 1
    return answer
""",
    "topology": """def solve(n, edges):
    import heapq
    graph, indegree = [[] for _ in range(n)], [0] * n
    for a, b in edges:
        graph[a].append(b)
        indegree[b] += 1
    ready, result = [i for i in range(n) if not indegree[i]], []
    heapq.heapify(ready)
    while ready:
        node = heapq.heappop(ready)
        result.append(node)
        for other in graph[node]:
            indegree[other] -= 1
            if not indegree[other]: heapq.heappush(ready, other)
    return result if len(result) == n else None
""",
    "interval": """def solve(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]: result[-1][1] = max(result[-1][1], end)
        else: result.append([start, end])
    return result
""",
    "encoding": """def solve(text):
    result = []
    for char in text:
        if result and result[-1][0] == char: result[-1][1] += 1
        else: result.append([char, 1])
    return result
""",
    "practice": """def solve(value):
    return isinstance(value, list) and all(type(item) is int for item in value)
""",
}
CHECKS = {
    "prefix": """import itertools
for length in range(6):
    for values in itertools.product((-1, 0, 2), repeat=length):
        for target in (-2, 0, 2):
            expected = sum(sum(values[a:b]) == target for a in range(length) for b in range(a+1, length+1))
            assert module.solve(list(values), target) == expected, (values, target)
""",
    "topology": """for n, edges in [(0, []), (4, [(0, 2), (1, 2), (2, 3)]), (3, []), (2, [(0, 1), (1, 0)])]:
    result = module.solve(n, edges)
    if edges == [(0, 1), (1, 0)]: assert result is None
    else:
        assert sorted(result) == list(range(n))
        assert all(result.index(a) < result.index(b) for a, b in edges)
""",
    "interval": """for source, expected in [([], []), ([[1,3],[2,4],[9,10]], [[1,4],[9,10]]),
                         ([[-4,-1],[-1,3],[0,0]], [[-4,3]]), ([[2,2],[2,2]], [[2,2]])]:
    assert module.solve(source) == expected
""",
    "encoding": """import itertools
for length in range(7):
    for chars in itertools.product(('a', '中'), repeat=length):
        source = ''.join(chars)
        result = module.solve(source)
        assert ''.join(char * count for char, count in result) == source
        assert all(count > 0 for char, count in result)
        assert all(a[0] != b[0] for a, b in zip(result, result[1:]))
""",
    "practice": """for value, expected in [([], True), ([1,-2], True), ([True], False), ([1.5], False), ('text', False)]:
    assert module.solve(value) == expected
""",
}
GOALS = {
    "prefix": "实现 Python prefix sum 连续子数组统计代码",
    "topology": "实现 Python stable topological order 图排序代码",
    "interval": "实现 Python interval merge 区间合并代码",
    "encoding": "实现 Python run length 编码代码",
    "practice": "实现 Python JSON 输入类型检查代码",
}
ARMS = ("A_single", "B_snapshot", "C_upgraded", "D_memory",
        "D_no_priority", "D_fixed_workers", "D_poll_replanning", "D_no_memory")
TIME_LIMIT = 30
STEP_LIMIT = 6


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def working_directory(path: Path):
    """每组使用同一干净项目根；所有执行线程结束后再恢复 cwd。"""
    previous = Path.cwd()
    path.mkdir(parents=True, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def fixture(directory: Path, kind: str) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    solution, verifier, receipt = directory / "solution.py", directory / "verifier.py", directory / "validation.json"
    verifier.write_text("import importlib.util,json,pathlib,time,sys\n"
        "spec=importlib.util.spec_from_file_location('submission',sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n" + CHECKS[kind] +
        "pathlib.Path(__file__).with_name('validation.json').write_text(json.dumps({'passed':True,'verified_at':time.time()}))\n"
        "print('OK')\n", encoding="utf-8")
    return {"kind": kind, "solution": solution, "verifier": verifier, "receipt": receipt,
            "verifier_sha256": digest(verifier)}


def verified_receipt(item: dict, begin: float, finished: float) -> dict:
    """只接受本次执行窗口生成且独立检查器未变更的回执。"""
    if not item["receipt"].is_file() or digest(item["verifier"]) != item["verifier_sha256"]:
        return {}
    receipt = json.loads(item["receipt"].read_text())
    if receipt.get("passed") is not True or not begin <= receipt.get("verified_at", 0) <= finished:
        return {}
    return receipt


def script(item: dict) -> list[dict]:
    """固定响应只控制操作顺序，实际工具执行与验收失败仍如实计入结果。"""
    actions = [
        ("workspace_read", {"path": str(item["verifier"])}),
        ("workspace_write", {"path": str(item["solution"]), "content": SOLUTIONS[item["kind"]]}),
        ("terminal_exec", {"argv": [sys.executable, str(item["verifier"]), str(item["solution"])],
                           "cwd": "run", "timeout": TIME_LIMIT}),
    ]
    return [{"response": json.dumps({"action": name, "params": params}, ensure_ascii=False)} for name, params in actions] + [
        {"response": json.dumps({"final": {"summary": "独立验收已执行，实际结果见证据"}}, ensure_ascii=False)}]


def config_for(directory: Path, *, memory: bool) -> TrinaConfig:
    config = TrinaConfig().with_overrides(run__workspace=str(directory / "runs"), audit__dir=str(directory / "logs"),
        report__output_dir=str(directory / "reports"), llm__backend="replay", llm__model="fixed-replay-v1",
        llm__reasoning_effort="default", llm__tool_protocol="json", llm__max_steps=STEP_LIMIT,
        llm__max_seconds=TIME_LIMIT, llm__context_window=65536, llm__max_tokens=1024,
        scope__max_host_concurrency=4, run__strategy_memory=memory)
    config.path = directory / "configs" / "trina.yaml"
    return config


def prepare_memory(root: Path) -> Path:
    """练习题只验证输入类型，与测试侧四类算法不共享解答或数据。"""
    config = config_for(root, memory=True)
    item = fixture(root / "practice", "practice")
    task = current.TaskSpec("practice", str(item["solution"]), GOALS["practice"], context_key="python-stdlib-v1")
    backend = ReplayBackend(config.llm)
    backend.script = script(item)
    with working_directory(root):
        state = PentestAgent(config, backend=backend).run(task.target, goal=task.goal, run_id="practice")
    memory = StrategyMemory(root / "frozen-memory.sqlite3")
    key = memory.remember(task, state, workspace=config.workspace_dir / state.run_id)
    if not key or not item["receipt"].exists():
        raise RuntimeError("训练夹具未通过真实验收，不能伪造经验库")
    return memory.path


def ablate(runtime, arm: str) -> None:
    """消融仅作用于本次基准实例，保留原始任务、执行器、预算和并发上限。"""
    if arm == "D_no_priority":
        original = runtime.coordination._dynamic_priority
        def static_score(intent, *args):
            row = original(intent, *args)
            return {**row, "score": int(intent.get("priority", 0)) * 10000}
        runtime.coordination._dynamic_priority = static_score
    elif arm == "D_fixed_workers":
        original = runtime.pool.available
        runtime.pool.available = lambda: 0 if any(worker.busy for worker in runtime.pool.workers) else original()
        runtime._assess_workers = lambda: None
    elif arm == "D_poll_replanning":
        original = runtime.coordination.plan
        def eager_plan():
            runtime.coordination._planned_state_signature = None
            runtime.coordination._dirty_tasks.update(runtime.coordination.intents)
            runtime.coordination.schedule_revision += 1
            return original()
        runtime.coordination.plan = eager_plan
        # 固定 100 ms 检查周期模拟无事件唤醒的控制器，避免自发规划事件形成忙循环。
        runtime.event_bus.wait = lambda after, timeout=None: threading.Event().wait(.1)


def measure(root: Path, arm: str, baseline, memory_path: Path) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    with working_directory(root):
        return _measure(root, arm, baseline, memory_path)


def _measure(root: Path, arm: str, baseline, memory_path: Path) -> dict:
    enabled = arm.startswith("D_") and arm != "D_no_memory"
    config = config_for(root, memory=enabled)
    kinds = ("prefix", "topology", "interval", "encoding", "prefix", "encoding")
    fixtures = {f"t{index}": fixture(root / f"t{index}", kind) for index, kind in enumerate(kinds)}
    module = baseline if arm == "B_snapshot" else current
    tasks = [module.TaskSpec(key, str(item["solution"]), GOALS[item["kind"]],
                depends_on=("t0",) if key == "t4" else (), priority=1 if key == "t0" else 0,
                max_steps=STEP_LIMIT, max_seconds=TIME_LIMIT, context_key="python-stdlib-v1") for key, item in fixtures.items()]
    starts, ends, states, workers, launches = {}, {}, {}, {}, []
    lock = threading.Lock()
    active = peak = 0
    runtime = None
    bus = EventBus(root / "events.sqlite3", "single") if arm == "A_single" else None
    def execute(worker, task, run_id, hints, registry):
        nonlocal active, peak
        with lock:
            starts[task.id], workers[task.id] = time.time(), worker.id
            launches.append(task.id)
            active += 1
            peak = max(peak, active)
        worker.backend.script = script(fixtures[task.id])
        try:
            if runtime is not None:
                state = runtime._execute_agent(worker, task, run_id, hints, registry)
            else:
                agent = PentestAgent(config, backend=worker.backend, registry=registry, event_bus=bus,
                    shared_context={"intent_id": task.id, "worker_id": worker.id})
                state = agent.run(
                        task.target, goal=task.goal, max_steps=STEP_LIMIT, max_seconds=TIME_LIMIT, run_id=run_id)
                from trina.report import build_report
                paths = build_report(state, config, meta={"benchmark": arm, "task": task.id}).save()
                state.artifacts["report_paths"] = ", ".join(str(path) for path in paths)
                agent._save_state(state, Path(state.artifacts["workspace"]))
            states[task.id] = state
            return state
        finally:
            with lock:
                ends[task.id] = time.time()
                active -= 1
    if arm != "A_single":
        extra = {"strategy_memory": StrategyMemory(memory_path, readonly=True)} if enabled else {}
        runtime = module.SwarmRuntime(config, tasks, max_workers=4, run_id=arm.lower(), executor=execute,
            backend_factory=ReplayBackend, write_report=False, **extra)
        ablate(runtime, arm)
        bus = runtime.event_bus
    begin = time.time()
    if runtime is None:
        worker = current.Worker("single", ReplayBackend(config.llm), llm=copy.deepcopy(config.llm))
        registry = build_registry()
        with bus.keep_open():
            for task in tasks:
                execute(worker, task, "single-" + task.id, [], registry)
        summary = {}
    else:
        summary = runtime.run()
    finished = time.time()
    passed, first_success, per_task = [], [], {}
    for key, item in fixtures.items():
        # 验收脚本保持原样且真实产物存在；Agent 的 success 字段不能替代此判断。
        receipt = verified_receipt(item, begin, finished)
        valid = bool(receipt)
        duration = receipt["verified_at"] - begin if valid else None
        if valid:
            passed.append(key)
            first_success.append(duration)
        per_task[key] = {"passed": valid, "first_success_seconds": duration, "status": states[key].status,
                         "queue_seconds": starts[key] - max([begin] + [ends[dep] for dep in next(t for t in tasks if t.id == key).depends_on])}
    events = bus.replay(kinds=("model.started", "tool.finished"), limit=10000)
    requests = sum(event.kind == "model.started" for event in events)
    tool_events = [event for event in events if event.kind == "tool.finished"]
    failures = sum(event.payload.get("ok") is False for event in tool_events)
    idle = sum(finished - min(starts[key] for key in starts if workers[key] == worker_id)
               - sum(ends[key] - starts[key] for key in starts if workers[key] == worker_id) for worker_id in set(workers.values()))
    return {"arm": arm, "total_seconds": finished - begin, "verified_fixture_rate": len(passed) / len(tasks),
            "competition_success_rate": None, "first_verified_seconds": min(first_success) if first_success else None,
            "model_requests": requests, "provider_requests": 0, "total_tokens": None, "token_source": "replay_unreported",
            "tool_calls": len(tool_events), "invalid_tool_ratio": failures / max(1, len(tool_events)),
            "worker_active_seconds": sum(ends[key] - starts[key] for key in starts), "worker_idle_seconds": max(0, idle),
            "mean_ready_queue_seconds": statistics.mean(item["queue_seconds"] for item in per_task.values()),
            "peak_concurrency": peak, "duplicate_executions": len(launches) - len(set(launches)),
            "context_budget_errors": sum(state.counters.get("context_budget_errors", 0) for state in states.values()),
            "planner_rounds": runtime.coordination.planner_rounds if runtime else 0,
            "memory": summary.get("strategy_memory", {}), "latency": summarize_latency(bus.path),
            "tasks": per_task, "events_path": str(bus.path),
            "artifact_dir": str(root), "fixture_sha256": {key: row["verifier_sha256"] for key, row in fixtures.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    args = parser.parse_args()
    if args.repeats < 1 or args.output_dir.exists():
        parser.error("repeats 必须为正数，output-dir 必须为新目录以保留历史结果")
    root = args.output_dir.resolve()
    root.mkdir(parents=True)
    baseline = load_baseline(args.baseline_dir)
    memory = prepare_memory(root / "training")
    memory_digest = digest(memory)
    report = {"method": "本地 Replay 功能/调度对照；实际 AgentRuntime、注册工具及独立验收，不代表模型解题能力",
        "comparison_scope": "A 单 Agent 顺序执行；B 会话前调度模块；C 当前调度；D 加只读经验；所有组共用当前 AgentRuntime/适配层",
        "fixed": {"backend": "replay", "model": "fixed-replay-v1", "reasoning": "default", "workers_limit": 4,
                  "seconds_per_task": TIME_LIMIT, "steps_per_task": STEP_LIMIT, "provider_token_budget": 0,
                  "context_window": 65536, "max_output_tokens_per_request": 1024,
                  "python": sys.version, "platform": platform.platform(), "memory_sha256": memory_digest,
                  "fixture_version": hashlib.sha256(json.dumps(CHECKS, sort_keys=True).encode()).hexdigest()},
        "limitations": ["固定响应不评估规划决策质量，真实供应商/竞赛对照待验证", "Replay 不报告 Token，不能把未知值当真实零 Token",
                        "Worker 有效时间这里只记录执行区间，无法据此推断解题贡献", "无效工具比例只包括实际失败工具，未推测无收益动作"],
        "ablations": {"D_no_priority": "只保留静态 priority，关闭动态评分", "D_fixed_workers": "整批 Worker 完成后再补位，关闭停滞调整",
                      "D_poll_replanning": "每 100 ms 唤醒并重算，关闭事件增量/唤醒", "D_no_memory": "关闭经验检索和写入"},
        "source_sha256": {label: {name: digest(directory / name) for name in ("swarm.py", "coordination.py", "intent_planner.py")}
                          for label, directory in (("baseline", args.baseline_dir), ("current", Path(current.__file__).parent))},
        "common_source_sha256": {name: digest(Path(current.__file__).parent / name) for name in (
            "events.py", "strategy_memory.py", "completion_inbox.py", "brain/agent.py", "brain/runtime.py",
            "tools/registry.py", "tools/workspace.py", "llm/replay_backend.py")},
        "samples": {arm: [] for arm in args.arms}}
    for repeat in range(args.repeats):
        for arm in (args.arms if repeat % 2 == 0 else list(reversed(args.arms))):
            sample = measure(root / f"{repeat}-{arm}", arm, baseline, memory)
            report["samples"][arm].append(sample)
            if digest(memory) != memory_digest:
                raise RuntimeError("冻结经验库被修改，停止不公平对照")
            (root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(arm, round(sample["total_seconds"], 3), sample["verified_fixture_rate"], sample["model_requests"], flush=True)
    print(str(root / "results.json"), flush=True)


if __name__ == "__main__":
    main()
