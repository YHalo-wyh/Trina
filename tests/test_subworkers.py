"""子 Worker 的真实调度、模型隔离、取消回收、终端执行及 UI 回归。"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trina.config import ConfigError, TrinaConfig
from trina.console_state import ConsoleContext, ConsoleState, _cells
from trina.interactive import Console
from trina.llm.base import LLMUsage
from trina.subworkers import Subworkers
from trina.swarm import SwarmRuntime, TaskSpec, task_llm
from trina.state import RunState


class SubworkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"), report__output_dir=str(self.root / "reports"))

    def test_parallel_models_and_reasoning_are_bound_to_each_task_and_restored(self):
        barrier = threading.Barrier(2, timeout=5)
        observed = {}
        def execute(worker, task, run_id, hints, registry):
            if task.id in {"a", "b"}:
                barrier.wait()
            observed[task.id] = (worker.llm.model, worker.llm.reasoning_effort, id(worker.backend))
            return RunState(run_id=run_id, target=task.target, status="success")
        tasks = [TaskSpec("a", "localhost", "A", model="deepseek-chat", reasoning_effort="max"),
                 TaskSpec("b", "localhost", "B", model="gpt-5.2", reasoning_effort="high"),
                 TaskSpec("c", "localhost", "C", depends_on=("a", "b"), model="deepseek-chat", reasoning_effort="max")]
        runtime = SwarmRuntime(self.config, tasks, executor=execute, backend_factory=lambda llm: object())
        runtime.run()
        self.assertEqual(observed["a"][:2], ("deepseek-chat", "max"))
        self.assertEqual(observed["b"][:2], ("gpt-5.2", "high"))
        self.assertEqual(observed["a"][2], observed["c"][2])
        self.assertNotEqual(observed["a"][2], observed["b"][2])
        restored = SwarmRuntime.resume(self.config.with_overrides(llm__model="different"), runtime.checkpoint,
                                      executor=execute, backend_factory=lambda _: object())
        self.assertEqual(restored._task_llms["a"].reasoning_effort, "max")
        self.assertEqual(restored._task_llms["b"].model, "gpt-5.2")
        self.assertEqual(restored.run()["workers_created"], 0)

    def test_actual_api_requests_use_separate_models_effort_and_usage(self):
        requests = []
        barrier = threading.Barrier(2, timeout=5)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                barrier.wait()
                content = json.dumps({"final": {"summary": "fixture response"}})
                events = [{"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}}}]
                body = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = self.config.with_overrides(llm__base_url=f"http://127.0.0.1:{server.server_port}/v1", llm__api_key_env="TRINA_SUBWORKER_FIXTURE_KEY", llm__timeout=5)
        manager = Subworkers(lambda _: None)
        self.addCleanup(manager.close)
        with patch.dict(os.environ, {"TRINA_SUBWORKER_FIXTURE_KEY": "local-fixture"}):
            manager.start(config, [TaskSpec("a", "localhost", "A", model="deepseek-chat", reasoning_effort="max", max_steps=1),
                                   TaskSpec("b", "localhost", "B", model="gpt-5.2", reasoning_effort="high", max_steps=1)])
            manager.thread.join(15)
        self.assertFalse(manager.running)
        self.assertEqual({(p["model"], p["reasoning_effort"]) for p in requests}, {("deepseek-chat", "max"), ("gpt-5.2", "high")})
        self.assertTrue(all(isinstance(p["messages"], list) and p["messages"][0]["role"] == "system" for p in requests))
        self.assertEqual(manager.usage.usage.to_dict()["total_tokens"], 240)
        self.assertAlmostEqual(manager.usage.usage_data()["cache_hit_rate"], .8)
        self.assertTrue(all(row["tokens"] == 120 for row in manager.rows()))

    def test_task_provider_overrides_defaults_without_writing_profile_store(self):
        from trina.providers import ProviderProfile, ProfileStore
        store = ProfileStore(self.root / "providers.json")
        store.profiles["local"] = ProviderProfile("local", "custom", "https://example.test/v1", "gpt-5.2")
        config = self.config.with_overrides(subworker__model="deepseek-chat", subworker__reasoning_effort="max")
        with patch("trina.providers.ProfileStore", return_value=store):
            llm = task_llm(config, TaskSpec("one", "localhost", "A", profile="local", reasoning_effort="high"))
        self.assertEqual((llm.model, llm.reasoning_effort), ("gpt-5.2", "high"))
        self.assertFalse(store.path.exists())
        self.assertEqual(store.profiles["local"].reasoning_effort, "default")

    def test_cross_thread_submission_configure_dedup_and_cancellation(self):
        entered = threading.Event()
        def execute(worker, task, run_id, hints, registry):
            entered.set()
            runtime.stop_event.wait(5)
            return RunState(run_id=run_id, target=task.target, status="stopped")
        runtime = SwarmRuntime(self.config, [TaskSpec("base", "localhost", "base")], max_workers=1,
                               executor=execute, backend_factory=lambda _: object())
        thread = threading.Thread(target=runtime.run)
        thread.start()
        self.addCleanup(lambda: (runtime.stop_event.set(), thread.join(6)))
        self.assertTrue(entered.wait(5))
        self.assertTrue(runtime.submit_task(TaskSpec("queued", "localhost", "queued")).result(5))
        self.assertFalse(runtime.submit_task(TaskSpec("dup", "localhost", "queued")).result(5))
        self.assertTrue(runtime.configure_task("queued", model="gpt-5.2", reasoning_effort="high").result(5))
        self.assertEqual(runtime._task_llms["queued"].model, "gpt-5.2")
        with self.assertRaisesRegex(ValueError, "只可调整"):
            runtime.configure_task("base", model="gpt-5.2").result(5)
        with self.assertRaisesRegex(ValueError, "不支持"):
            runtime.configure_task("queued", reasoning_effort="max").result(5)
        self.assertEqual(runtime._task_llms["queued"].reasoning_effort, "high")
        runtime.stop_event.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual({r.status for r in runtime.records.values()}, {"cancelled"})
        self.assertTrue(all(not w.busy and not w.capability_lease_id for w in runtime.pool.workers))
        self.assertEqual(runtime.coordination.todo["queued"]["status"], "cancelled")
        with self.assertRaisesRegex(RuntimeError, "收尾"):
            runtime.submit_task(TaskSpec("late", "localhost", "late")).result()
        self.assertTrue(runtime.audit.verify()[0])

    def test_timeout_and_backend_error_do_not_complete_or_deadlock_dependents(self):
        def factory(llm):
            if llm.model == "bad":
                raise RuntimeError("backend unavailable")
            return object()
        runtime = SwarmRuntime(self.config, [TaskSpec("timeout", "localhost", "timeout"),
            TaskSpec("after", "localhost", "after", depends_on=("timeout",)), TaskSpec("bad", "localhost", "bad", model="bad")],
            executor=lambda w, t, run, h, r: RunState(run_id=run, target=t.target, status="timed_out"), backend_factory=factory)
        result = runtime.run()["tasks"]
        self.assertEqual(result["timeout"]["status"], "timed_out")
        self.assertEqual(result["after"]["status"], "skipped")
        self.assertEqual(result["bad"]["status"], "failed")

    def test_real_replay_workers_execute_in_independent_directories(self):
        # 模型用离线回放；实际 Python 进程、文件写入、工具审计和报告都走真实链。
        script = self.root / "replay.json"
        code = "from pathlib import Path; Path('proof.txt').write_text('executed'); print('executed')"
        script.write_text(json.dumps({"steps": [
            {"response": json.dumps({"action": "terminal_exec", "params": {"argv": [sys.executable, "-c", code], "cwd": "run"}})},
            {"response": json.dumps({"final": {"summary": "真实执行完毕"}})},
        ]}), encoding="utf-8")
        config = self.config.with_overrides(llm__backend="replay", llm__replay_script=str(script))
        messages = []
        manager = Subworkers(messages.append)
        self.addCleanup(manager.close)
        manager.start(config, [TaskSpec("one", "localhost", "执行一", max_steps=2), TaskSpec("two", "localhost", "执行二", max_steps=2)])
        manager.thread.join(20)
        self.assertFalse(manager.running)
        snapshot = manager.snapshot()
        self.assertEqual(len(snapshot["tasks"]), 2)
        directories = []
        for item in snapshot["tasks"].values():
            # 工具确实成功，但文件写入不能冒充渗透任务权威成功。
            self.assertEqual(item["result"]["verdict"], "NOT_CONFIRMED")
            self.assertFalse(item.get("error"))
            directory = Path(item["result"]["artifacts"]["workspace"])
            directories.append(directory)
            self.assertEqual((directory / "proof.txt").read_text(), "executed")
        self.assertNotEqual(*directories)
        self.assertTrue(all(row["calls"] == 2 and row["tools"] == 1 for row in manager.rows()))
        self.assertEqual(manager.usage.usage.calls, 4)
        self.assertTrue(messages)

    def test_console_defaults_do_not_mutate_parent_and_configuration_validates(self):
        output = []
        console = Console(self.config, output=output.append)
        self.addCleanup(console._cleanup)
        original = self.config.llm.to_dict() if hasattr(self.config.llm, "to_dict") else vars(self.config.llm).copy()
        console.handle("/subworker model gpt-5.2")
        console.handle("/subworker thinking high")
        console.handle("/subworker settings workers 3")
        self.assertEqual(self.config.subworker.workers, 3)
        self.assertEqual(task_llm(self.config).reasoning_effort, "high")
        self.assertEqual(vars(self.config.llm), original)
        console.handle("/subworker thinking max")
        self.assertEqual(self.config.subworker.reasoning_effort, "high")
        self.assertIn("不支持", output[-1])
        with self.assertRaises(ConfigError):
            self.config.with_overrides(subworker__workers=0)
        restored = TrinaConfig._from_mapping(self.config.to_dict())
        self.assertEqual(restored.subworker.model, "gpt-5.2")

    def test_footer_metrics_share_one_row_and_survive_resize(self):
        view = ConsoleState()
        view.model_finished(LLMUsage(input_tokens=10000, output_tokens=1000, cache_read_tokens=8000))
        context = ConsoleContext("运行中", "DeepSeek Flash Max", "C:/项目", "目标", "轮次 8", 10000, 500000,
                                 metrics=view.metrics(), workers="2 运行 / 1 等待")
        for width in range(1, 201):
            text = "".join(value for _, value in context.footer(width))
            self.assertLessEqual(_cells(text), width)
            self.assertNotIn("\n", text)
            self.assertLessEqual(sum(_cells(v) for _, v in context.rail(width)), width)
            if width >= 88:
                self.assertLess(text.index("轮次"), text.index("缓存"))
        self.assertEqual(ConsoleState().metrics(), ())

    def test_subworker_panel_keyboard_targets_selected_worker(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output
        from trina.subworker_ui import show_subworkers
        manager = Subworkers(lambda _: None)
        manager._rows = {"binary": {"id": "binary", "goal": "分析 ELF", "status": "pending", "model": "deepseek-chat", "reasoning_effort": "high"}}
        output = Vt100_Output(io.StringIO(), lambda: Size(rows=22, columns=100), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            pipe.send_text("t")
            self.assertEqual(show_subworkers(manager, self.config), ("thinking", "binary"))
        from trina.subworker_ui import render_workers
        for width in (1, 20, 40, 88, 144):
            text = "".join(t for _, t in render_workers(manager.rows(), width, 0))
            self.assertTrue(all(_cells(line) <= width for line in text.splitlines()))
