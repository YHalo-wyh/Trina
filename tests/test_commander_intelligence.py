"""Commander V0.8 的真实队列、可控事件和离线执行器验收。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.config import TrinaConfig
from trina.coordination import CoordinationRuntime
from trina.intent_planner import FactIntentPlanner
from trina.state import RunState
from trina.swarm import SwarmRuntime, TaskSpec


class IntelligenceQueueTests(unittest.TestCase):
    def test_fact_timestamps_do_not_manufacture_progress_or_replanning(self):
        from trina.state import Credential, Finding

        tasks = [TaskSpec("a", "a.test", "alpha")]
        runtime = CoordinationRuntime(tasks)
        runtime.claim("a", "worker")
        state = RunState("run", "a.test")
        state.findings = [Finding("f", "verified sample", "sample", target="a.test", status="verified",
                                  evidence=[{"id": "e", "ts": 1}])]
        state.credentials = [Credential("a.test", "http", "user", "password", verified=True)]
        state.flags = [{"value": "flag{test}", "ts": 1}]
        self.assertEqual(len(runtime.publish_state("a", state)), 3)
        runtime.plan()
        rounds = runtime.planner_rounds
        state.findings[0].last_seen += 100
        state.findings[0].evidence[0]["ts"] = 2
        state.credentials[0].ts += 100
        state.flags[0]["ts"] = 2
        self.assertEqual(runtime.publish_state("a", state), [])
        runtime.plan()
        self.assertEqual(runtime.planner_rounds, rounds)
        self.assertEqual(runtime.intents["a"]["effective_progress"], 3)
        restored = CoordinationRuntime.restore(tasks, runtime.snapshot())
        self.assertEqual(restored.publish_state("a", state), [])
        state.credentials[0].password = "new credential"
        self.assertEqual(len(restored.publish_state("a", state)), 1)

    def test_verifier_receipt_counts_progress_without_spawned_followup(self):
        task = TaskSpec("a", "puzzle.py", "验证本地结果")
        runtime = CoordinationRuntime([task])
        runtime.claim(task.id, "worker")
        state = RunState("run", task.target)
        state.casebook["goal_verifications"] = [{"status": "passed", "evidence_id": "cmd-1", "verifier": "python verifier.py"}]
        added = runtime.publish_state(task.id, state)
        self.assertEqual(len(added), 1)
        self.assertEqual(runtime.intents[task.id]["effective_progress"], 1)
        self.assertEqual(FactIntentPlanner().propose(task, [runtime.facts[key] for key in added]), [])

    def test_context_dependency_and_conditions_define_identity(self):
        first = TaskSpec("first", "https://host.test/API", "核对入口")
        runtime = CoordinationRuntime([first])
        self.assertFalse(runtime.add_intent(replace(first, id="duplicate", max_steps=3)))
        for task in (replace(first, id="context", context_key="input-v2"),
                     replace(first, id="dependent", depends_on=("first",)),
                     replace(first, id="path", target="https://host.test/api"),
                     replace(first, id="model", model="another-model")):
            self.assertTrue(runtime.add_intent(task))
        self.assertEqual(runtime.duplicate_tasks, 1)

    def test_claim_cannot_bypass_dependencies(self):
        tasks = [TaskSpec("a", "a.test", "提取接口"), TaskSpec("b", "b.test", "验证接口", depends_on=("a",))]
        runtime = CoordinationRuntime(tasks)
        with self.assertRaises(RuntimeError):
            runtime.claim("b", "worker")
        lease = runtime.claim("a", "worker")
        runtime.settle("a", lease, "completed")
        self.assertEqual(runtime.plan(), ["b"])
        self.assertTrue(runtime.claim("b", "worker"))

    def test_only_affected_priorities_are_recomputed(self):
        tasks = [TaskSpec("source", "source.test", "提取资产"), TaskSpec("alpha", "alpha.test", "review"),
                 TaskSpec("beta", "beta.test", "review")]
        runtime = CoordinationRuntime(tasks)
        runtime.plan()
        runtime.claim("source", "worker")
        state = RunState("run", "source.test")
        state.host("beta.test").ports[443] = {"service": "https"}
        with patch.object(runtime, "_dynamic_priority", wraps=runtime._dynamic_priority) as score:
            runtime.publish_state("source", state)
            self.assertEqual(runtime.plan(), ["beta", "alpha"])
            self.assertEqual([call.args[0]["id"] for call in score.call_args_list], ["beta"])
        changes = [event for event in runtime.events if event["kind"] == "priority.updated"]
        self.assertEqual([item["intent_id"] for item in changes[-1]["payload"]["changes"]], ["beta"])
        rounds = runtime.planner_rounds
        runtime.publish_state("source", state)
        runtime.plan()
        self.assertEqual(runtime.planner_rounds, rounds)

    def test_coalescing_does_not_discard_real_retry_transition(self):
        runtime = CoordinationRuntime([TaskSpec("a", "a.test", "review", max_attempts=3)])
        runtime.plan()
        for _ in range(2):
            lease = runtime.claim("a", "worker")
            runtime.retry("a", lease, "same error")
            self.assertEqual(runtime.plan(), ["a"])
        self.assertLess(runtime.priority_snapshot["a"]["completion_likelihood"], 0)
        self.assertEqual(runtime.planner_rounds, 3)

    @staticmethod
    def tool(seq, ok=False, status="failed"):
        return SimpleNamespace(seq=seq, task_id="a", worker_id="worker", call_id=str(seq),
                               payload={"tool": "probe", "ok": ok, "status": status, "summary": "same outcome"})

    def test_stall_requires_no_progress_and_actions_not_just_elapsed_time(self):
        runtime = CoordinationRuntime([TaskSpec("a", "a.test", "review")])
        runtime.claim("a", "worker")
        old = runtime.intents["a"]["last_progress_at"]
        self.assertEqual(runtime.assess_progress(now=old + 1000), [])
        for seq in range(1, 4):
            runtime.observe_tool(self.tool(seq))
        self.assertEqual(runtime.assess_progress(now=old + 1), [])
        self.assertEqual(runtime.assess_progress(now=old + 1000), ["a"])
        self.assertEqual(runtime.intents["a"]["status"], "running")
        self.assertEqual(runtime.assess_progress(now=old + 1100), [])
        state = RunState("run", "a.test")
        state.host("a.test").ports[80] = {"service": "http"}
        runtime.publish_state("a", state)
        self.assertEqual(runtime.intents["a"]["lifecycle"], "RUNNING")
        self.assertEqual(runtime.intents["a"]["effective_progress"], 1)

    def test_invalid_direction_requires_evidence_and_repeated_events_are_noop(self):
        runtime = CoordinationRuntime([TaskSpec("a", "a.test", "review")])
        runtime.claim("a", "worker")
        self.assertFalse(runtime.invalidate_direction("a", "guess", ["missing"]))
        for seq in range(1, 4):
            runtime.observe_tool(self.tool(seq, status="denied"))
        self.assertTrue(runtime.intents["a"]["direction_invalid"])
        runtime.plan()
        rounds = runtime.planner_rounds
        for _ in range(10):
            runtime.emit("direction.invalid", intent_id="a", reason="same")
            runtime.plan()
        self.assertEqual(runtime.planner_rounds, rounds)

    def test_usage_replay_and_checkpoint_do_not_double_count(self):
        tasks = [TaskSpec("a", "a.test", "review")]
        runtime = CoordinationRuntime(tasks)
        runtime.claim("a", "worker")
        start = SimpleNamespace(seq=10, task_id="a", kind="model.started", payload={})
        end = SimpleNamespace(seq=11, task_id="a", kind="model.finished", payload={"usage": {"total_tokens": 37}})
        runtime.observe_usage(start)
        runtime.observe_usage(end)
        runtime.intents["a"]["stop_loss_requested"] = True
        restored = CoordinationRuntime.restore(tasks, runtime.snapshot())
        restored.observe_usage(start)
        restored.observe_usage(end)
        self.assertEqual(restored.resource_stats()["model_requests"], 1)
        self.assertEqual(restored.resource_stats()["tokens"], 37)
        restored.recover_inflight()
        self.assertEqual(restored.intents["a"]["lifecycle"], "QUEUED")
        restored.claim("a", "new-worker")
        self.assertFalse(restored.intents["a"]["stop_loss_requested"])

    def test_ambiguous_duplicate_and_cyclic_plans_remain_single(self):
        for goal in ("分析完整目标", "独立任务：核对服务；核对服务", "独立任务：核对服务；然后提取证据",
                     "任务计划：1. alpha [依赖:2]；2. beta [依赖:1]"):
            task = TaskSpec("root", "local.test", goal)
            self.assertEqual(SwarmRuntime._decompose_task(task, FactIntentPlanner()), [task])


class IntelligenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = TrinaConfig.load().with_overrides(run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"), report__output_dir=str(root / "reports"))
        self.calls = []

    def execute(self, worker, task, run_id, hints, registry):
        self.calls.append(task.id)
        state = RunState(run_id, task.target, task.goal, status="success")
        state.ended_at = time.time()
        return state

    def runtime(self, tasks, **kwargs):
        return SwarmRuntime(self.config, tasks, executor=kwargs.pop("executor", self.execute),
                            backend_factory=lambda config: object(), write_report=False, **kwargs)

    def test_simple_task_uses_one_worker_no_scheduler_model_calls(self):
        runtime = self.runtime([TaskSpec("root", "local.test", "查看接口")], max_workers=4)
        summary = runtime.run()
        self.assertEqual(self.calls, ["root"])
        self.assertEqual(summary["workers_created"], 1)
        self.assertEqual(summary["resources"]["model_requests"], 0)
        self.assertEqual(runtime.event_bus.replay(kinds=("model.started",)), [])

    def test_repeated_state_events_only_project_changed_facts(self):
        runtime = self.runtime([TaskSpec("a", "a.test", "alpha")], max_workers=1)
        runtime.coordination.claim("a", "worker-1")
        state = RunState(f"{runtime.run_id}-a-1", "a.test", status="running")
        state.host("a.test").ports[80] = {"service": "http"}
        with runtime.event_bus.keep_open(), patch.object(runtime.coordination, "publish_state", wraps=runtime.coordination.publish_state) as publish:
            for index in range(12):
                state.counters["tools"] = index
                runtime.event_bus.publish("state.updated", run_id=state.run_id, task_id="a", worker_id="worker-1", state=state.to_dict())
            runtime._consume_events()
            self.assertEqual(publish.call_count, 1)
            state.host("a.test").ports[443] = {"service": "https"}
            runtime.event_bus.publish("state.updated", run_id=state.run_id, task_id="a", worker_id="worker-1", state=state.to_dict())
            runtime._consume_events()
            self.assertEqual(publish.call_count, 2)
        self.assertEqual(len(runtime.coordination.facts), 2)

    def test_explicit_independent_goal_is_split_and_joined_without_extra_agent(self):
        barrier = threading.Barrier(2)

        def execute(worker, task, run_id, hints, registry):
            barrier.wait(timeout=5)
            state = self.execute(worker, task, run_id, hints, registry)
            state.host(task.id + ".test").ports[80] = {"service": "http"}
            return state

        runtime = self.runtime([TaskSpec("root", "local.test", "独立任务：提取接口 => 接口清单；分析证书 => 证书结论", max_steps=10)],
                               executor=execute, max_workers=2)
        summary = runtime.run()
        self.assertEqual(set(self.calls), {"root-part-1", "root-part-2"})
        self.assertEqual(summary["workers_created"], 2)
        self.assertEqual(summary["tasks"]["root"]["status"], "completed")
        self.assertEqual(len(summary["tasks"]["root"]["result"]["verified_facts"]), 2)
        self.assertEqual(sum(item.max_steps for item in runtime.tasks if not item.aggregate), 10)
        self.assertTrue(all(item.deliverable for item in runtime.tasks))
        self.assertEqual(summary["resources"]["model_requests"], 0)

    def test_explicit_plan_dependencies_run_in_order(self):
        runtime = self.runtime([TaskSpec("root", "local.test", "任务计划：1. 提取接口；2. 核对权限 [依赖:1]")], max_workers=2)
        runtime.run()
        self.assertEqual(self.calls, ["root-part-1", "root-part-2"])
        self.assertEqual(len(runtime.pool.workers), 1)

    def test_aggregate_completion_unlocks_out_of_order_dependency_graph(self):
        tasks = [TaskSpec("root", "local.test", "汇总", depends_on=("group",), aggregate=True),
                 TaskSpec("next", "next.test", "后继工作", depends_on=("root",)),
                 TaskSpec("group", "local.test", "分组汇总", depends_on=("leaf",), aggregate=True),
                 TaskSpec("leaf", "leaf.test", "实际工作")]
        runtime = self.runtime(tasks, max_workers=1)
        runtime.run()
        self.assertEqual(self.calls, ["leaf", "next"])
        self.assertTrue(all(record.status == "completed" for record in runtime.records.values()))

    def test_dynamic_submission_uses_same_decomposition_and_dedup(self):
        runtime = self.runtime([TaskSpec("seed", "seed.test", "初始任务")], max_workers=2)
        goal = TaskSpec("root", "local.test", "独立任务：提取接口；分析证书")
        future = runtime.submit_task(goal)
        runtime._drain_submissions()
        self.assertTrue(future.result())
        self.assertFalse(runtime.add_task(goal))
        runtime.run()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(runtime.records["root"].status, "completed")

    def test_failure_releases_slot_and_preserves_successful_group_results(self):
        def execute(worker, task, run_id, hints, registry):
            if task.id.endswith("1"):
                raise RuntimeError("controlled failure")
            state = self.execute(worker, task, run_id, hints, registry)
            state.host("local.test").ports[443] = {"service": "https"}
            return state

        runtime = self.runtime([TaskSpec("root", "local.test", "独立任务：提取接口；分析证书")], executor=execute, max_workers=1)
        runtime.run()
        self.assertEqual(runtime.records["root-part-1"].status, "failed")
        self.assertEqual(runtime.records["root"].status, "blocked")
        self.assertTrue(runtime.records["root"].result["verified_facts"])
        self.assertTrue(all(not worker.busy and not worker.capability_lease_id for worker in runtime.pool.workers))

    def test_cancel_releases_worker_before_next_task(self):
        runtime = None

        def execute(worker, task, run_id, hints, registry):
            state = self.execute(worker, task, run_id, hints, registry)
            if task.id == "first":
                accepted = runtime.control_task(task.id, "cancel")
                self.assertTrue(accepted.result(timeout=5))
                self.assertTrue(runtime._task_stops[task.id].wait(5))
                state.status = "stopped"
            return state

        runtime = self.runtime([TaskSpec("first", "a.test", "alpha", priority=1), TaskSpec("second", "b.test", "beta")],
                               executor=execute, max_workers=1)
        summary = runtime.run()
        self.assertEqual(runtime.records["first"].status, "cancelled")
        self.assertEqual(self.calls, ["first", "second"])
        self.assertEqual(summary["worker_reuses"], 1)

    def test_completion_notification_observes_committed_result_snapshot(self):
        runtime = self.runtime([TaskSpec("a", "a.test", "alpha")], max_workers=1)
        snapshot = {}
        observed = []
        runtime.on_update = lambda value: snapshot.update(value)
        subscription = runtime.event_bus.subscribe(
            lambda event: observed.append((event.task_id, snapshot["tasks"][event.task_id]["status"])),
            after=runtime.event_bus.last_seq, kinds=("task.result_ready",))
        self.addCleanup(subscription.close)
        runtime.run()
        runtime._save()
        self.assertEqual(observed, [("a", "completed")])
        events = runtime.event_bus.replay(kinds=("worker.executing", "worker.finished", "task.result_ready"))
        self.assertEqual([event.kind for event in events], ["worker.executing", "worker.finished", "task.result_ready"])
        self.assertLessEqual(runtime.records["a"].execution_seconds,
                             runtime.records["a"].ended_at - runtime.records["a"].started_at)

    def test_cancelled_executor_exception_is_not_failed_or_retried(self):
        runtime = None

        def execute(worker, task, run_id, hints, registry):
            runtime.control_task(task.id, "cancel").result(5)
            raise InterruptedError("cooperative stop")

        runtime = self.runtime([TaskSpec("a", "a.test", "alpha", max_attempts=3)], executor=execute, max_workers=1)
        runtime.run()
        self.assertEqual(runtime.records["a"].status, "cancelled")
        self.assertEqual(runtime.records["a"].attempt, 1)
        self.assertEqual(runtime.pool.available(), 1)
        self.assertFalse(runtime.pool.workers[0].capability_lease_id)

    def test_cancelled_group_notifies_only_after_branches_are_readable(self):
        runtime = None
        snapshots = []
        def execute(worker, task, run_id, hints, registry):
            runtime.control_task("root", "cancel").result(5)
            state = self.execute(worker, task, run_id, hints, registry)
            state.host(task.target).ports[443] = {"service": "https"}
            state.status = "stopped"
            return state
        runtime = self.runtime([TaskSpec("root", "a.test", "独立任务：检查接口；检查证书")], executor=execute, max_workers=1)
        subscription = runtime.event_bus.subscribe(lambda event: snapshots.append(dict(runtime.records["root"].result))
            if event.task_id == "root" else None, after=runtime.event_bus.last_seq, kinds=("task.result_ready",))
        self.addCleanup(subscription.close)
        runtime.run()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["status"], "cancelled")
        self.assertTrue(snapshots[0]["verified_facts"])

    def test_global_stop_preserves_cancelled_group_results_and_checkpoint(self):
        runtime = None
        def execute(worker, task, run_id, hints, registry):
            runtime.stop_event.set()
            state = self.execute(worker, task, run_id, hints, registry)
            state.host(task.target).ports[80] = {"service": "http"}
            state.status = "stopped"
            return state
        runtime = self.runtime([TaskSpec("root", "a.test", "独立任务：检查接口；检查证书")], executor=execute, max_workers=1)
        runtime.run()
        self.assertEqual(runtime.records["root"].status, "cancelled")
        self.assertTrue(runtime.records["root"].result["verified_facts"])
        restored = SwarmRuntime.resume(self.config, runtime.checkpoint, executor=self.execute,
                                       backend_factory=lambda config: object())
        restored.run()
        self.assertEqual(self.calls, ["root-part-1"])
        self.assertEqual(restored.records["root"].result, runtime.records["root"].result)

    def test_stalled_worker_without_confirmed_invalidity_keeps_lease(self):
        runtime = self.runtime([TaskSpec("a", "a.test", "alpha"), TaskSpec("b", "b.test", "beta")], max_workers=1)
        lease = runtime.coordination.claim("a", "worker")
        row = runtime.coordination.intents["a"]
        row.update(last_progress_at=time.time() - 400, no_progress_actions=3)
        runtime._assess_workers()
        self.assertEqual(row["lifecycle"], "STALLED")
        self.assertEqual(row["lease_id"], lease)
        self.assertFalse(runtime._task_stops["a"].is_set())

    def test_confirmed_stall_cooperatively_stops_and_keeps_evidence(self):
        runtime = None

        def execute(worker, task, run_id, hints, registry):
            state = self.execute(worker, task, run_id, hints, registry)
            if task.id == "a":
                state.host("a.test").ports[80] = {"service": "http"}
                runtime.event_bus.publish("state.updated", source="worker", task_id=task.id, worker_id=worker.id,
                                          run_id=run_id, state=state.to_dict())
                for seq in range(3):
                    runtime.event_bus.publish("tool.finished", source="worker", task_id=task.id, worker_id=worker.id,
                        run_id=run_id, call_id=str(seq), tool="probe", ok=False, status="denied", summary="same denial")
                self.assertTrue(runtime._task_stops[task.id].wait(5))
                state.status = "stopped"
            return state

        runtime = self.runtime([TaskSpec("a", "a.test", "alpha", priority=2), TaskSpec("b", "b.test", "beta")],
                               executor=execute, max_workers=1)
        real_assess = runtime.coordination.assess_progress
        with patch.object(runtime.coordination, "assess_progress", side_effect=lambda: real_assess(stale_seconds=0)):
            runtime.run()
        self.assertEqual(runtime.records["a"].status, "blocked")
        self.assertTrue(runtime.records["a"].result["verified_facts"])
        self.assertEqual(runtime.records["b"].status, "completed")
        self.assertTrue(runtime.event_bus.replay(kinds=("coordination.worker.stop_loss",)))

    def test_checkpoint_restores_plan_without_resplitting_or_repeating_finished_tasks(self):
        runtime = self.runtime([TaskSpec("root", "local.test", "独立任务：提取接口；分析证书")], max_workers=1)
        runtime.run()
        count = len(self.calls)
        restored = SwarmRuntime.resume(self.config, runtime.checkpoint, executor=self.execute,
                                       backend_factory=lambda config: object())
        restored.run()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(restored.tasks), 3)
        self.assertEqual(restored.records["root"].status, "completed")

    def test_default_model_identity_survives_restore_and_explicit_defaults(self):
        task = TaskSpec("a", "a.test", "alpha")
        runtime = self.runtime([task], max_workers=1)
        runtime.run()
        restored = SwarmRuntime.resume(self.config, runtime.checkpoint, executor=self.execute,
                                       backend_factory=lambda config: object())
        self.assertFalse(restored.add_task(replace(task, id="again")))
        self.assertFalse(runtime.add_task(replace(task, id="explicit", model=runtime._task_llms["a"].model,
                                                reasoning_effort=runtime._task_llms["a"].reasoning_effort)))

    def test_existing_delegation_tool_passes_context_and_deliverable_into_runtime(self):
        from trina.tools.coordination import WorkerTasks
        runtime = self.runtime([TaskSpec("seed", "seed.test", "seed")], max_workers=2)
        manager = SimpleNamespace(runtime=runtime, submit=lambda config, task, **kwargs: runtime.add_task(task))
        ctx = SimpleNamespace(config=self.config, extra={"subworkers": manager})
        result = WorkerTasks().run(ctx, "spawn", task_id="root", target="a.test",
            goal="独立任务：提取接口；分析证书", context_key="input-revision-2", deliverable="交付证据")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["accepted"])
        self.assertTrue(all(task.context_key == "input-revision-2" for task in runtime.tasks if task.id != "seed"))
        runtime.run()
        self.assertEqual(runtime.records["root"].status, "completed")

    def test_simultaneous_completions_share_checkpoint_and_release_all_slots(self):
        barrier = threading.Barrier(4)

        def execute(worker, task, run_id, hints, registry):
            barrier.wait(timeout=5)
            return self.execute(worker, task, run_id, hints, registry)

        tasks = [TaskSpec(f"t{i}", f"h{i}.test", f"direction{i}") for i in range(4)]
        runtime = self.runtime(tasks, executor=execute, max_workers=4)
        runtime.run()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(runtime.pool.available(), 4)
        self.assertLess(len(runtime.event_bus.replay(kinds=("runtime.checkpoint",))), 6)

    def test_duplicate_tool_response_points_to_correct_execution_context(self):
        from trina.tools.coordination import WorkerTasks
        first = TaskSpec("first", "a.test", "alpha", context_key="input-1")
        second = replace(first, id="second", context_key="input-2")
        runtime = self.runtime([first, second], max_workers=1)
        manager = SimpleNamespace(runtime=runtime, submit=lambda config, task, **kwargs: runtime.add_task(task))
        ctx = SimpleNamespace(config=self.config, extra={"subworkers": manager})
        result = WorkerTasks().run(ctx, "spawn", task_id="again", target="a.test", goal="alpha", context_key="input-2")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["accepted"])
        self.assertEqual(result.data["task_id"], "second")

    def test_usage_is_live_then_reconciled_once_and_worker_reused(self):
        runtime = None

        def execute(worker, task, run_id, hints, registry):
            state = self.execute(worker, task, run_id, hints, registry)
            for kind, payload in (("model.started", {}), ("model.finished", {"usage": {"total_tokens": 20}})):
                runtime.event_bus.publish(kind, run_id=run_id, source="worker", task_id=task.id, worker_id=worker.id, **payload)
            state.ledger = {"calls": 1, "total_tokens": 20}
            return state

        runtime = self.runtime([TaskSpec("a", "a.test", "alpha"), TaskSpec("b", "b.test", "beta")],
                               executor=execute, max_workers=1)
        summary = runtime.run()
        self.assertEqual(summary["resources"]["model_requests"], 2)
        self.assertEqual(summary["resources"]["tokens"], 40)
        self.assertEqual(summary["resources"]["workers"]["worker-1"]["tokens"], 40)


if __name__ == "__main__":
    unittest.main()
