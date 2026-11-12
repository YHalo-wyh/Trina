"""OpenAI 兼容后端的 OpenCode Go 请求契约。"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from trina.config import LLMConfig
from trina.llm.openai_backend import OpenAICompatBackend


class OpenCodeGoHeadersTest(unittest.TestCase):
    def test_go_session_header_is_stable_for_a_conversation(self) -> None:
        backend = OpenAICompatBackend(LLMConfig(
            backend="openai",
            base_url="http://opencode.ai.tsecbench.gw/inference/go/openai/v1",
            model="glm-5.3-flash",
        ))
        requests = []
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

        def respond(request, *, timeout):
            requests.append(request)
            return io.BytesIO(body)

        with patch.dict("os.environ", {"TRINA_LLM_API_KEY": "test-key"}), patch(
            "trina.llm.openai_backend.urllib.request.urlopen", side_effect=respond
        ):
            self.assertTrue(backend.complete("system", "first").ok)
            self.assertTrue(backend.complete("system", "second").ok)

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].full_url, "http://opencode.ai.tsecbench.gw/inference/go/openai/v1/chat/completions")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-key")
        self.assertEqual(requests[0].get_header("User-agent"), "Trina/0.3.0")
        session = requests[0].get_header("X-opencode-session")
        self.assertTrue(session)
        self.assertEqual(session, requests[1].get_header("X-opencode-session"))

    def test_other_provider_receives_no_go_session(self) -> None:
        backend = OpenAICompatBackend(LLMConfig(backend="openai", base_url="https://api.deepseek.com/v1"))
        requests = []
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

        def respond(request, *, timeout):
            requests.append(request)
            return io.BytesIO(body)

        with patch("trina.llm.openai_backend.urllib.request.urlopen", side_effect=respond):
            self.assertTrue(backend.complete("system", "user").ok)

        self.assertIsNone(requests[0].get_header("X-opencode-session"))
        self.assertEqual(requests[0].get_header("User-agent"), "Trina/0.3.0")

    def test_native_messages_and_responses_protocols(self) -> None:
        cases = [
            ("anthropic", {"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 5, "output_tokens": 2}}, "/messages"),
            ("responses", {"output": [{"content": [{"type": "output_text", "text": "hello"}]}], "usage": {"input_tokens": 5, "output_tokens": 2}}, "/responses"),
        ]
        for api, body, endpoint in cases:
            with self.subTest(api=api):
                backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1", api=api), key_override="private")
                with patch("trina.llm.openai_backend.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as send:
                    result = backend.complete("system", "user")
                request = send.call_args.args[0]
                self.assertTrue(request.full_url.endswith(endpoint))
                payload = json.loads(request.data)
                self.assertEqual(result.text, "hello")
                self.assertEqual(result.usage.input_tokens, 5)
                if api == "anthropic":
                    self.assertEqual(payload["system"], "system")
                    self.assertEqual(request.get_header("X-api-key"), "private")
                    self.assertTrue(request.get_header("Anthropic-version"))

    def test_cache_usage_is_normalized_without_double_counting(self):
        cases = [
            ("openai", {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 600}}, 1000, 600, 0),
            ("responses", {"input_tokens": 1000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 200}}, 1000, 400, 200),
            ("anthropic", {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 300}, 1000, 600, 300),
            ("openai", {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 750}, 1000, 750, 0),
        ]
        for api, usage, inputs, cached, written in cases:
            with self.subTest(api=api, usage=usage):
                body = {"usage": usage, "choices": [{"message": {"content": "answer"}}],
                        "content": [{"type": "text", "text": "answer"}],
                        "output": [{"content": [{"type": "output_text", "text": "answer"}]}]}
                backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1", api=api), key_override="test")
                with patch.object(backend, "_request_json", return_value=(body, "")):
                    response = backend.complete("system", "user")
                self.assertEqual(response.usage.input_tokens, inputs)
                self.assertEqual(response.usage.cache_read_tokens, cached)
                self.assertEqual(response.usage.cache_write_tokens, written)
                self.assertEqual(response.usage.to_dict()["total_tokens"], inputs + 200)
                self.assertAlmostEqual(response.usage.to_dict()["cache_hit_rate"], cached / inputs)
                self.assertEqual(backend.ledger.calls, 1)

    def test_missing_cache_is_distinct_from_reported_zero_and_invalid_output_still_has_usage(self):
        backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1"), key_override="test")
        for details, reported in (({}, False), ({"prompt_tokens_details": {"cached_tokens": 0}}, True), ({"prompt_tokens_details": {"cached_tokens": None}}, False)):
            body = {"usage": {"prompt_tokens": 100, **details}, "choices": [{"message": {"content": "answer"}}]}
            with patch.object(backend, "_request_json", return_value=(body, "")):
                response = backend.complete("s", "u")
            self.assertEqual(response.usage.cache_reported, reported)
            self.assertEqual(response.usage.to_dict()["cache_hit_rate"], 0 if reported else None)
        for body in (
            {"choices": [{"message": {"content": "", "reasoning_content": "not an answer"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 30}},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 30}},
        ):
            with patch.object(backend, "_request_json", return_value=(body, "")):
                response = backend.complete("s", "u")
            self.assertFalse(response.ok)
            self.assertEqual(response.text, "")
            self.assertEqual(response.usage.input_tokens, 20)
            self.assertEqual(response.usage.output_tokens, 30)

    def test_zero_denominator_stays_unknown(self):
        from trina.llm.base import LLMUsage
        known_zero = LLMUsage(input_tokens=0, cache_reported=True, cache_input_tokens=0)
        unknown_input = LLMUsage(input_tokens=1000)
        self.assertIsNone(known_zero.merge(unknown_input).to_dict()["cache_hit_rate"])

    def test_http_error_never_echoes_the_key(self) -> None:
        import urllib.error
        backend = OpenAICompatBackend(LLMConfig(base_url="https://service.example/v1"), key_override="private-key")
        error = urllib.error.HTTPError("https://service.example", 403, "denied", {}, io.BytesIO(b'private-key rejected'))
        with patch("trina.llm.openai_backend.urllib.request.urlopen", side_effect=error):
            result = backend.complete("system", "user")
        self.assertFalse(result.ok)
        self.assertNotIn("private-key", result.error)


if __name__ == "__main__":
    unittest.main()
