"""统一事件协议：并发顺序、持久游标、重放续流、工具生命周期与实时调度。"""
from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.events import EventBus
from trina.event_view import EventTranscript
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools.http import HttpClient
from trina.tools.registry import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec


def _publish_in_process(path: str) -> None:
    EventBus.open_existing(path).publish("other.process", text="已提交")


class _EventTool(Tool):
    spec = ToolSpec(name="event_test", summary="事件测试", params={"mode": ParamSpec("str")})

    def run(self, ctx, mode):
        if mode == "crash":
            raise ValueError("测试异常")
        if mode == "late":
            time.sleep(.15)
            ctx.emit("tool.output", delta="LATE")
        ctx.log("正在读取")
        return ToolResult(summary="已读取", raw="原始输出", facts={"ports": [{"host": "127.0.0.1", "port": 80}]})


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bus = EventBus(self.root / "events.sqlite3", "stream-test")

    def context(self, run_id="test", *, bus=None):
        config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
                                            audit__dir=str(self.root / "logs"),
                                            report__output_dir=str(self.root / "reports"))
        audit = AuditLog.open(config.audit_dir, run_id=run_id, resume=False)
        guard = ScopeGuard(config.scope, audit=audit)
        return ToolContext(config=config, guard=guard, audit=audit, state=RunState(run_id, "127.0.0.1"),
                           http=HttpClient(guard), workspace=config.workspace_dir / run_id, event_bus=bus or self.bus)

    def test_concurrent_publish_is_ordered_and_payload_is_a_snapshot(self):
        seen = []
        self.bus.subscribe(lambda event: seen.append(event.seq))
        threads = [threading.Thread(target=lambda: [self.bus.publish("test", number=i) for i in range(15)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(seen, list(range(1, 61)))
        data = {"nested": {"token": "do-not-persist", "values": [1]}}
        event = self.bus.publish("snapshot", data=data)
        data["nested"]["values"].append(2)
        self.assertEqual(event.payload["data"]["nested"]["values"], [1])
        self.assertNotIn("do-not-persist", json.dumps(event.to_dict()))

    def test_deep_checkpoint_data_is_preserved_and_redaction_is_idempotent(self):
        data = {"child": {"child": {"child": {"child": {"child": {"child": {"port": 80, "password": "secret"}}}}}}}
        first = self.bus.publish("snapshot", data=data)
        second = self.bus.publish("snapshot", **first.payload)
        self.assertEqual(first.payload, second.payload)
        self.assertNotIn("max-depth", json.dumps(second.payload))
        self.assertNotIn('"secret"', json.dumps(second.payload))

    def test_acknowledged_cursor_survives_reopen_unacked_delivery_repeats(self):
        for i in range(5):
            self.bus.publish("test", number=i)
        consumer = self.bus.subscribe(name="ui")
        self.assertEqual([event.seq for event in consumer.poll(limit=2)], [1, 2])
        consumer.ack()
        self.assertEqual([event.seq for event in consumer.poll(limit=2)], [3, 4])
        consumer.close()
        resumed = EventBus.open_existing(self.bus.path).subscribe(name="ui")
        self.assertEqual([event.seq for event in resumed.poll()], [3, 4, 5])
        resumed.ack()
        reopened = EventBus.open_existing(self.bus.path)
        reopened.publish("test", number=5)
        self.assertEqual([event.seq for event in reopened.subscribe(name="ui").poll()], [6])

    def test_replay_to_live_handoff_has_no_gap_and_failure_can_retry(self):
        self.bus.publish("before")
        inside, release = threading.Event(), threading.Event()
        seen = []
        def receive(event):
            if event.seq == 1:
                inside.set()
                self.assertTrue(release.wait(5))
            seen.append(event.seq)
        subscribing = threading.Thread(target=lambda: self.bus.subscribe(receive))
        subscribing.start()
        self.assertTrue(inside.wait(5))
        self.bus.publish("during")
        release.set()
        subscribing.join(5)
        self.bus.publish("after")
        self.assertEqual(seen, [1, 2, 3])
        failed = []
        def broken(event):
            if not failed:
                failed.append(event.seq)
                raise ValueError("重试")
            failed.append(event.seq)
        subscription = self.bus.subscribe(broken, after=3)
        self.bus.publish("retry")
        self.bus.publish("next")
        self.assertEqual(failed, [4, 4, 5])
        self.assertEqual(subscription.cursor, 5)
        self.assertTrue(self.bus.errors)

    def test_cross_process_wait_and_filtered_cursor(self):
        consumer = self.bus.subscribe(kinds=("other.*",), name="filtered")
        self.bus.publish("unrelated")
        self.assertEqual(consumer.poll(), [])
        consumer.ack()
        self.assertEqual(consumer.cursor, 1)
        process = multiprocessing.get_context("spawn").Process(target=_publish_in_process, args=(str(self.bus.path),))
        process.start()
        try:
            batch = consumer.poll(timeout=10)
            self.assertEqual([event.kind for event in batch], ["other.process"])
        finally:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)

    def test_identity_and_invalid_cursor_are_rejected(self):
        with self.assertRaises(ValueError):
            EventBus(self.bus.path, "other-stream")
        with self.assertRaises(ValueError):
            self.bus.subscribe(after=1)
        with self.assertRaises(FileNotFoundError):
            EventBus.open_existing(self.root / "missing.sqlite3")

    def test_tool_success_invalid_exception_and_timeout_have_one_terminal(self):
        ctx, registry = self.context(), ToolRegistry([_EventTool()])
        self.assertTrue(registry.execute("event_test", {"mode": "ok"}, ctx).ok)
        self.assertFalse(registry.execute("event_test", {}, ctx).ok)
        self.assertFalse(registry.execute("missing", {}, ctx).ok)
        self.assertFalse(registry.execute("event_test", {"mode": "crash"}, ctx).ok)
        self.assertFalse(registry.execute("event_test", {"mode": "late"}, ctx, timeout=.03).ok)
        time.sleep(.2)
        events = self.bus.replay()
        calls = {}
        for event in events:
            if event.kind.startswith("tool."):
                calls.setdefault(event.call_id, []).append(event)
        self.assertEqual(len(calls), 5)
        for call in calls.values():
            self.assertEqual(call[0].kind, "tool.requested")
            self.assertEqual(call[-1].kind, "tool.finished")
            self.assertEqual(sum(event.kind == "tool.finished" for event in call), 1)
        self.assertEqual([event.payload["status"] for event in events if event.kind == "tool.finished"],
                         ["completed", "invalid", "invalid", "failed", "timed_out"])
        self.assertNotIn("LATE", json.dumps([event.to_dict() for event in events]))
        self.assertTrue(ctx.audit.verify()[0])

    def test_external_stdout_and_stderr_arrive_before_process_completion(self):
        from trina.tools.workspace import TerminalExec
        ctx = self.context()
        live, done, errors = threading.Event(), threading.Event(), []
        self.bus.subscribe(lambda event: live.set() if event.kind == "tool.output" and "FIRST" in event.payload.get("delta", "") else None)
        def execute():
            try:
                result = ToolRegistry([TerminalExec()]).execute("terminal_exec", {
                    "argv": [sys.executable, "-u", "-c", "import time,sys; print('FIRST'); time.sleep(.4); print('ERR', file=sys.stderr); print('LAST')"],
                    "timeout": 5, "cwd": "run"}, ctx)
                self.assertTrue(result.ok, result.error)
                self.assertIn("LAST", result.data["stdout"])
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(live.wait(5))
            self.assertFalse(done.is_set(), "首个 stdout 必须在命令结束前可见")
        finally:
            thread.join(10)
        self.assertFalse(errors, errors)
        outputs = [event for event in self.bus.replay() if event.kind == "tool.output"]
        self.assertTrue(any(event.payload.get("stream") == "stderr" for event in outputs))

    def test_recovery_marks_open_call_interrupted_without_reexecuting(self):
        self.bus.publish("tool.requested", call_id="lost", tool="terminal_exec", params={"argv": ["never-run"]})
        self.bus.publish("job.started", call_id="lost-job", tool="terminal_start", params={"argv": ["never-run"]})
        reopened = EventBus.open_existing(self.bus.path)
        reopened.interrupt_open_calls()
        reopened.interrupt_open_calls()
        finished = reopened.replay(kinds=("tool.finished",))
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].payload["status"], "interrupted")
        jobs = reopened.replay(kinds=("job.finished",))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].payload["status"], "interrupted")

    def test_real_command_timeout_is_terminal_and_background_close_finishes_stream(self):
        from trina.tools.workspace import TerminalExec
        from trina.tools.terminal import TerminalStart
        ctx = self.context()
        registry = ToolRegistry([TerminalExec(), TerminalStart()])
        result = registry.execute("terminal_exec", {
            "argv": [sys.executable, "-u", "-c", "import time; print('BEFORE'); time.sleep(10)"],
            "timeout": .2, "cwd": "run"}, ctx, timeout=5)
        self.assertFalse(result.ok)
        terminal = self.bus.replay(kinds=("tool.finished",))[-1]
        self.assertEqual(terminal.payload["status"], "timed_out")
        self.assertIn("BEFORE", result.data["stdout"])
        jobs = ctx.extra["background_jobs"]
        self.addCleanup(jobs.close)
        started = registry.execute("terminal_start", {
            "argv": [sys.executable, "-u", "-c", "import time; print('STARTED'); time.sleep(10)"],
            "timeout": 10}, ctx)
        self.assertTrue(started.ok, started.error)
        jobs.close()
        high_water = self.bus.last_seq
        time.sleep(.05)
        self.assertEqual(self.bus.last_seq, high_water)
        events = self.bus.replay(kinds=("job.*",))
        self.assertEqual(events[0].kind, "job.started")
        self.assertEqual(events[-1].kind, "job.finished")
        self.assertEqual(events[-1].payload["status"], "cancelled")
        self.assertEqual(sum(event.kind == "job.finished" for event in events), 1)

    def test_task_native_prose_and_final_are_visible_with_paired_model_events(self):
        from trina.brain.agent import PentestAgent
        from trina.config import LLMConfig
        from trina.llm import LLMResponse
        from trina.llm.base import ToolCall
        from trina.llm.replay_backend import ReplayBackend
        backend = ReplayBackend(LLMConfig(backend="replay"))
        backend.supports_native_tools = True
        replies = [
            LLMResponse(text="先检查服务。", tool_calls=[ToolCall("c1", "event_test", '{"mode":"ok"}')]),
            LLMResponse(tool_calls=[ToolCall("c2", "finish", '{"summary":"检查完成。"}')]),
        ]
        backend.complete_messages = lambda *_args, **_kwargs: replies.pop(0)
        agent = PentestAgent(self.context().config, backend=backend, registry=ToolRegistry([_EventTool()]), event_bus=self.bus)
        agent.run("127.0.0.1", max_steps=3, run_id="native-test")
        events = self.bus.replay()
        self.assertEqual([event.payload["text"] for event in events if event.kind == "assistant.message"],
                         ["先检查服务。", "检查完成。"])
        starts = [event.payload["request_id"] for event in events if event.kind == "model.started"]
        finishes = [event.payload["request_id"] for event in events if event.kind == "model.finished"]
        self.assertEqual(starts, finishes)
        self.assertEqual(len(starts), 2)

    def test_model_error_projects_immediately_and_replays_once_without_losing_draft(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        from trina.events import ModelEventStream
        from trina.llm import LLMResponse, LLMStreamEvent
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True, dynamic_values=lambda *_: (), plain=True)
            editor.buffer.text = "保留我的补充"
            view = EventTranscript(editor.write, editor=editor)
            self.bus.subscribe(view.consume)
            stream = ModelEventStream(self.bus.publish)
            stream.on_event(LLMStreamEvent("error", delta="HTTP 403: forbidden"))
            self.assertIn("HTTP 403", "".join(editor._chunks))
            self.assertEqual(self.bus.replay()[-1].kind, "model.error")
            stream.finish(LLMResponse(ok=False, error="HTTP 403: forbidden"))
            self.assertEqual("".join(editor._chunks).count("HTTP 403"), 1)
            self.assertEqual(editor.buffer.text, "保留我的补充")
            live = "".join(editor._chunks)
            editor.clear_transcript()
            replay = EventTranscript(editor.write, editor=editor)
            for event in self.bus.replay():
                replay.consume(event)
            self.assertEqual("".join(editor._chunks), live)

    def test_cli_consumer_replays_then_resumes_without_executing_tools(self):
        from trina.cli import build_parser
        self.bus.publish("tool.requested", call_id="one", tool="never-execute")
        parser = build_parser()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            args = parser.parse_args(["events", str(self.bus.path), "--json", "--consumer", "cli"])
            self.assertEqual(args.func(args), 0)
            self.assertEqual(args.func(args), 0)
            self.bus.publish("test.next")
            self.assertEqual(args.func(args), 0)
        self.assertEqual([json.loads(line)["seq"] for line in stream.getvalue().splitlines()], [1, 2])

    def test_cli_run_uses_tool_windows_and_plain_final_text(self):
        from trina.cli import build_parser
        from trina.config import LLMConfig
        from trina.llm import LLMResponse
        from trina.llm.replay_backend import ReplayBackend
        backend = ReplayBackend(LLMConfig(backend="replay"))
        responses = [LLMResponse(text=json.dumps({"action": "event_test", "params": {"mode": "ok"}})),
                     LLMResponse(text=json.dumps({"final": {"summary": "服务检查完成。"}}))]
        backend.complete_messages = lambda *_args, **_kwargs: responses.pop(0)
        args = build_parser().parse_args(["run", "--target", "127.0.0.1", "--max-steps", "2", "--no-report"])
        stream = io.StringIO()
        with patch("trina.cli.build_config", return_value=self.context().config), \
             patch("trina.llm.make_backend", return_value=backend), \
             patch("trina.tools.build_registry", return_value=ToolRegistry([_EventTool()])), \
             contextlib.redirect_stdout(stream):
            self.assertEqual(args.func(args), 0)
        output = stream.getvalue()
        self.assertIn("event_test · 完成", output)
        self.assertIn("Output", output)
        self.assertIn("原始输出", output)
        self.assertIn("服务检查完成。", output)
        self.assertNotIn("[Trina] 输出", output)
        self.assertNotIn("[1/2]", output)

    def test_tool_window_updates_in_place_and_replay_matches_live(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True, dynamic_values=lambda *_: (), plain=True)
            editor.buffer.text = "尚未发送"
            view = EventTranscript(editor.write, editor=editor)
            self.bus.subscribe(view.consume)
            self.bus.publish("assistant.message", text="先检查输入。", final=True, message_id="m1")
            self.bus.publish("tool.requested", call_id="one", tool="terminal_exec", params={"argv": ["python", "-V"]})
            self.bus.publish("tool.started", call_id="one", tool="terminal_exec")
            self.bus.publish("tool.output", call_id="one", stream="stdout", delta="Python ")
            self.bus.publish("tool.output", call_id="one", stream="stdout", delta="3.11")
            self.bus.publish("tool.finished", call_id="one", tool="terminal_exec", status="completed", ok=True,
                             duration=.2, data={"stdout": "Python 3.11", "exit_code": 0})
            self.bus.publish("assistant.message", text="检查完成。", final=True, message_id="m2")
            self.assertEqual(len(editor._chunks), 3)
            self.assertEqual(editor.buffer.text, "尚未发送")
            live = "".join(editor._chunks)
            self.assertEqual(live.count("terminal_exec"), 1)
            self.assertIn("Output", live)
            self.assertIn("Python 3.11", live)
            self.assertNotIn("[Trina] 输出", live)
            editor.clear_transcript()
            replay = EventTranscript(editor.write, editor=editor)
            for event in self.bus.replay():
                replay.consume(event)
                replay.consume(event)  # 续流重复投递按 stream/seq 去重。
            self.assertEqual("".join(editor._chunks), live)

    def test_commander_consumes_live_facts_before_worker_terminal(self):
        from trina.swarm import SwarmRuntime, TaskSpec
        config = self.context().config
        produced, release = threading.Event(), threading.Event()
        runtime = None
        errors = []
        def execute(worker, task, run_id, hints, registry):
            ctx = self.context(run_id, bus=runtime.event_bus)
            ctx.extra.update(intent_id=task.id, worker_id=worker.id)
            ToolRegistry([_EventTool()]).execute("event_test", {"mode": "ok"}, ctx)
            produced.set()
            self.assertTrue(release.wait(10))
            ctx.state.status = "success"
            return ctx.state
        runtime = SwarmRuntime(config, [TaskSpec("main", "127.0.0.1", "识别服务")], max_workers=1, executor=execute)
        events = runtime.event_bus.subscribe(kinds=("coordination.facts.published",))
        def run():
            try:
                runtime.run()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(produced.wait(5))
            self.assertTrue(events.poll(timeout=5))
            self.assertEqual(runtime.records["main"].status, "running")
            self.assertTrue(runtime.coordination.facts)
            self.assertTrue(any(row["kind"] == "tool" for row in runtime.coordination.traces))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(errors, errors)
        self.assertEqual(runtime.records["main"].status, "completed")

    def test_commander_recovers_durable_checkpoint_ahead_of_json_file(self):
        from trina.swarm import SwarmRuntime, TaskSpec
        config = self.context().config
        runtime = SwarmRuntime(config, [TaskSpec("a", "127.0.0.1", "识别服务")], max_workers=1)
        runtime._save()
        old = runtime.checkpoint.read_text(encoding="utf-8")
        runtime.records["a"].result = {"password": "runtime-secret"}
        runtime.add_task(TaskSpec("b", "127.0.0.1", "验证服务", depends_on=("a",)))
        runtime.checkpoint.write_text(old, encoding="utf-8")
        restored = SwarmRuntime.resume(config, runtime.checkpoint)
        self.assertEqual({task.id for task in restored.tasks}, {"a", "b"})
        self.assertEqual(restored.coordination.todo["b"]["status"], "waiting")
        self.assertEqual(restored.records["a"].result["password"], "runtime-secret")
        self.assertNotIn("runtime-secret", json.dumps([event.to_dict() for event in runtime.event_bus.replay()]))

    def test_live_credential_identity_matches_final_state_and_public_events_are_redacted(self):
        from trina.swarm import SwarmRuntime, TaskSpec
        from trina.state import Credential
        runtime = SwarmRuntime(self.context().config, [TaskSpec("main", "127.0.0.1", "验证服务")], max_workers=1)
        record = runtime.records["main"]
        record.status, record.worker_id, record.attempt = "running", "worker-1", 1
        state = RunState(f"{runtime.run_id}-main-1", "127.0.0.1")
        state.credentials.append(Credential(service="http", target="127.0.0.1", username="user",
                                            password="runtime-secret", verified=True))
        runtime.event_bus.publish("state.updated", run_id=state.run_id, task_id="main", worker_id="worker-1", state=state.to_dict())
        runtime._consume_events()
        facts = [row for row in runtime.coordination.facts.values() if row["kind"] == "credential"]
        self.assertEqual(facts[0]["payload"]["password"], "runtime-secret")
        self.assertEqual(runtime.coordination.publish_state("main", state), [])
        self.assertNotIn("runtime-secret", json.dumps([event.to_dict() for event in runtime.event_bus.replay()]))

    def test_commander_drains_backlog_before_settling_a_worker(self):
        from trina.swarm import SwarmRuntime, TaskSpec
        runtime = SwarmRuntime(self.context().config, [TaskSpec("main", "127.0.0.1", "识别服务")], max_workers=1)
        record = runtime.records["main"]
        record.status, record.worker_id, record.attempt = "running", "worker-1", 1
        # 批次边界前有大量模型增量，工具终态不能留到任务完成后才消费。
        event = runtime.event_bus.publish("model.text_delta", delta="x")
        import sqlite3
        with sqlite3.connect(runtime.event_bus.path) as db:
            db.executemany("INSERT INTO events(body) VALUES (?)", [(json.dumps({
                key: value for key, value in event.to_dict().items() if key != "seq"}),)] * 1005)
        runtime.event_bus.publish("tool.finished", task_id="main", worker_id="worker-1",
                                  run_id=f"{runtime.run_id}-main-1", call_id="last", tool="event_test", ok=True,
                                  status="completed", summary="尾部观察")
        runtime._consume_events()
        self.assertEqual(runtime._commander_events.cursor, runtime.event_bus.last_seq)
        self.assertTrue(any(row.get("summary") == "尾部观察" for row in runtime.coordination.traces))
