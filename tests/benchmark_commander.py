"""本地调度对照：真实 Swarm/EventBus/检查点，受控 Worker；不代表解题速度。

python tests/benchmark_commander.py --baseline-dir <修改前源码副本目录> --output <结果.json>
基线目录必须包含 coordination.py、intent_planner.py 和 swarm.py。
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trina.config import TrinaConfig
from trina.state import RunState
import trina.swarm as current


def load_baseline(directory: Path):
    """仅替换基线的两个调度模块引用；其余依赖与升级版完全相同。"""
    for short in ("coordination", "intent_planner"):
        name = "trina._baseline_" + short
        spec = importlib.util.spec_from_file_location(name, directory / (short + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module = types.ModuleType("trina._baseline_swarm")
    module.__package__ = "trina"
    module.__file__ = str(directory / "swarm.py")
    sys.modules[module.__name__] = module
    source = Path(module.__file__).read_text(encoding="utf-8")
    source = source.replace("from .coordination import", "from ._baseline_coordination import")
    source = source.replace("from .intent_planner import", "from ._baseline_intent_planner import")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


SCENARIOS = {
    "single": (1, 1), "two_independent": (2, 2), "four_independent": (4, 4),
    "dependencies": (6, 3), "continuous": (8, 2), "long_and_short": (7, 2),
    "simultaneous_completion": (4, 4), "event_burst": (2, 2),
}


def measure(module, scenario: str):
    count, workers = SCENARIOS[scenario]
    with tempfile.TemporaryDirectory(prefix="trina-scheduler-benchmark-") as temporary:
        root = Path(temporary)
        config = TrinaConfig.load().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"),
            report__output_dir=str(root / "reports"), scope__max_host_concurrency=4)
        tasks = []
        for index in range(count):
            deps = (f"t{index - 2}",) if scenario == "dependencies" and index >= 2 else ()
            tasks.append(module.TaskSpec(f"t{index}", f"host{index}.test", f"branch {index}", depends_on=deps))
        starts, ends, worker_ids, calls = {}, {}, {}, []
        active = peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(workers) if scenario == "simultaneous_completion" else None
        runtime = None

        def execute(worker, task, run_id, hints, registry):
            nonlocal active, peak
            with lock:
                starts[task.id] = time.perf_counter()
                worker_ids[task.id] = worker.id
                calls.append(task.id)
                active += 1
                peak = max(active, peak)
            state = RunState(run_id, task.target, task.goal, status="success")
            if scenario == "event_burst":
                state.host(task.target).ports[443] = {"service": "https"}
                for _ in range(48):
                    runtime.event_bus.publish("state.updated", run_id=run_id, source="worker", task_id=task.id,
                                              worker_id=worker.id, state=state.to_dict())
            if barrier:
                barrier.wait(timeout=10)
            delay = .04
            if scenario == "long_and_short":
                delay = .16 if task.id == "t0" else .02
            time.sleep(delay)
            state.ended_at = time.time()
            with lock:
                ends[task.id] = time.perf_counter()
                active -= 1
            return state

        runtime = module.SwarmRuntime(config, tasks, max_workers=workers, executor=execute,
                                      backend_factory=lambda config: object(), write_report=False)
        # 仅测 Commander 主线程的这些实际工作段；不含 EventBus.wait 和 Worker 休眠。
        timing = {"depth": 0, "seconds": 0.0, "plan_seconds": 0.0, "checkpoints": 0}

        def instrument(owner, name):
            original = getattr(owner, name)

            def call(*args, **kwargs):
                outer = timing["depth"] == 0
                timing["depth"] += 1
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - start
                    timing["depth"] -= 1
                    if outer:
                        timing["seconds"] += elapsed
                    if name == "plan":
                        timing["plan_seconds"] += elapsed
                    if name == "_save":
                        timing["checkpoints"] += 1
            setattr(owner, name, call)

        for name in ("_save", "_consume_events", "_drain_submissions", "_task_registry"):
            instrument(runtime, name)
        instrument(runtime.coordination, "plan")
        if hasattr(runtime, "_assess_workers"):
            instrument(runtime, "_assess_workers")
        begin = time.perf_counter()
        summary = runtime.run()
        finish = time.perf_counter()
        elapsed = finish - begin
        queue = [starts[task.id] - max([begin] + [ends[dep] for dep in task.depends_on]) for task in tasks]
        idle = 0.0
        for worker in set(worker_ids.values()):
            ids = [key for key, value in worker_ids.items() if value == worker]
            idle += finish - min(starts[key] for key in ids) - sum(ends[key] - starts[key] for key in ids)
        assert all(item["status"] == "completed" for item in summary["tasks"].values())
        return {"total_ms": elapsed * 1000, "mean_ready_queue_ms": statistics.mean(queue) * 1000,
                "worker_idle_ms": idle * 1000, "peak_concurrency": peak,
                "duplicate_executions": sum(value - 1 for value in Counter(calls).values()),
                "model_requests": len(runtime.event_bus.replay(kinds=("model.started",))),
                "tokens": sum(item["result"].get("ledger", {}).get("total_tokens", 0) for item in summary["tasks"].values()),
                "scheduler_observed_ms": timing["seconds"] * 1000, "priority_ms": timing["plan_seconds"] * 1000,
                "planner_rounds": runtime.coordination.planner_rounds, "checkpoints": timing["checkpoints"]}


def incremental(module):
    """许多无关任务下，新事实处理的实际 CPU/评分调用次数对照。"""
    tasks = [module.TaskSpec(f"t{i}", f"h{i}.test", f"direction{i}") for i in range(400)]
    coordination = module.CoordinationRuntime(tasks)
    coordination.plan()
    coordination.claim("t0", "worker")
    calls = 0
    original = coordination._dynamic_priority

    def score(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    coordination._dynamic_priority = score
    start = time.perf_counter()
    for index in range(1, 41):
        state = RunState("run", "h0.test")
        state.host(f"h{index}.test").ports[80] = {"service": "http"}
        coordination.publish_state("t0", state)
        coordination.plan()
    return {"elapsed_ms": (time.perf_counter() - start) * 1000, "priority_evaluations": calls,
            "new_facts": 40, "tasks": 400}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须为正数")
    baseline = load_baseline(args.baseline_dir)
    versions = {"before": baseline, "after": current}
    report = {"method": "真实 Swarm/EventBus/SQLite/检查点；受控 Worker；交替顺序、取中位数；不代表真实解题性能",
              "repeats": args.repeats, "baseline": str(args.baseline_dir.resolve()), "source_sha256": {}, "scenarios": {}}
    report["common_source_sha256"] = {name: hashlib.sha256((Path(current.__file__).parent / name).read_bytes()).hexdigest()
                                      for name in ("events.py", "profiling.py", "strategy_memory.py")}
    for label, directory in (("before", args.baseline_dir), ("after", Path(current.__file__).parent)):
        report["source_sha256"][label] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                         for name in ("swarm.py", "coordination.py", "intent_planner.py")}
    # 相同热身消除首次 import 的偏差。
    for module in versions.values():
        measure(module, "single")
    for scenario in SCENARIOS:
        samples = {name: [] for name in versions}
        for repeat in range(args.repeats):
            for label in (list(versions) if repeat % 2 == 0 else list(reversed(versions))):
                samples[label].append(measure(versions[label], scenario))
        report["scenarios"][scenario] = {label: {"median": {key: statistics.median(row[key] for row in rows) for key in rows[0]},
                                                        "samples": rows} for label, rows in samples.items()}
        print(scenario, {key: round(value["median"]["total_ms"], 2) for key, value in report["scenarios"][scenario].items()}, flush=True)
    report["incremental"] = {label: incremental(module) for label, module in versions.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(args.output.resolve()), flush=True)


if __name__ == "__main__":
    main()
