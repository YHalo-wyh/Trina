"""RASC 首版的调度、隔离与恢复测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from trina.config import TrinaConfig
from trina.state import RunState
from trina.swarm import SwarmRuntime, TaskSpec, WorkerPool, default_tasks, validate_tasks


class SwarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = TrinaConfig.load().with_overrides(
            run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"),
            report__output_dir=str(root / "reports"),
        )

    def test_dag_validation_and_dedup(self) -> None:
        tasks = default_tasks("127.0.0.1", "验证目标")
        validate_tasks(tasks)
        self.assertEqual([task.id for task in tasks], ["main"])
        self.assertEqual(tasks[0].capabilities, ())
        with self.assertRaisesRegex(ValueError, "环"):
            validate_tasks([
                TaskSpec("a", "127.0.0.1", "A", depends_on=("b",)),
                TaskSpec("b", "127.0.0.1", "B", depends_on=("a",)),
            ])
        with self.assertRaisesRegex(ValueError, "等价"):
            validate_tasks([TaskSpec("a", "127.0.0.1", "A"), TaskSpec("b", "127.0.0.1", "a")])

    def test_add_task_validates_and_persists(self) -> None:
        runtime = SwarmRuntime(
            self.config,
            [TaskSpec("base", "127.0.0.1", "识别服务")],
            max_workers=1,
            executor=lambda *args: None,
        )
        self.assertFalse(runtime.add_task(TaskSpec("duplicate", "127.0.0.1", " 识别服务 ")))
        with self.assertRaisesRegex(ValueError, "无效依赖"):
            runtime.add_task(TaskSpec("bad", "127.0.0.1", "错误依赖", depends_on=("missing",)))
        self.assertTrue(runtime.add_task(TaskSpec("verify", "127.0.0.1", "验证服务", depends_on=("base",))))
        checkpoint = json.loads(runtime.checkpoint.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in checkpoint["tasks"]], ["base", "verify"])
        self.assertEqual(checkpoint["records"]["verify"]["status"], "pending")

    def test_parallel_leases_reuse_and_handoff(self) -> None:
        active = 0
        maximum = 0
        lock = threading.Lock()
        first_branch = threading.Barrier(2, timeout=5)
        observed: list[tuple[str, tuple[str, ...], list[str], str]] = []

        def execute(worker, task, run_id, hints, registry):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            if task.id in {"recon", "web"}:
                first_branch.wait()
            with lock:
                active -= 1
            observed.append((task.id, tuple(registry.names()), hints, worker.id))
            state = RunState(run_id=run_id, target=task.target, goal=task.goal, status="success")
            state.host(task.target).technologies.append(task.id)
            state.ended_at = time.time()
            return state

        tasks = [
            TaskSpec("recon", "127.0.0.1", "识别服务", ("recon",), priority=20),
            TaskSpec("web", "127.0.0.1", "梳理 Web 面", ("web",), priority=10),
            TaskSpec("verify", "127.0.0.1", "验证目标", (), ("recon", "web")),
        ]
        runtime = SwarmRuntime(self.config, tasks, max_workers=2, executor=execute)
        summary = runtime.run()
        self.assertEqual(maximum, 2)
        self.assertEqual({item["status"] for item in summary["tasks"].values()}, {"completed"})
        self.assertEqual(summary["workers_created"], 2)
        self.assertEqual(summary["worker_reuses"], 1)
        verify = next(item for item in observed if item[0] == "verify")
        self.assertEqual(len(verify[2]), 2)
        briefs = [json.loads(item) for item in verify[2]]
        self.assertEqual({item["source_task"] for item in briefs}, {"recon", "web"})
        self.assertTrue(all(item["similarity"] >= 0.6 for item in briefs))
        self.assertTrue(all("flags" not in item["intelligence"] for item in briefs))
        self.assertEqual(
            summary["tasks"]["verify"]["result"]["facts"]["hosts"][0]["technologies"],
            ["verify"],
            "当前题目的事实图不能吸收前序 Worker 状态",
        )
        recon = next(item for item in observed if item[0] == "recon")
        self.assertNotIn("sqli_test", recon[1])
        self.assertTrue(runtime.checkpoint.exists())
        self.assertTrue(all(Path(path).exists() for path in summary["report_paths"]))
        self.assertTrue(all(not worker.busy for worker in runtime.pool.workers))
        self.assertTrue(all(not worker.capability_lease_id for worker in runtime.pool.workers))
        self.assertTrue(all(summary["tasks"][task]["capabilities"] for task in ("recon", "web", "verify")))
        self.assertTrue(summary["tasks"]["verify"]["evidence_refs"])
        self.assertTrue(runtime.audit.verify()[0])

    def test_commander_does_not_forward_unrelated_completed_task(self) -> None:
        observed: dict[str, list[str]] = {}

        def execute(worker, task, run_id, hints, registry):
            observed[task.id] = list(hints)
            state = RunState(run_id=run_id, target=task.target, goal=task.goal, status="success")
            state.host(task.target).technologies.append(task.id)
            state.ended_at = time.time()
            return state

        tasks = [
            TaskSpec("first", "http://alpha.test", "梳理登录接口", priority=20),
            TaskSpec("second", "http://beta.test", "分析二进制加密算法", priority=10),
        ]
        runtime = SwarmRuntime(self.config, tasks, max_workers=1, executor=execute)
        runtime.run()
        self.assertEqual(observed["first"], [])
        self.assertEqual(observed["second"], [])

    def test_resume_retries_only_inflight(self) -> None:
        calls: list[str] = []

        def execute(worker, task, run_id, hints, registry):
            calls.append(task.id)
            state = RunState(run_id=run_id, target=task.target, status="success")
            state.ended_at = time.time()
            return state

        tasks = [TaskSpec("a", "127.0.0.1", "A"), TaskSpec("b", "127.0.0.1", "B", depends_on=("a",))]
        runtime = SwarmRuntime(self.config, tasks, max_workers=1, executor=execute)
        runtime.records["a"].status = "completed"
        runtime.records["a"].attempt = 1
        runtime.records["b"].status = "running"
        runtime.records["b"].attempt = 1
        runtime._save()
        resumed = SwarmRuntime.resume(self.config, runtime.checkpoint, executor=execute)
        summary = resumed.run()
        self.assertEqual(calls, ["b"])
        self.assertEqual(summary["tasks"]["b"]["attempt"], 2)
        self.assertEqual(summary["tasks"]["a"]["attempt"], 1)

    def test_scope_preflight_preserved_in_real_agent(self) -> None:
        script = Path(self.temp.name) / "replay.json"
        script.write_text(json.dumps({"default": '{"final": {"summary": "done"}}'}), encoding="utf-8")
        config = self.config.with_overrides(
            llm__backend="replay", llm__replay_script=str(script), scope__trust_mode="restricted",
        )
        runtime = SwarmRuntime(config, [TaskSpec("blocked", "8.8.8.8", "检查目标", max_steps=1)], max_workers=1)
        summary = runtime.run()
        self.assertEqual(summary["tasks"]["blocked"]["status"], "blocked")
        self.assertEqual(summary["tasks"]["blocked"]["result"]["status"], "blocked")
        self.assertTrue(Path(summary["tasks"]["blocked"]["result"]["artifacts"]["audit_path"]).exists())

    def test_replay_agent_runs_registered_http_tool_and_writes_report(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><title>Trina fixture</title></html>")

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        target = f"http://127.0.0.1:{server.server_port}/"
        script = Path(self.temp.name) / "http_replay.json"
        script.write_text(json.dumps({"steps": [
            item for _ in range(2) for item in (
                {"response": json.dumps({"thought": "检查首页", "action": "http_fingerprint", "params": {"url": target}})},
                {"response": json.dumps({"thought": "完成", "final": {"summary": "首页已检查"}})},
            )
        ]}), encoding="utf-8")
        config = self.config.with_overrides(llm__backend="replay", llm__replay_script=str(script))
        runtime = SwarmRuntime(config, [
            TaskSpec("http", target, "检查首页", ("recon",), max_steps=2),
            TaskSpec("http_again", target, "复核首页", ("recon",), depends_on=("http",), max_steps=2),
        ], max_workers=1)
        summary = runtime.run()
        self.assertEqual(summary["tasks"]["http"]["status"], "completed", summary["tasks"]["http"])
        self.assertEqual(summary["tasks"]["http_again"]["status"], "completed", summary["tasks"]["http_again"])
        self.assertGreaterEqual(summary["tasks"]["http"]["result"]["counters"]["tool_calls"], 1)
        self.assertEqual(summary["tasks"]["http"]["result"]["ledger"]["calls"], 2)
        self.assertEqual(summary["tasks"]["http_again"]["result"]["ledger"]["calls"], 2)
        self.assertEqual(summary["worker_reuses"], 1)
        self.assertTrue(all(Path(path).exists() for path in summary["report_paths"]))

    def test_worker_lease_rejects_stale_release(self) -> None:
        pool = WorkerPool(self.config, 1, backend_factory=lambda _: object())
        worker = pool.acquire()
        self.assertIsNotNone(worker)
        assert worker is not None
        with self.assertRaisesRegex(RuntimeError, "租约"):
            pool.release(worker, "stale")
        pool.release(worker, worker.lease_id)


if __name__ == "__main__":
    unittest.main()
