from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from trina.profiling import AgentProfiler
from trina.tools.registry import ToolResult


def event(kind: str, *, payload=None, call_id="", task_id="", worker_id="worker-1"):
    return SimpleNamespace(kind=kind, payload=payload or {}, call_id=call_id,
                           task_id=task_id, worker_id=worker_id)


class ProfilerTests(unittest.TestCase):
    def test_report_aggregates_supported_metrics_without_raw_payloads(self):
        profiler = AgentProfiler("run-1", worker_id="worker-1")
        profiler.consume_event(event("task.started", task_id="intent-1"))
        profiler.consume_event(event("model.started", payload={"request_id": "req-1"}, task_id="intent-1"))
        profiler.consume_event(event("model.text_delta", payload={"request_id": "req-1", "delta": "x"}, task_id="intent-1"))
        profiler.consume_event(event("model.finished", payload={
            "request_id": "req-1", "ok": True,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 80,
                       "input_reported": True, "output_reported": True, "cache_read_reported": True},
        }, task_id="intent-1"))
        profiler.consume_event(event("tool.started", payload={"tool": "probe"}, call_id="call-1", task_id="intent-1"))
        profiler.consume_event(event("tool.finished", payload={"tool": "probe", "status": "completed", "ok": True,
                                                                 "duration": .25, "raw": "secret"},
                                      call_id="call-1", task_id="intent-1"))
        profiler.consume_event(event("task.finished", payload={"status": "completed"}, task_id="intent-1"))
        profiler.record_phase("prompt_build", .01)
        report = profiler.snapshot()
        self.assertEqual(report["llm"]["input_tokens"], 100)
        self.assertEqual(report["llm"]["cache_read_tokens"], 80)
        self.assertEqual(report["tools"]["calls"], 1)
        self.assertEqual(report["subworkers"]["completed"], 1)
        self.assertIn("prompt_build", report["phases"])
        self.assertNotIn("secret", json.dumps(report, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performance.json"
            written = profiler.write(path)
            self.assertEqual(written["path"], str(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run_id"], "run-1")

    def test_tool_result_view_keeps_failure_and_evidence_reference(self):
        result = ToolResult(
            ok=False,
            summary="HTTP 检查失败",
            error="timeout",
            data={"items": [{"id": index, "body": "x" * 1000} for index in range(80)], "status": 504},
            raw="full raw output" * 1000,
            evidence=[{"path": "tool-output.raw.txt"}],
        )
        observation = result.to_observation()
        self.assertIn("失败", observation)
        self.assertIn("timeout", observation)
        self.assertIn("tool-output.raw.txt", observation)
        self.assertLess(len(observation), 2600)
        self.assertTrue(result.truncated)
        self.assertNotIn("full raw output" * 100, observation)


if __name__ == "__main__":
    unittest.main()
