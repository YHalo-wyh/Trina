"""模型自动预算、手动窗口及供应商切换持久化。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trina.config import LLMConfig, TrinaConfig
from trina.model_context import parse_context_window
from trina.model_ui import ModelManager
from trina.provider_form import ProviderEditor
from trina.providers import ProfileStore, ProviderProfile


class ModelContextTests(unittest.TestCase):
    def test_deepseek_500k_and_confirmed_million_models(self):
        for model in ("deepseek-chat", "DeepSeek V4.1 Flash Max", "deepseek-v4-pro"):
            self.assertEqual(LLMConfig(model=model).effective_context_window, 500000)
        for model in ("gpt-4.1", "gpt-5.4", "gpt-5.6-sol", "gpt-6.1-sol", "gemini-2.5-pro", "claude-sonnet-4-6", "claude-opus-4.6", "claude-sonnet-5-5"):
            self.assertEqual(LLMConfig(model=model).effective_context_window, 1000000)
        self.assertEqual(LLMConfig(model="unknown-vendor-model").effective_context_window, 32768)
        self.assertNotEqual(LLMConfig(model="gpt-5.4-mini").effective_context_window, 1000000)
        self.assertEqual(LLMConfig(model="deepseek-chat", context_window=1000000).effective_context_window, 1000000)

    def test_manual_context_parsing_and_validation(self):
        for text, number in (("auto", 0), ("500K", 500000), ("1M", 1000000), ("1.5M", 1500000), ("262144", 262144)):
            self.assertEqual(parse_context_window(text), number)
        for text in ("-1", "large", "0.1", "500000;id"):
            with self.assertRaises(ValueError):
                parse_context_window(text)
        with self.assertRaises(ValueError):
            LLMConfig(context_window=1000).validate()

    def test_manager_save_switch_reload_and_auto_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "providers.json")
            cfg = TrinaConfig()
            cfg.llm.base_url = "https://api.deepseek.com/v1"
            manager = ModelManager(cfg, emit=lambda _: None, reload=lambda: None, store=store)
            manager.set_context_window("750K")
            first = cfg.llm.profile
            self.assertEqual(cfg.llm.effective_context_window, 750000)
            self.assertIn("750,000", manager.status_text())
            other = ProviderProfile("million", "openai", "https://api.openai.com/v1", "gpt-4.1")
            manager._commit(other)
            self.assertEqual(cfg.llm.effective_context_window, 1000000)
            manager.use(first)
            self.assertEqual(cfg.llm.effective_context_window, 750000)
            loaded = ProfileStore(store.path)
            self.assertEqual(loaded.profiles[first].context_window, 750000)
            manager.set_context_window("auto")
            self.assertEqual(cfg.llm.effective_context_window, 500000)

    def test_provider_form_draft_keeps_context_and_rejects_invalid_input(self):
        from prompt_toolkit.application.current import create_app_session
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            form = ProviderEditor(ProviderProfile("x", "custom", "http://localhost/v1", "m"), is_new=True,
                                  test_connection=lambda _: "ok", test_model=lambda _: "ok")
            form.fields["context_window"].text = "1M"
            self.assertEqual(form.draft().context_window, 1000000)
            form.fields["context_window"].text = "bad"
            with self.assertRaises(ValueError):
                form.draft()
