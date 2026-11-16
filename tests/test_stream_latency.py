"""真实事件存储与终端之间的低延迟流回归，不调用付费模型。"""
from __future__ import annotations

import tempfile
import threading
import unittest
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from trina.brain.chat import _VisibleStream
from trina.console_ui import EnhancedConsoleInput
from trina.event_view import EventTranscript
from trina.events import EventBus, ModelEventStream
from trina.llm import LLMResponse, LLMStreamEvent
from trina.config import LLMConfig
from trina.llm.openai_backend import OpenAICompatBackend


class StreamLatencyTests(unittest.TestCase):
    def test_real_sse_thinking_and_reply_are_visible_before_done(self):
        thought_seen, reply_seen = threading.Event(), threading.Event()
        allow_reply, allow_done = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                def send(delta):
                    self.wfile.write(("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode())
                    self.wfile.flush()
                send({"reasoning_content": "先检查编译器。"})
                allow_reply.wait(5)
                send({"content": "开始编译。"})
                allow_done.wait(5)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True, dynamic_values=lambda *_: (), plain=True)
            bus = EventBus(Path(tmp) / "events.sqlite3", "http")
            view = EventTranscript(editor.write, editor=editor)
            def receive(event):
                view.consume(event)
                if event.kind == "model.thinking_delta":
                    thought_seen.set()
                if event.kind == "model.text_delta":
                    reply_seen.set()
            bus.subscribe(receive)
            results = []
            def request():
                backend = OpenAICompatBackend(LLMConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout=5), key_override="fixture-key")
                stream = ModelEventStream(bus.publish)
                result = stream.invoke(backend.complete_messages, [{"role": "user", "content": "编译"}], on_event=stream.on_event)
                stream.finish(result)
                results.append(result)
            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.assertTrue(thought_seen.wait(2))
                self.assertIn("先检查编译器", editor._chunk_text(editor._chunks[0], 80))
                self.assertTrue(worker.is_alive())
                allow_reply.set()
                self.assertTrue(reply_seen.wait(2))
                self.assertIn("开始编译", editor._chunk_text(editor._chunks[1], 80))
                self.assertTrue(worker.is_alive(), "正文必须在 DONE 前显示")
            finally:
                allow_reply.set()
                allow_done.set()
                worker.join(6)
                server.shutdown()
                server.server_close()
                server_thread.join(2)
            self.assertTrue(results[0].ok)

    def test_burst_is_coalesced_without_losing_bytes_or_phase_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = EventBus(Path(tmp) / "events.sqlite3", "burst")
            visible = []
            stream = ModelEventStream(bus.publish, visible.append)
            for delta in ["think"] * 300:
                stream.on_event(LLMStreamEvent("thinking_delta", delta=delta))
            for delta in ["reply"] * 300:
                stream.on_event(LLMStreamEvent("text_delta", delta=delta))
            stream.finish(LLMResponse(ok=True, text="reply" * 300))
            stream.on_event(LLMStreamEvent("text_delta", delta="late"))
            rows = bus.replay()
            self.assertLess(len(rows), 30, "短时间大量 token 不得变成逐 token 的磁盘事务")
            for kind, expected in (("thinking_delta", "think" * 300), ("text_delta", "reply" * 300)):
                self.assertEqual("".join(row.payload["delta"] for row in rows if row.kind == "model." + kind), expected)
            self.assertEqual(rows[-1].kind, "model.finished")
            self.assertEqual("".join(item.delta for item in visible if item.type == "text_delta"), "reply" * 300)

    def test_idle_tail_and_first_chunk_arrive_before_request_finishes(self):
        rows, arrived = [], threading.Event()
        def emit(kind, **payload):
            rows.append((kind, payload))
            if payload.get("delta") == "tail":
                arrived.set()
        stream = ModelEventStream(emit)
        stream.on_event(LLMStreamEvent("text_delta", delta="first"))
        self.assertEqual(rows[-1][1]["delta"], "first")
        stream.on_event(LLMStreamEvent("text_delta", delta="tail"))
        self.assertTrue(arrived.wait(1), "供应商暂停时也不能把尾部留到 done")
        self.assertNotIn("model.finished", [kind for kind, _ in rows])
        stream.finish(LLMResponse(ok=True, text="firsttail"))

    def test_exception_flushes_pending_tail_and_closes_stream(self):
        rows = []
        stream = ModelEventStream(lambda kind, **data: rows.append((kind, data)))
        def fail():
            stream.on_event(LLMStreamEvent("thinking_delta", delta="one"))
            stream.on_event(LLMStreamEvent("thinking_delta", delta="two"))
            raise RuntimeError("connection lost")
        with self.assertRaises(RuntimeError):
            stream.invoke(fail)
        self.assertEqual("".join(data["delta"] for kind, data in rows if kind == "model.thinking_delta"), "onetwo")
        self.assertEqual(rows[-1][0], "model.finished")
        self.assertFalse(rows[-1][1]["ok"])

    def test_code_fences_and_braces_stream_before_final(self):
        seen = []
        stream = _VisibleStream(lambda text, final: seen.append((text, final)))
        code = '下面是代码：\n```python\nvalue = {"answer": 42}\nprint(value)\n```\n'
        for char in code:
            stream.on_event(LLMStreamEvent("text_delta", delta=char))
        self.assertIn('print(value)', seen[-1][0])
        self.assertTrue(all(not final for _, final in seen))

    def test_thinking_text_and_tool_output_update_independent_blocks(self):
        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            bus = EventBus(Path(tmp) / "events.sqlite3", "ui")
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True, dynamic_values=lambda *_: (), plain=True)
            editor.buffer.text = "补充尚未发送"
            bus.subscribe(EventTranscript(editor.write, editor=editor).consume)
            stream = ModelEventStream(bus.publish)
            stream.on_event(LLMStreamEvent("thinking_delta", delta="检查编译条件"))
            self.assertIn("检查编译条件", editor._chunk_text(editor._chunks[0], 90))
            stream.on_event(LLMStreamEvent("text_delta", delta="开始验证。"))
            self.assertIn("开始验证", editor._chunk_text(editor._chunks[1], 90))
            stream.finish(LLMResponse(ok=True, text="开始验证。"))
            bus.publish("assistant.message", text="开始验证。", final=True)
            self.assertEqual(len(editor._chunks), 2, "最终正文不能再复制一份流式输出")
            bus.publish("tool.started", call_id="call", tool="terminal_exec", params={"argv": ["python", "-V"]})
            from trina.terminal import render_tool_call_fragments
            with patch("trina.terminal.render_tool_call_fragments", wraps=render_tool_call_fragments) as render:
                for _ in range(50):
                    bus.publish("tool.output", call_id="call", delta="line\n", stream="stdout")
                self.assertEqual(render.call_count, 0, "网络/工具线程不应执行卡片排版")
                self.assertGreaterEqual(editor._char_count, 50 * len("line\n"), "工具输出必须计入历史缓冲大小")
                editor.transcript_control.sync(90)
                self.assertEqual(render.call_count, 1, "显示帧只排版最新快照")
                editor.transcript_control.sync(90)
                self.assertEqual(render.call_count, 1)
            self.assertEqual(len(editor._chunks), 3)
            self.assertEqual(editor.buffer.text, "补充尚未发送")
            editor.clear_transcript()
            self.assertFalse(editor._thinking_indices)


if __name__ == "__main__":
    unittest.main()
