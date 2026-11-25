"""时延报告只读取事件元数据，并按实际来源配对。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trina.event_latency import summarize_latency
from trina.events import EventBus, ModelEventStream
from trina.llm import LLMResponse


class EventLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "events.sqlite3"
        self.bus = EventBus(self.path, "demo")

    def emit(self, seconds: float, kind: str, *, worker: str = "", **payload: object) -> None:
        with patch("trina.events.time.time", return_value=seconds):
            self.bus.publish(kind, run_id=worker or "chat", task_id=worker, worker_id=worker, **payload)

    def test_reports_response_headers_first_reasoning_and_first_text_separately(self):
        self.emit(1, "model.started", request_id="r")
        self.emit(2, "model.connected", request_id="r")
        self.emit(3, "model.text_delta", request_id="r", delta="")
        self.emit(4, "model.thinking_delta", request_id="r", delta="first")
        self.emit(5, "model.thinking_delta", request_id="r", delta="more")
        self.emit(6, "model.text_delta", request_id="r", delta="text")
        self.emit(7, "model.finished", request_id="r")
        summary = summarize_latency(self.path)
        self.assertEqual(summary["request_to_connected"]["p50_seconds"], 1)
        self.assertEqual(summary["request_to_first_output"]["p50_seconds"], 3)
        self.assertEqual(summary["connected_to_first_output"]["p50_seconds"], 2)
        self.assertEqual(summary["request_to_first_text"]["p50_seconds"], 5)
        self.assertEqual(summary["request_to_first_output"]["samples"], 1)

        partial = summarize_latency(self.path, after=1)
        self.assertIsNone(partial["request_to_first_output"]["p95_seconds"])

    def test_tool_gap_uses_last_terminal_and_does_not_cross_turns_or_workers(self):
        self.emit(1, "tool.finished", worker="a")
        self.emit(2, "model.started", worker="b", request_id="same")
        self.emit(3, "tool.finished", worker="a")
        self.emit(4, "model.started", worker="a", request_id="same")
        self.emit(5, "model.thinking_delta", worker="b", request_id="same", delta="b")
        self.emit(6, "model.thinking_delta", worker="a", request_id="same", delta="a")
        self.emit(7, "tool.finished")
        self.emit(20, "user.message")
        self.emit(30, "model.started", request_id="chat")
        self.emit(31, "tool.finished")
        self.emit(32, "tool.requested")
        self.emit(33, "model.started", request_id="unfinished")

        summary = summarize_latency(self.path)
        self.assertEqual(summary["last_tool_to_request"],
                         {"samples": 1, "p50_seconds": 1, "p95_seconds": 1})
        self.assertEqual(summary["request_to_first_output"]["p50_seconds"], 2.5)
        self.assertEqual(summarize_latency(self.path, task="b")["last_tool_to_request"]["samples"], 0)

    def test_transport_timing_is_recorded_without_exposing_other_transport_fields(self):
        for reused, connect in ((False, .2), (True, 0)):
            stream = ModelEventStream(self.bus.publish)
            response = LLMResponse(text="ok", transport={
                "connection_reused": reused, "connect_seconds": connect,
                "headers_seconds": .8, "first_event_seconds": 1.2, "other": "private",
            })
            stream.finish(response)

        finished = self.bus.replay(kinds=("model.finished",))
        self.assertNotIn("other", finished[0].payload["transport"])
        summary = summarize_latency(self.path)
        self.assertEqual(summary["new_connection"]["samples"], 1)
        self.assertEqual(summary["new_connection"]["p50_seconds"], .2)
        self.assertEqual(summary["transport_to_headers"]["samples"], 2)
        self.assertEqual(summary["transport_to_first_event"]["p50_seconds"], 1.2)

    def test_latency_cli_prints_only_aggregates(self):
        from trina.cli import main

        self.emit(1, "model.started", request_id="private-request")
        self.emit(2, "model.text_delta", request_id="private-request", delta="private-output")
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["events", str(self.path), "--latency", "--json"])
        output = stream.getvalue()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["request_to_first_output"]["samples"], 1)
        self.assertNotIn("private", output)
        self.assertEqual(self.bus.last_seq, 2)
