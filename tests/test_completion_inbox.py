"""后台完成事件进入 Agent 决策循环的事件驱动合同。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from trina.brain.runtime import ActionOutcome, AgentRuntime
from trina.completion_inbox import CompletionInbox
from trina.events import EventBus
from trina.llm import LLMResponse
from trina.llm.base import ToolCall


class _Context:
    def __init__(self, path: Path):
        self.events = EventBus(path, "run")
        self.state = SimpleNamespace(run_id="run")
        self.extra = {"worker_id": "", "intent_id": ""}

    def emit(self, kind: str, **payload):
        return self.events.publish(kind, run_id="run", **payload)


class CompletionInboxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ctx = _Context(Path(directory.name) / "events.sqlite3")
        self.inbox = CompletionInbox(self.ctx)
        self.addCleanup(self.inbox.close)

    def job(self, kind: str, job_id: str, **payload):
        self.ctx.events.publish(
            kind, run_id="run", call_id=job_id, job_id=job_id, tool="terminal_start", **payload,
        )

    def test_wait_is_event_driven_and_coalesces_adjacent_completions(self):
        self.job("job.started", "a", status="running")
        self.job("job.started", "b", status="running")
        messages: list[dict] = []
        result: list[bool] = []
        waiter = threading.Thread(target=lambda: result.append(
            self.inbox.deliver(messages, wait=True, deadline=time.monotonic() + 2)
        ))
        waiter.start()
        time.sleep(.03)
        self.job("job.finished", "a", status="completed", data={"stdout": "A"})
        time.sleep(.01)
        self.job("job.finished", "b", status="failed", error="boom", data={"stderr": "B"})
        waiter.join(2)

        self.assertEqual(result, [True])
        self.assertEqual(len(messages), 1)
        rows = json.loads(messages[0]["content"].split("\n", 1)[1])
        self.assertEqual([row["id"] for row in rows], ["a", "b"])
        self.assertEqual([row["status"] for row in rows], ["completed", "failed"])
        self.assertFalse(self.inbox.deliver(messages, wait=False))
        resumed = self.ctx.events.replay(kinds=("agent.resumed",))
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0].payload["count"], 2)

    def test_close_releases_waiter_without_restarting_agent(self):
        self.job("job.started", "slow", status="running")
        result: list[bool] = []
        waiter = threading.Thread(target=lambda: result.append(
            self.inbox.deliver([], wait=True, deadline=time.monotonic() + 5)
        ))
        waiter.start()
        time.sleep(.03)
        self.inbox.close()
        waiter.join(2)
        self.assertEqual(result, [False])
        self.assertEqual(self.ctx.events.replay(kinds=("agent.resumed",)), [])

    def test_service_without_followup_does_not_wait_or_resume_model(self):
        self.job("job.started", "server", status="running", follow_up=False)
        self.assertFalse(self.inbox.deliver([], wait=True))
        self.job("job.finished", "server", status="completed", data={"stdout": "done"})
        self.assertFalse(self.inbox.deliver([], wait=True))
        self.assertEqual(self.ctx.events.replay(kinds=("agent.resumed",)), [])

    def test_runtime_requests_one_followup_for_one_completion_batch(self):
        responses = [
            LLMResponse(tool_calls=[ToolCall("finish-1", "finish", '{"summary":"first"}')]),
            LLMResponse(tool_calls=[ToolCall("finish-2", "finish", '{"summary":"second"}')]),
        ]
        messages: list[dict] = []
        resumed = False

        def resume(*, wait=False):
            nonlocal resumed
            if wait and not resumed:
                resumed = True
                messages.append({"role": "user", "content": "[后台任务完成]"})
                return True
            return False

        def execute(action, *_args, **_kwargs):
            return ActionOutcome(
                observation=action["final"]["summary"], answer=action["final"]["summary"], stop=True,
            )

        runtime = AgentRuntime(
            max_steps=4, messages=messages, request=lambda *_: responses.pop(0), execute=execute,
            decode_text=lambda _: None, native=lambda: True, resume=resume,
        )
        outcome = runtime.run()
        self.assertEqual(outcome.answer, "second")
        self.assertEqual(runtime.rounds, 2)
        self.assertTrue(resumed)

    def test_acknowledged_result_does_not_request_followup(self):
        self.job("job.started", "a", status="running")
        self.job("job.finished", "a", status="completed")
        self.inbox.acknowledge(self.ctx.events.stream_id, "a")
        self.assertFalse(self.inbox.deliver([], wait=True))
        self.job("job.finished", "a", status="completed")
        self.assertFalse(self.inbox.deliver([], wait=True))

    def test_snapshot_restores_pending_once_and_replays_new_completion(self):
        self.job("job.started", "a", status="running")
        self.job("job.started", "b", status="running")
        self.job("job.finished", "a", status="completed")
        snapshot = self.inbox.snapshot()
        self.inbox.close()
        self.job("job.finished", "b", status="timed_out", error="timeout")
        restored = CompletionInbox(self.ctx, snapshot)
        self.addCleanup(restored.close)
        messages = []
        self.assertTrue(restored.deliver(messages))
        self.assertEqual(len(json.loads(messages[0]["content"].split("\n", 1)[1])), 2)
        consumed = restored.snapshot()
        restored.close()
        again = CompletionInbox(self.ctx, consumed)
        self.addCleanup(again.close)
        self.assertFalse(again.deliver(messages, wait=True))

    def test_foreign_worker_events_do_not_enter_context_and_parent_call_is_preserved(self):
        self.ctx.events.publish("job.started", run_id="run", worker_id="other", job_id="foreign")
        self.ctx.events.publish("job.finished", run_id="run", worker_id="other", job_id="foreign", status="completed")
        self.assertFalse(self.inbox.deliver([]))
        self.job("job.started", "a", status="running", parent_call_id="original-call")
        self.job("job.finished", "a", status="completed", parent_call_id="original-call")
        messages = []
        self.inbox.deliver(messages)
        self.assertEqual(json.loads(messages[0]["content"].split("\n", 1)[1])[0]["tool_call_id"], "original-call")

    def test_worker_result_ready_wakes_before_whole_swarm_finishes(self):
        worker_bus = EventBus(self.ctx.events.path.parent / "worker.sqlite3", "swarm")
        item = {"status": "running", "goal": "read source", "result": {}}
        manager = SimpleNamespace(runtime=SimpleNamespace(event_bus=worker_bus), task_result=lambda task_id: dict(item))
        self.inbox.watch_worker(manager, "child")
        worker_bus.publish("task.finished", task_id="child", status="completed")
        self.assertFalse(self.inbox.deliver([]))
        item.update(status="completed", result={"verdict": "verified", "verified_facts": []})
        worker_bus.publish("task.result_ready", task_id="child", status="completed")
        messages = []
        self.assertTrue(self.inbox.deliver(messages))
        self.assertIn("verified", messages[0]["content"])
        worker_bus.publish("runtime.finished")
        self.assertFalse(self.inbox.deliver(messages))

    def test_cancellation_releases_waiter_without_model_followup(self):
        self.job("job.started", "a", status="running")
        stop = threading.Event()
        result = []
        waiter = threading.Thread(target=lambda: result.append(self.inbox.deliver([], wait=True, stop_event=stop)))
        waiter.start()
        stop.set()
        waiter.join(1)
        self.assertEqual(result, [False])

    def test_restore_reads_completed_worker_checkpoint_without_redispatch(self):
        self.ctx.config = SimpleNamespace(workspace_dir=self.ctx.events.path.parent)
        worker_bus = EventBus(self.ctx.events.path.parent / "workers.sqlite3", "swarm")
        checkpoint = worker_bus.path.parent / "swarm.json"
        manager = SimpleNamespace(runtime=SimpleNamespace(event_bus=worker_bus, checkpoint=checkpoint),
                                  task_result=lambda task_id: {"status": "running"})
        self.inbox.watch_worker(manager, "child")
        snapshot = self.inbox.snapshot()
        self.inbox.close()
        checkpoint.write_text(json.dumps({"run_id": "swarm", "tasks": [{"id": "child"}], "coordination": {
            "intents": {"child": {"status": "completed", "result": {"verdict": "verified checkpoint"},
                                   "evidence_refs": ["proof-1"]}}}}), encoding="utf-8")
        restored = CompletionInbox(self.ctx, snapshot)
        self.addCleanup(restored.close)
        messages = []
        self.assertTrue(restored.deliver(messages))
        self.assertIn("verified checkpoint", messages[0]["content"])
        self.assertIn("proof-1", messages[0]["content"])
        self.assertFalse(restored.deliver(messages, wait=True))

    def test_recovered_job_preserves_original_tool_call(self):
        self.job("job.started", "a", parent_call_id="original-call")
        snapshot = self.inbox.snapshot()
        self.inbox.close()
        restored = CompletionInbox(self.ctx, snapshot)
        self.addCleanup(restored.close)
        self.ctx.events.interrupt_open_calls()
        messages = []
        self.assertTrue(restored.deliver(messages))
        self.assertIn("interrupted", messages[0]["content"])
        self.assertIn("original-call", messages[0]["content"])

    def test_old_worker_notification_cannot_read_new_group_with_same_task_id(self):
        first = EventBus(self.ctx.events.path.parent / "first.sqlite3", "first")
        second = EventBus(self.ctx.events.path.parent / "second.sqlite3", "second")
        manager = SimpleNamespace(runtime=SimpleNamespace(event_bus=first),
                                  task_result=lambda task_id: {"status": "running"})
        self.inbox.watch_worker(manager, "same-task")
        manager.runtime = SimpleNamespace(event_bus=second)
        manager.task_result = lambda task_id: {"status": "completed", "result": {"summary": "wrong group"}}
        first.publish("task.result_ready", task_id="same-task")
        messages = []
        self.assertTrue(self.inbox.deliver(messages))
        self.assertNotIn("wrong group", messages[0]["content"])
        self.assertIn("interrupted", messages[0]["content"])

    def test_manual_historical_worker_result_acknowledges_original_stream(self):
        from dataclasses import asdict
        from trina.subworkers import Subworkers
        from trina.swarm import TaskRecord
        from trina.tools.coordination import WorkerTasks

        first = EventBus(self.ctx.events.path.parent / "first.sqlite3", "first")
        second = EventBus(self.ctx.events.path.parent / "second.sqlite3", "second")
        manager = Subworkers(lambda _: None)
        manager.runtime = SimpleNamespace(event_bus=first)
        record = {**asdict(TaskRecord()), "goal": "original", "status": "running"}
        manager._update({"run_id": "first", "tasks": {"old": record}})
        self.inbox.watch_worker(manager, "old")
        record = {**record, "status": "completed", "result": {"summary": "original result"}}
        manager._update({"run_id": "first", "tasks": {"old": record}})
        first.publish("task.result_ready", task_id="old")
        manager.runtime = SimpleNamespace(event_bus=second)
        manager._update({"run_id": "second", "tasks": {}})
        self.ctx.extra.update(subworkers=manager, completion_inbox=self.inbox)
        result = WorkerTasks().run(self.ctx, "result", task_id="old")
        self.assertTrue(result.ok)
        self.assertFalse(self.inbox.deliver([], wait=True), "手动读取旧组结果不能留下重复续接通知")
        self.assertEqual(result.data["stream_id"], "first")


if __name__ == "__main__":
    unittest.main()
