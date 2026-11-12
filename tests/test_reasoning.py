"""思考参数按协议落到真实请求，不修改系统提示或授权控制。"""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from trina.config import LLMConfig
from trina.llm.openai_backend import OpenAICompatBackend
from trina.llm.reasoning import reasoning_support


class ReasoningTests(unittest.TestCase):
    def request(self, model, level, *, api="chat", base="https://service.example/v1", max_tokens=4096):
        cfg = LLMConfig(backend="openai", base_url=base, model=model, api=api, reasoning_effort=level)
        body = {"choices": [{"message": {"content": "OK"}}],
                "content": [{"type": "text", "text": "OK"}],
                "output": [{"content": [{"type": "output_text", "text": "OK"}]}]}
        with patch("trina.llm.openai_backend.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(body).encode())) as send:
            result = OpenAICompatBackend(cfg, key_override="test-only").complete("unchanged-system", "user", max_tokens=max_tokens)
        self.assertTrue(result.ok, result.error)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload.get("instructions", payload.get("system", payload.get("messages", [{}])[0].get("content"))), "unchanged-system")
        return payload

    def test_chat_and_responses_use_their_native_reasoning_field(self):
        chat = self.request("gpt-5.2", "high")
        self.assertEqual(chat["reasoning_effort"], "high")
        self.assertIn("max_completion_tokens", chat)
        self.assertNotIn("temperature", chat)
        responses = self.request("gpt-5.2", "off", api="responses")
        self.assertEqual(responses["reasoning"], {"effort": "none"})
        self.assertNotIn("reasoning_effort", responses)

    def test_default_leaves_provider_reasoning_unspecified(self):
        for model in ("custom", "deepseek-chat", "gpt-5.2"):
            with self.subTest(model=model):
                payload = self.request(model, "default")
                self.assertFalse({"thinking", "reasoning", "reasoning_effort", "output_config"} & payload.keys())

    def test_deepseek_toggle_and_effort_across_protocols(self):
        chat = self.request("deepseek-chat", "max")
        self.assertEqual(chat["thinking"], {"type": "enabled"})
        self.assertEqual(chat["reasoning_effort"], "max")
        self.assertNotIn("temperature", chat)
        disabled = self.request("deepseek-chat", "off")
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", disabled)
        messages = self.request("deepseek-v4-flash", "low", api="anthropic")
        self.assertEqual(messages["output_config"], {"effort": "low"})
        responses = self.request("deepseek-chat", "high", api="responses")
        self.assertEqual(responses["reasoning"], {"effort": "high"})

    def test_claude_adaptive_and_budget_fit_output_limit(self):
        adaptive = self.request("claude-opus-4-6", "max", api="anthropic")
        self.assertEqual(adaptive["thinking"], {"type": "adaptive"})
        self.assertEqual(adaptive["output_config"], {"effort": "max"})
        budget = self.request("claude-sonnet-4-5", "high", api="anthropic", max_tokens=2048)
        self.assertEqual(budget["thinking"], {"type": "enabled", "budget_tokens": 2047})
        probe = self.request("claude-sonnet-4-5", "high", api="anthropic", max_tokens=100)
        self.assertEqual(probe["thinking"], {"type": "disabled"})

    def test_toggle_models_have_only_default_off_and_on(self):
        for model in ("glm-5.3-flash", "kimi-k2.5", "qwen3-235b-a22b"):
            with self.subTest(model=model):
                cfg = LLMConfig(backend="openai", model=model)
                self.assertEqual(reasoning_support(cfg).levels, ("default", "off", "high"))
                self.assertEqual(reasoning_support(cfg).label("high"), "开启")
                payload = self.request(model, "off")
                self.assertEqual(payload.get("enable_thinking", payload.get("thinking")),
                                 False if model.startswith("qwen") else {"type": "disabled"})

    def test_openrouter_uses_gateway_reasoning_instead_of_vendor_fields(self):
        payload = self.request("deepseek/deepseek-v4-flash", "high", base="https://openrouter.ai/api/v1")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_unsupported_level_fails_before_outbound_request(self):
        cfg = LLMConfig(backend="openai", base_url="https://service.example/v1", model="unknown", reasoning_effort="high")
        with patch("trina.llm.openai_backend.urllib.request.urlopen") as send:
            result = OpenAICompatBackend(cfg).complete("system", "user")
        self.assertFalse(result.ok)
        self.assertIn("不支持", result.error)
        send.assert_not_called()

    def test_codex_does_not_offer_unsupported_minimal_or_off(self):
        for model in ("gpt-5-codex", "gpt-5.1-codex", "gpt-5.2-codex"):
            levels = reasoning_support(LLMConfig(backend="openai", model=model)).levels
            self.assertNotIn("off", levels)
            self.assertNotIn("minimal", levels)

    def test_fixed_mode_variants_do_not_offer_a_fake_toggle(self):
        for model in ("deepseek-r1", "qwen3-coder-plus", "qwen3-235b-a22b-thinking-2507", "qwen3-235b-a22b-instruct-2507", "gpt-5-chat-latest"):
            with self.subTest(model=model):
                self.assertEqual(reasoning_support(LLMConfig(backend="openai", model=model)).levels, ("default",))
        self.assertEqual(reasoning_support(LLMConfig(backend="openai", model="gpt-5.2-pro")).levels,
                         ("default", "medium", "high", "xhigh"))

if __name__ == "__main__":
    unittest.main()
