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
        self.assertEqual(requests[0].get_header("User-agent"), "Trina/0.2.0")
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
        self.assertEqual(requests[0].get_header("User-agent"), "Trina/0.2.0")

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
