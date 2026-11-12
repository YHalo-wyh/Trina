"""标准消息、SSE 完整性、缓存前缀及终端增量输出的回归。"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm import LLMResponse, LLMStreamEvent
from trina.llm.openai_backend import OpenAICompatBackend
from trina.llm.replay_backend import ReplayBackend
from trina.tools.registry import ToolRegistry


def sse(*events):
    return io.BytesIO("".join(
        f"data: {json.dumps(event, ensure_ascii=False) if isinstance(event, dict) else event}\n\n"
        for event in events
    ).encode("utf-8"))


class StreamingTests(unittest.TestCase):
    def test_real_http_delivers_delta_before_server_completes_response(self):
        release, first_delta = threading.Event(), threading.Event()
        requests, results = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"early"}}]}\n\n')
                self.wfile.flush()
                release.wait(3)
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        backend = OpenAICompatBackend(LLMConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout=5), key_override="")
        def receive(event):
            if event.type == "text_delta":
                first_delta.set()
        worker = threading.Thread(target=lambda: results.append(backend.complete_messages(
            [{"role": "user", "content": "hello"}], on_event=receive)))
        try:
            worker.start()
            self.assertTrue(first_delta.wait(2), "必须在服务器发送终止事件前收到正文")
            self.assertTrue(worker.is_alive())
            self.assertEqual(results, [])
        finally:
            release.set()
            worker.join(5)
            server.shutdown()
            server.server_close()
            serving.join(2)
        self.assertTrue(results[0].ok, results[0].error)
        self.assertEqual(results[0].text, "early")
        self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "hello"}])

    def test_chat_preserves_roles_streams_before_terminal_and_tallies_once(self):
        backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1"), key_override="test")
        messages = [
            {"role": "system", "content": "stable"}, {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"}, {"role": "user", "content": "second"},
        ]
        events = []
        body = sse(
            {"choices": [{"delta": {"reasoning_content": "private"}}]},
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 2,
                                        "prompt_tokens_details": {"cached_tokens": 60}}}, "[DONE]",
        )
        with patch("trina.llm.openai_backend.urllib.request.urlopen", return_value=body) as send:
            result = backend.complete_messages(messages, on_event=events.append)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["messages"], messages)
        self.assertTrue(payload["stream"])
        self.assertEqual([item.type for item in events], ["start", "thinking_delta", "text_delta", "text_delta", "done"])
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.usage.cache_read_tokens, 60)
        self.assertEqual(backend.ledger.calls, 1)

    def test_native_api_dialects_keep_conversation_structured(self):
        messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}]
        cases = [
            ("anthropic", [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}},
                {"type": "message_delta", "usage": {"output_tokens": 2}}, {"type": "message_stop"},
            ]),
            ("responses", [
                {"type": "response.output_text.delta", "delta": "OK"},
                {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 2}}},
            ]),
        ]
        for api, rows in cases:
            with self.subTest(api=api):
                backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1", api=api), key_override="test")
                with patch("trina.llm.openai_backend.urllib.request.urlopen", return_value=sse(*rows)) as send:
                    result = backend.complete_messages(messages)
                payload = json.loads(send.call_args.args[0].data)
                self.assertEqual(payload["messages" if api == "anthropic" else "input"], messages[1:])
                self.assertEqual(payload["system" if api == "anthropic" else "instructions"], "stable")
                self.assertTrue(result.ok, result.error)
                self.assertEqual((result.text, result.usage.input_tokens, result.usage.output_tokens), ("OK", 10, 2))

    def test_missing_terminal_or_responses_incomplete_never_succeeds(self):
        for api, rows in [
            ("chat", [{"choices": [{"delta": {"content": "partial"}}]}]),
            ("responses", [{"type": "response.output_text.delta", "delta": "partial"},
                           {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}]),
        ]:
            with self.subTest(api=api):
                backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1", api=api), key_override="test")
                events = []
                with patch("trina.llm.openai_backend.urllib.request.urlopen", return_value=sse(*rows)):
                    result = backend.complete_messages([{"role": "user", "content": "test"}], on_event=events.append)
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "")
                self.assertEqual(events[-1].type, "error")

    def test_chat_history_is_append_only_and_protocol_is_not_streamed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = TrinaConfig().with_overrides(run__workspace=directory, audit__dir=str(Path(directory) / "logs"))
            backend = ReplayBackend(LLMConfig(backend="replay"))
            replies = iter(["第一轮回答", "第二轮回答", '{"final": {"summary": "完成"}}'])
            requests, visible = [], []
            def complete(messages, *, on_event=None):
                requests.append([dict(item) for item in messages])
                result = LLMResponse(text=next(replies))
                for part in (result.text[:1], result.text[1:]):
                    on_event(LLMStreamEvent("text_delta", delta=part))
                return result
            backend.complete_messages = complete
            chat = ChatAgent(config, registry=ToolRegistry([]), backend=backend,
                             on_stream=lambda text, final: visible.append((text, final)))
            self.assertEqual(chat.ask("first"), "第一轮回答")
            self.assertEqual(chat.ask("second"), "第二轮回答")
            self.assertEqual(requests[1][:len(requests[0])], requests[0])
            self.assertEqual(requests[1][len(requests[0])], {"role": "assistant", "content": "第一轮回答"})
            self.assertTrue(any(not final and text for text, final in visible))
            visible.clear()
            self.assertEqual(chat.ask("finish"), "完成")
            self.assertEqual(visible, [("完成", True)])
            self.assertTrue(chat.last_reply_streamed)

    def test_terminal_replaces_one_stream_card_and_keeps_draft(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from trina.console_ui import EnhancedConsoleInput
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            editor = EnhancedConsoleInput(status=lambda: "ready", running=lambda: True, dynamic_values=lambda *_: (), plain=True)
            editor.buffer.text = "未发送补充"
            editor.update_stream("first")
            editor.write("another event")
            editor.update_stream("first second", final=True)
            self.assertEqual(len(editor._chunks), 2)
            self.assertEqual(editor.buffer.text, "未发送补充")
            self.assertEqual("".join(editor._chunks).count("[Trina] 输出"), 1)
            self.assertIn("first second", "".join(editor._chunks))

    def test_protocol_after_prose_is_held_before_it_can_reach_the_ui(self):
        from trina.brain.chat import _VisibleStream
        for protocol in ('{"action": "scan"}', '< | DSML | invoke name="scan">'):
            with self.subTest(protocol=protocol):
                visible = []
                stream = _VisibleStream(lambda text, final: visible.append((text, final)))
                for char in "检查一下。\n" + protocol:
                    stream.on_event(LLMStreamEvent("text_delta", delta=char))
                self.assertTrue(visible)
                self.assertFalse(any('{' in text or '<' in text for text, _ in visible))
                stream.cancel()
                self.assertEqual(visible[-1], ("", True))

    def test_malformed_sse_and_length_finish_are_errors(self):
        for body in (
            io.BytesIO(b'data: not-json\n\ndata: [DONE]\n\n'),
            sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]}),
        ):
            backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1"), key_override="test")
            with patch("trina.llm.openai_backend.urllib.request.urlopen", return_value=body):
                result = backend.complete_messages([{"role": "user", "content": "test"}])
            self.assertFalse(result.ok)
            self.assertEqual(result.text, "")


if __name__ == "__main__":
    unittest.main()
