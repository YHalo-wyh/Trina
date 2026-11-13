"""事件驱动 Planner、共享待办、双图、Intent 租约与 Worker 经验复用。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from trina.coordination import CoordinationRuntime
from trina.state import RunState
from trina.swarm import TaskSpec


class CoordinationTests(unittest.TestCase):
    def tasks(self):
        return [
            TaskSpec("discover", "host.test", "识别服务", priority=10, max_seconds=30),
            TaskSpec("verify", "host.test", "验证服务", depends_on=("discover",), max_seconds=30),
        ]

    def records(self):
        return {key: SimpleNamespace(status="pending", error="") for key in ("discover", "verify")}

    def test_planner_only_advances_on_events_and_todo_keeps_dependencies(self):
        runtime = CoordinationRuntime(self.tasks())
        records = self.records()
        self.assertEqual(runtime.plan(records), ["discover"])
        rounds = runtime.planner_rounds
        self.assertEqual(runtime.plan(records), ["discover"])
        self.assertEqual(runtime.planner_rounds, rounds, "轮询本身不能让 Planner 重规划")
        lease = runtime.claim("discover", "worker-1", ttl=30)
        records["discover"].status = "running"
        runtime.settle("discover", lease, "completed")
        records["discover"].status = "completed"
        self.assertEqual(runtime.plan(records), ["verify"])
        self.assertEqual(runtime.todo["verify"]["status"], "ready")

    def test_fact_and_exploration_graphs_are_separate_and_anchored(self):
        runtime = CoordinationRuntime(self.tasks())
        records = self.records()
        runtime.plan(records)
        lease = runtime.claim("discover", "worker-1")
        state = RunState("run-discover", "host.test", "识别服务", status="success")
        state.host("host.test").ports[80] = {"service": "http"}
        state.steps.append({"index": 1, "action": "port_scan", "ok": True, "summary": "80/tcp open"})
        facts = runtime.publish_state("discover", state, evidence_refs=["evidence:1"])
        runtime.settle("discover", lease, "completed")
        self.assertTrue(facts)
        self.assertEqual(runtime.facts[facts[0]]["kind"], "asset")
        self.assertIn(f"intent:discover", runtime.exploration["nodes"])
        self.assertIn({"intent": "intent:discover", "fact": facts[0]}, runtime.exploration["anchors"])
        self.assertNotIn("status", runtime.facts[facts[0]]["payload"], "探索状态不能混入事实载荷")

    def test_trace_search_reuses_failures_but_excludes_current_worker(self):
        runtime = CoordinationRuntime(self.tasks())
        first = runtime.claim("discover", "worker-1", ttl=1)
        state = RunState("run", "host.test", "", status="failed")
        state.steps.append({"index": 1, "action": "nmap", "ok": False, "error": "connection timeout"})
        runtime.publish_state("discover", state)
        runtime.settle("discover", first, "failed", error="connection timeout")
        hits = runtime.search_traces("timeout", exclude_intent="verify")
        self.assertTrue(any(row["intent_id"] == "discover" and not row["ok"] for row in hits))
        self.assertFalse(runtime.search_traces("timeout", exclude_intent="discover"))

    def test_timeout_recovery_dedup_and_checkpoint_round_trip(self):
        runtime = CoordinationRuntime(self.tasks())
        self.assertFalse(runtime.add_intent(TaskSpec("duplicate", "host.test", "识别服务")))
        runtime.claim("discover", "worker-1", ttl=1)
        self.assertEqual(runtime.expire_leases(time.time() + 2), ["discover"])
        self.assertEqual(runtime.intents["discover"]["status"], "pending")
        restored = CoordinationRuntime.restore(self.tasks(), runtime.snapshot())
        self.assertEqual(restored.todo, runtime.todo)
        self.assertEqual(restored.exploration, runtime.exploration)
        previous_trace = restored.trace_seq
        lease = restored.claim("discover", "worker-2", ttl=30)
        self.assertGreater(restored.traces[-1]["id"], previous_trace)
        restored.settle("discover", lease, "completed")


if __name__ == "__main__":
    unittest.main()
