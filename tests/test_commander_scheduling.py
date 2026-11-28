"""Commander 动态优先级、事件重规划和 Worker 分配测试。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from trina.config import TrinaConfig
from trina.coordination import CoordinationRuntime
from trina.state import RunState
from trina.swarm import SwarmRuntime, TaskSpec


class CommanderPriorityTests(unittest.TestCase):
    def test_high_priority_task_runs_first(self) -> None:
        runtime = CoordinationRuntime([
            TaskSpec("low", "low.test", "低价值任务", priority=1),
            TaskSpec("high", "high.test", "高价值任务", priority=9),
        ])
        self.assertEqual(runtime.plan(), ["high", "low"])

    def test_equal_priority_keeps_queue_order(self) -> None:
        runtime = CoordinationRuntime([
            TaskSpec("z-first", "one.test", "首个任务", priority=3, max_steps=4),
            TaskSpec("a-second", "two.test", "第二个任务", priority=3, max_steps=4),
        ])
        self.assertEqual(runtime.plan(), ["z-first", "a-second"])

    def test_unsatisfied_dependency_is_not_ready(self) -> None:
        runtime = CoordinationRuntime([
            TaskSpec("parent", "host.test", "发现入口"),
            TaskSpec("child", "host.test", "验证入口", depends_on=("parent",), priority=100),
        ])
        self.assertEqual(runtime.plan(), ["parent"])
        self.assertEqual(runtime.todo["child"]["status"], "waiting")

    def test_satisfied_dependency_enters_ready_queue_immediately(self) -> None:
        runtime = CoordinationRuntime([
            TaskSpec("parent", "host.test", "发现入口"),
            TaskSpec("child", "host.test", "验证入口", depends_on=("parent",)),
        ])
        runtime.plan()
        lease = runtime.claim("parent", "worker-1")
        runtime.settle("parent", lease, "completed")
        self.assertEqual(runtime.plan(), ["child"])
        self.assertEqual(runtime.todo["child"]["status"], "ready")

    def test_new_evidence_reorders_affected_task(self) -> None:
        runtime = CoordinationRuntime([
            TaskSpec("source", "seed.test", "收集资产", priority=10),
            TaskSpec("alpha", "alpha.test", "核对服务", max_steps=4),
            TaskSpec("beta", "beta.test", "核对服务", max_steps=4),
        ])
        self.assertEqual(runtime.plan(), ["source", "alpha", "beta"])
        runtime.claim("source", "worker-1")
        state = RunState("source-run", "seed.test", status="running")
        state.host("beta.test").ports[443] = {"service": "https"}
        runtime.publish_state("source", state)
        self.assertEqual(runtime.plan(), ["beta", "alpha"])
        self.assertGreater(
            runtime.priority_snapshot["beta"]["new_information"],
            runtime.priority_snapshot["alpha"]["new_information"],
        )

    def test_duplicate_events_are_coalesced_without_replanning(self) -> None:
        runtime = CoordinationRuntime([TaskSpec("task", "host.test", "核对服务")])
        runtime.plan()
        runtime.emit("worker.idle", intent_id="task", worker_id="worker-1")
        runtime.plan()
        rounds = runtime.planner_rounds
        runtime.emit("worker.idle", intent_id="task", worker_id="worker-1")
        runtime.plan()
        time.sleep(0.06)
        runtime.emit("worker.idle", intent_id="task", worker_id="worker-1")
        runtime.plan()
        self.assertEqual(runtime.planner_rounds, rounds)
        self.assertEqual(runtime.coalesced_events, 1)


class CommanderWorkerAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = TrinaConfig.load().with_overrides(
            run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"),
            report__output_dir=str(root / "reports"),
        )

    @staticmethod
    def _state(task: TaskSpec, run_id: str) -> RunState:
        state = RunState(run_id=run_id, target=task.target, goal=task.goal, status="success")
        state.ended_at = time.time()
        return state

    def test_idle_worker_claims_next_task(self) -> None:
        calls: list[str] = []

        def execute(worker, task, run_id, hints, registry):
            del worker, hints, registry
            calls.append(task.id)
            return self._state(task, run_id)

        runtime = SwarmRuntime(
            self.config,
            [
                TaskSpec("first", "one.test", "首个任务", priority=2),
                TaskSpec("second", "two.test", "后续任务", priority=1),
            ],
            max_workers=1,
            executor=execute,
            write_report=False,
        )
        summary = runtime.run()
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(summary["workers_created"], 1)
        self.assertEqual(summary["worker_reuses"], 1)

    def test_swarm_uses_new_evidence_to_reorder_pending_tasks(self) -> None:
        calls: list[str] = []

        def execute(worker, task, run_id, hints, registry):
            del worker, hints, registry
            calls.append(task.id)
            state = self._state(task, run_id)
            if task.id == "source":
                state.host("beta.test").ports[443] = {"service": "https"}
            return state

        runtime = SwarmRuntime(
            self.config,
            [
                TaskSpec("source", "seed.test", "收集资产", priority=10),
                TaskSpec("alpha", "alpha.test", "核对服务", max_steps=4),
                TaskSpec("beta", "beta.test", "核对服务", max_steps=4),
            ],
            max_workers=1,
            executor=execute,
            write_report=False,
        )
        runtime.run()
        self.assertEqual(calls, ["source", "beta", "alpha"])

    def test_similar_tasks_are_not_dispatched_in_parallel_or_twice(self) -> None:
        lock = threading.Lock()
        active = 0
        maximum = 0
        calls: list[str] = []

        def execute(worker, task, run_id, hints, registry):
            nonlocal active, maximum
            del worker, hints, registry
            with lock:
                calls.append(task.id)
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return self._state(task, run_id)

        runtime = SwarmRuntime(
            self.config,
            [
                TaskSpec("check", "same.test", "检查 Web 服务"),
                TaskSpec("check-again", "same.test", "检查 Web 服务 again"),
            ],
            max_workers=2,
            executor=execute,
            write_report=False,
        )
        runtime.run()
        self.assertEqual(maximum, 1)
        self.assertEqual(Counter(calls), Counter({"check": 1, "check-again": 1}))

    def test_independent_tasks_respect_concurrency_limit(self) -> None:
        lock = threading.Lock()
        active = 0
        maximum = 0

        def execute(worker, task, run_id, hints, registry):
            nonlocal active, maximum
            del worker, hints, registry
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return self._state(task, run_id)

        tasks = [TaskSpec(f"task-{index}", f"host-{index}.test", f"独立任务 {index}") for index in range(4)]
        runtime = SwarmRuntime(
            self.config, tasks, max_workers=2, executor=execute, write_report=False,
        )
        summary = runtime.run()
        self.assertEqual(maximum, 2)
        self.assertEqual(summary["workers_created"], 2)
        self.assertTrue(all(item["status"] == "completed" for item in summary["tasks"].values()))

    def test_scheduler_adds_no_model_requests(self) -> None:
        def execute(worker, task, run_id, hints, registry):
            del worker, hints, registry
            return self._state(task, run_id)

        runtime = SwarmRuntime(
            self.config,
            [TaskSpec("one", "one.test", "任务一"), TaskSpec("two", "two.test", "任务二")],
            max_workers=1,
            executor=execute,
            write_report=False,
        )
        summary = runtime.run()
        self.assertEqual(runtime.event_bus.replay(kinds=("model.started",)), [])
        self.assertEqual(summary["coordination"]["scheduler_model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
