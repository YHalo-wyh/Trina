"""连接持久化、命名密钥切换与可取消录入流程。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.config import LLMConfig, TrinaConfig
from trina.llm.openai_backend import OpenAICompatBackend
from trina.model_ui import ModelManager
from trina.models import normalize_spec
from trina.providers import PRESETS, ProfileStore, ProviderProfile


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "providers.json"
        self.env = patch.dict(os.environ, {"TRINA_PROVIDERS_FILE": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def profile(self, name="deepseek"):
        return ProviderProfile(name, "deepseek", "https://api.deepseek.com/v1", "deepseek-chat",
                               keys={"work": "test-work", "personal": "test-personal"}, active_key="work")

    def test_selected_connection_survives_restart_and_named_key_switch(self):
        store = ProfileStore()
        p = self.profile()
        store.profiles[p.id], store.active = p, p.id
        store.save()
        cfg = TrinaConfig.load()
        self.assertEqual(cfg.llm.profile, p.id)
        backend = OpenAICompatBackend(cfg.llm)
        self.assertEqual(backend.api_key(), "test-work")
        store.profiles[p.id].active_key = "personal"
        store.save()
        self.assertEqual(backend.api_key(), "test-personal")
        self.assertNotIn("test-personal", repr(p))
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_environment_is_an_explicit_alternative_to_saved_key(self):
        store = ProfileStore()
        p = self.profile()
        store.profiles[p.id] = p
        with patch.dict(os.environ, {"TRINA_LLM_API_KEY": "env-key"}):
            self.assertEqual(store.key(p.id, p.api_key_env), "test-work")
            p.active_key = ""
            self.assertEqual(store.key(p.id, p.api_key_env), "env-key")

    def test_saved_connection_takes_precedence_over_legacy_launcher(self):
        with patch.dict(os.environ, {"TRINA_API": "deepseek", "TRINA_MODEL": "legacy-model"}):
            self.assertEqual(TrinaConfig.load().llm.model, "legacy-model")
            store = ProfileStore()
            p = self.profile("work")
            p.model = "selected-model"
            store.profiles[p.id], store.active = p, p.id
            store.save()
            self.assertEqual(TrinaConfig.load().llm.model, "selected-model")
            cfg = TrinaConfig.load(overrides={"llm.backend": "omp", "llm.profile": "", "llm.model": "provider/model"})
            self.assertEqual((cfg.llm.backend, cfg.llm.profile, cfg.llm.model), ("omp", "", "provider/model"))

    def test_switching_provider_does_not_reuse_other_profile_key(self):
        store = ProfileStore()
        first, second = self.profile("first"), self.profile("second")
        second.keys = {"work": "second-provider-key"}
        store.profiles = {first.id: first, second.id: second}
        store.active = first.id
        store.save()
        cfg = TrinaConfig.load()
        store.apply(cfg.llm, "second")
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "second-provider-key")

    def test_removing_previously_active_profile_keeps_store_loadable(self):
        manager, _, _ = self.manager(["work", "delete"], [])
        manager.store.profiles["work"], manager.store.active = self.profile("work"), "work"
        manager.store.save()
        manager.remove()
        self.assertEqual(ProfileStore().active, "")

    def test_explicit_config_and_overrides_keep_priority(self):
        store = ProfileStore()
        store.profiles["deepseek"], store.active = self.profile(), "deepseek"
        store.save()
        explicit = Path(self.temp.name) / "explicit.yaml"
        explicit.write_text("llm:\n  backend: replay\n  model: custom\n", encoding="utf-8")
        self.assertEqual(TrinaConfig.load(explicit).llm.model, "custom")
        self.assertEqual(TrinaConfig.load(overrides={"llm.model": "override"}).llm.model, "override")

    def test_provider_catalog_and_model_namespace(self):
        self.assertGreaterEqual(len(PRESETS), 25)
        self.assertEqual(len(PRESETS), len({p.id for p in PRESETS}))
        self.assertEqual(normalize_spec("deepseek/deepseek-chat", "openai", provider="deepseek"), "deepseek-chat")
        self.assertEqual(normalize_spec("meta-llama/Llama-3.3", "openai"), "meta-llama/Llama-3.3")
        self.assertEqual(normalize_spec("openai/gpt-oss-20b", "openai", provider="openrouter"), "openai/gpt-oss-20b")
        self.assertEqual(normalize_spec("openrouter/auto", "openai", provider="openrouter"), "openrouter/auto")

    def test_profile_override_changes_endpoint_and_key_together(self):
        store = ProfileStore()
        first, second = self.profile("first"), self.profile("second")
        second.base_url = "https://second.example/v1"
        second.keys = {"work": "second-key"}
        store.profiles, store.active = {first.id: first, second.id: second}, first.id
        store.save()
        with patch.dict(os.environ, {"TRINA_LLM_PROFILE": "second"}):
            cfg = TrinaConfig.load()
            self.assertEqual(cfg.llm.base_url, second.base_url)
            self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "second-key")
        cfg = TrinaConfig.load(overrides={"llm.profile": "second"})
        self.assertEqual(cfg.llm.base_url, second.base_url)

    def manager(self, choices, answers):
        choose = iter(choices)
        ask = iter(answers)
        output = []
        cfg = TrinaConfig()
        manager = ModelManager(cfg, emit=output.append, reload=lambda: None,
                               choose=lambda *_args, **_kwargs: next(choose),
                               ask=lambda *_args, **_kwargs: next(ask))
        return manager, cfg, output

    def test_add_official_provider_and_switch_key_without_echo(self):
        manager, cfg, output = self.manager(["deepseek", "model:deepseek-chat"],
                                           ["work", "https://api.deepseek.com/v1", "secret-one", "first"])
        manager.add()
        self.assertEqual(cfg.llm.profile, "work")
        manager.choose = lambda *_args, **_kwargs: "add"
        answers = iter(["second", "secret-two"])
        manager.ask = lambda *_args, **_kwargs: next(answers)
        manager.keys()
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "secret-two")
        manager.choose = lambda *_args, **_kwargs: "key:first"
        manager.keys()
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "secret-one")
        self.assertNotIn("secret-one", "\n".join(output))
        self.assertNotIn("secret-two", "\n".join(output))

    def test_custom_provider_preserves_full_model_id(self):
        manager, cfg, _ = self.manager(["custom", "responses", "manual"],
                                      ["gateway", "https://gateway.example/v1", "", "vendor/model"])
        manager.add()
        self.assertEqual(cfg.llm.api, "responses")
        self.assertEqual(cfg.llm.model, "vendor/model")
        self.assertEqual(TrinaConfig.load().llm.base_url, "https://gateway.example/v1")

    def test_cancel_add_leaves_no_credentials_on_disk(self):
        manager, _, _ = self.manager(["deepseek", None], ["work", "https://api.deepseek.com/v1", "secret", "first"])
        manager.add()
        self.assertFalse(self.path.exists())

    def test_invalid_or_duplicate_profiles_fail_without_replacing_file(self):
        store = ProfileStore()
        store.profiles["work"] = self.profile("work")
        store.save()
        original = self.path.read_bytes()
        store.profiles["work"].base_url = "https://user:pass@example.com/v1"
        with self.assertRaises(ValueError):
            store.save()
        self.assertEqual(self.path.read_bytes(), original)

    def test_model_home_masks_keys_and_reveals_only_on_explicit_selection(self):
        manager, cfg, output = self.manager(["back"], [])
        item = self.profile("work")
        manager._commit(item)
        revealed = []
        manager.reveal = lambda title, key: revealed.append((title, key))
        manager.run()
        status = manager.status_text()
        self.assertIn("deepseek-chat", status)
        self.assertIn("DeepSeek", status)
        self.assertIn("work  ••••••••", status)
        self.assertNotIn("test-work", status)
        self.assertEqual(revealed, [])
        manager.view_key()
        self.assertEqual(revealed, [("API Key · work", "test-work")])
        self.assertNotIn("test-work", "\n".join(output))
        self.assertNotIn("test-work", manager.status_text())

    def test_viewing_other_key_does_not_activate_it_or_write_file(self):
        manager, cfg, _ = self.manager(["view", "personal"], [])
        manager._commit(self.profile("work"))
        original = self.path.read_bytes()
        revealed = []
        manager.reveal = lambda _title, key: revealed.append(key)
        manager.keys()
        self.assertEqual(revealed, ["test-personal"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "test-work")

    def test_reasoning_persists_per_profile_and_resets_on_incompatible_model(self):
        manager, cfg, _ = self.manager([], [])
        manager._commit(self.profile("work"))
        manager.set_reasoning("high")
        self.assertEqual(cfg.llm.reasoning_effort, "high")
        self.assertEqual(TrinaConfig.load().llm.reasoning_effort, "high")
        item = manager.current()
        item.model, item.base_url = "ordinary-chat", "https://service.example/v1"
        manager._commit(item)
        self.assertEqual(cfg.llm.reasoning_effort, "default")
        self.assertEqual(TrinaConfig.load().llm.reasoning_effort, "default")

    def test_omp_model_and_thinking_survive_legacy_launcher_on_restart(self):
        manager, cfg, _ = self.manager(["provider/model"], [])
        manager.pick_omp_model()
        manager.set_reasoning("xhigh")
        with patch.dict(os.environ, {"TRINA_API": "deepseek", "TRINA_MODEL": "legacy"}):
            loaded = TrinaConfig.load()
        self.assertEqual((loaded.llm.backend, loaded.llm.model, loaded.llm.reasoning_effort),
                         ("omp", "provider/model", "xhigh"))

    def test_legacy_api_connection_can_add_and_delete_current_key(self):
        manager, cfg, output = self.manager(["work"], ["work", "test-new-key"])
        cfg.llm.backend, cfg.llm.base_url, cfg.llm.model = "openai", "https://api.deepseek.com/v1", "deepseek-chat"
        manager.keys(action="add")
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), "test-new-key")
        manager.keys(action="remove")
        self.assertEqual(ProfileStore().profiles[cfg.llm.profile].keys, {})
        self.assertNotIn("test-new-key", "\n".join(output))

    def test_cancel_key_entry_returns_to_model_home_without_saving(self):
        manager, cfg, _ = self.manager(["addkey", "back"], [])
        manager._commit(self.profile("work"))
        original = self.path.read_bytes()
        def cancel(*_args, **_kwargs):
            raise KeyboardInterrupt()
        manager.ask = cancel
        manager.run()
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_global_reasoning_does_not_replace_file(self):
        store = ProfileStore()
        store.save()
        original = self.path.read_bytes()
        store.reasoning_effort = "invalid"
        with self.assertRaises(ValueError):
            store.save()
        self.assertEqual(self.path.read_bytes(), original)

    def test_yaml_reasoning_remains_effective_without_saved_choice(self):
        explicit = Path(self.temp.name) / "reasoning.yaml"
        explicit.write_text("llm:\n  backend: openai\n  model: gpt-5.2\n  reasoning_effort: high\n", encoding="utf-8")
        self.assertEqual(TrinaConfig.load(explicit).llm.reasoning_effort, "high")

    def test_invalid_edit_error_is_visible_inside_model_panel(self):
        manager, _, _ = self.manager([], ["https://user:pass@example.com/v1"])
        manager._commit(self.profile("work"))
        original = self.path.read_bytes()
        selections, headers = iter(["edit", "chat", "back"]), []
        def choose(_title, _choices, *, subtitle=""):
            headers.append(subtitle)
            return next(selections)
        manager.choose = choose
        manager.run()
        self.assertIn("Base URL 必须", headers[-1])
        self.assertEqual(self.path.read_bytes(), original)
