"""模型同轮独立读取的真实并发、顺序归并与回归合同。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.brain.chat import ChatAgent
from trina.brain.agent import PentestAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMResponse
from trina.llm.base import ToolCall
from trina.llm.replay_backend import ReplayBackend
from trina.tools import build_registry
from trina.tools.workspace import WorkspaceRead, WorkspaceWrite


class ParallelToolCallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = TrinaConfig.load().with_overrides(
            run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"),
            policy__mode="guided",
            llm__tool_protocol="native",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_batch(self, count: int, *, parallelism: int = 4, dependencies: dict[int, list[int]] | None = None,
                  missing: set[int] | None = None, delay: float = .08, synchronize: bool = False) -> dict:
        dependencies, missing = dependencies or {}, missing or set()
        config = self.config.with_overrides(llm__tool_parallelism=parallelism)
        backend = ReplayBackend(LLMConfig(backend="replay"))
        calls = []
        for index in range(1, count + 1):
            params: dict[str, object] = {"path": f"source-{index}.py", "root": "run"}
            if index in dependencies:
                params["_depends_on"] = dependencies[index]
            calls.append(ToolCall(f"read-{index}", "read", json.dumps(params)))
        replies = [
            LLMResponse(tool_calls=calls),
            LLMResponse(tool_calls=[ToolCall("finish", "finish", '{"summary":"done"}')]),
        ]
        backend.complete_messages = lambda *_args, **_kwargs: replies.pop(0)  # type: ignore[method-assign]
        chat = ChatAgent(config, registry=build_registry(["workspace"]), backend=backend)
        for index in range(1, count + 1):
            if index not in missing:
                (chat.workspace / f"source-{index}.py").write_text(f"value = {index}\n", encoding="utf-8")

        lock = threading.Lock()
        active = 0
        maximum = 0
        starts: dict[int, float] = {}
        finishes: dict[int, float] = {}
        original = WorkspaceRead.run
        barrier = threading.Barrier(count, timeout=3) if synchronize else None

        def delayed(tool, ctx, path, start_line=1, line_count=240, root="project"):
            nonlocal active, maximum
            index = int(Path(path).stem.split("-")[-1])
            with lock:
                active += 1
                maximum = max(maximum, active)
                starts[index] = time.monotonic()
            try:
                if barrier is not None:
                    barrier.wait()
                time.sleep(delay)
                return original(tool, ctx, path, start_line, line_count, root)
            finally:
                with lock:
                    finishes[index] = time.monotonic()
                    active -= 1

        try:
            started = time.monotonic()
            with patch.object(WorkspaceRead, "run", delayed):
                reply = chat.ask("读取这些项目代码文件并分析")
            elapsed = time.monotonic() - started
            tool_rows = [row for row in chat._messages if row.get("role") == "tool"]
            events = chat.ctx.events.replay(kinds=("tool.*",))
            return {
                "reply": reply,
                "elapsed": elapsed,
                "maximum": maximum,
                "starts": starts,
                "finishes": finishes,
                "tool_rows": tool_rows,
                "events": events,
                "tool_calls": chat.state.counters.get("tool_calls", 0),
            }
        finally:
            chat.close()

    def test_two_independent_reads_overlap(self):
        result = self.run_batch(2)
        self.assertEqual(result["maximum"], 2)
        self.assertLess(result["starts"][2], result["finishes"][1])

    def test_four_independent_reads_overlap(self):
        result = self.run_batch(4, delay=.05, synchronize=True)
        self.assertEqual(result["maximum"], 4)

    def test_eight_reads_respect_configured_limit(self):
        result = self.run_batch(8, parallelism=3, delay=.2)
        self.assertGreaterEqual(result["maximum"], 2)
        self.assertLessEqual(result["maximum"], 3)
        self.assertEqual(result["tool_calls"], 8)

    def test_declared_dependency_does_not_start_early(self):
        result = self.run_batch(2, dependencies={2: [1]})
        self.assertEqual(result["maximum"], 1)
        self.assertGreaterEqual(result["starts"][2], result["finishes"][1])

    def test_failure_isolated_and_call_ids_stay_in_model_order(self):
        result = self.run_batch(3, missing={2})
        ids = [row["tool_call_id"] for row in result["tool_rows"]]
        self.assertEqual(ids, ["read-1", "read-2", "read-3", "finish"])
        self.assertIn("文件不存在", result["tool_rows"][1]["content"])
        self.assertIn("value = 3", result["tool_rows"][2]["content"])

    def test_single_read_keeps_original_path_and_receipt_pair(self):
        result = self.run_batch(1)
        self.assertEqual(result["maximum"], 1)
        self.assertEqual(result["tool_calls"], 1)
        kinds = [event.kind for event in result["events"]]
        self.assertEqual(kinds.count("tool.requested"), 1)
        self.assertEqual(kinds.count("tool.finished"), 1)

    def test_parallel_state_merge_keeps_all_receipts(self):
        result = self.run_batch(4)
        finished = [event for event in result["events"] if event.kind == "tool.finished"]
        self.assertEqual(len(finished), 4)
        self.assertEqual([event.call_id for event in finished], [f"read-{index}" for index in range(1, 5)])
        self.assertTrue(all(event.payload.get("status") == "completed" for event in finished))
        self.assertEqual(result["tool_calls"], 4)

    def test_overlapping_writes_remain_serial_and_keep_order(self):
        config = self.config.with_overrides(llm__tool_parallelism=4)
        backend = ReplayBackend(LLMConfig(backend="replay"))
        replies = [
            LLMResponse(tool_calls=[
                ToolCall("write-1", "write", '{"path":"shared.txt","content":"first","root":"run"}'),
                ToolCall("write-2", "write", '{"path":"shared.txt","content":"second","root":"run"}'),
            ]),
            LLMResponse(tool_calls=[ToolCall("finish", "finish", '{"summary":"done"}')]),
        ]
        backend.complete_messages = lambda *_args, **_kwargs: replies.pop(0)  # type: ignore[method-assign]
        chat = ChatAgent(config, registry=build_registry(["workspace"]), backend=backend)
        lock = threading.Lock()
        active = 0
        maximum = 0
        original = WorkspaceWrite.run

        def delayed(tool, ctx, path, content, root="project"):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(.05)
                return original(tool, ctx, path, content, root)
            finally:
                with lock:
                    active -= 1

        try:
            with patch.object(WorkspaceWrite, "run", delayed):
                self.assertEqual(chat.ask("依次修改项目代码文件"), "done")
            self.assertEqual(maximum, 1)
            self.assertEqual((chat.workspace / "shared.txt").read_text(encoding="utf-8"), "second")
            ids = [row["tool_call_id"] for row in chat._messages if row.get("role") == "tool"]
            self.assertEqual(ids, ["write-1", "write-2", "finish"])
        finally:
            chat.close()

    def test_measured_parallel_speedup_against_serial(self):
        serial = self.run_batch(4, parallelism=1, delay=.25)
        parallel = self.run_batch(4, parallelism=4, delay=.25)
        self.assertLess(parallel["elapsed"], serial["elapsed"] * .8)

    def test_task_agent_real_path_uses_the_same_parallel_executor(self):
        config = self.config.with_overrides(llm__tool_parallelism=4)
        backend = ReplayBackend(LLMConfig(backend="replay"))
        calls = [ToolCall(f"task-read-{index}", "read", json.dumps({
            "path": f"task-{index}.py", "root": "run",
        })) for index in range(1, 5)]
        replies = [
            LLMResponse(tool_calls=calls),
            LLMResponse(tool_calls=[ToolCall("task-finish", "finish", '{"summary":"done"}')]),
        ]
        backend.complete_messages = lambda *_args, **_kwargs: replies.pop(0)  # type: ignore[method-assign]
        run_id = "parallel-task-agent"
        workspace = config.workspace_dir / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        for index in range(1, 5):
            (workspace / f"task-{index}.py").write_text(f"item = {index}\n", encoding="utf-8")

        lock = threading.Lock()
        active = 0
        maximum = 0
        barrier = threading.Barrier(4, timeout=3)
        original = WorkspaceRead.run

        def delayed(tool, ctx, path, start_line=1, line_count=240, root="project"):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                barrier.wait()
                return original(tool, ctx, path, start_line, line_count, root)
            finally:
                with lock:
                    active -= 1

        agent = PentestAgent(config, registry=build_registry(["workspace"]), backend=backend)
        with patch.object(WorkspaceRead, "run", delayed):
            state = agent.run(str(workspace), goal="读取项目代码文件并汇总", run_id=run_id, max_steps=6)
        self.assertEqual(maximum, 4)
        self.assertGreaterEqual(state.counters.get("tool_calls", 0), 4)
        finished = agent.events.replay(kinds=("tool.finished",))
        ids = [event.call_id for event in finished if event.call_id.startswith("task-read-")]
        self.assertEqual(ids, [f"task-read-{index}" for index in range(1, 5)])


if __name__ == "__main__":
    unittest.main()
