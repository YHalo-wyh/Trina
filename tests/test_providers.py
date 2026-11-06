"""供应商整条配置的保存、编辑、迁移及无副作用测试。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trina.config import TrinaConfig
from trina.llm.openai_backend import OpenAICompatBackend
from trina.model_ui import ModelManager
from trina.models import normalize_spec
from trina.providers import PRESETS, ProfileStore, ProviderProfile, normalize_endpoint


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'providers.json'
        self.env = patch.dict(os.environ, {'TRINA_PROVIDERS_FILE': str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def profile(self, name='work', key='test-work'):
        return ProviderProfile(name, 'deepseek', 'https://api.deepseek.com/v1', 'deepseek-chat', api_key=key)

    def manager(self, choices=(), *, form=None):
        choose = iter(choices)
        output = []
        cfg = TrinaConfig()
        manager = ModelManager(cfg, emit=output.append, reload=lambda: None,
                               choose=lambda *_args, **_kwargs: next(choose), form=form)
        return manager, cfg, output

    def test_selected_provider_and_key_survive_restart(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile())
        loaded = TrinaConfig.load()
        self.assertEqual(loaded.llm.profile, 'work')
        self.assertEqual(OpenAICompatBackend(loaded.llm).api_key(), 'test-work')
        self.assertNotIn('test-work', repr(manager.store.profiles['work']))
        self.assertEqual(json.loads(self.path.read_text())['version'], 2)
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_environment_fallback_is_part_of_provider_configuration(self):
        store = ProfileStore()
        item = self.profile()
        store.profiles[item.id] = item
        with patch.dict(os.environ, {'TRINA_LLM_API_KEY': 'env-key'}):
            self.assertEqual(store.key(item.id, item.api_key_env), 'test-work')
            item.api_key = ''
            self.assertEqual(store.key(item.id, item.api_key_env), 'env-key')

    def test_removed_launcher_variables_do_not_select_a_provider(self):
        with patch.dict(os.environ, {'TRINA_API': 'deepseek', 'TRINA_MODEL': 'legacy'}):
            self.assertNotEqual(TrinaConfig.load().llm.model, 'legacy')
            manager, _, _ = self.manager()
            manager._commit(self.profile())
            self.assertEqual(TrinaConfig.load().llm.model, 'deepseek-chat')
            cfg = TrinaConfig.load(overrides={'llm.backend': 'omp', 'llm.profile': '', 'llm.model': 'provider/model'})
            self.assertEqual((cfg.llm.backend, cfg.llm.profile, cfg.llm.model), ('omp', '', 'provider/model'))

    def test_switching_provider_changes_endpoint_and_key_together(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile('first'))
        second = replace(self.profile('second', 'second-key'), base_url='https://second.example/v1')
        manager._commit(second, activate=False)
        manager.use('second')
        self.assertEqual(cfg.llm.base_url, second.base_url)
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), 'second-key')

    def test_explicit_config_environment_and_overrides_keep_priority(self):
        manager, _, _ = self.manager()
        manager._commit(self.profile('first'))
        manager._commit(self.profile('second', 'second-key'), activate=False)
        explicit = Path(self.temp.name) / 'explicit.yaml'
        explicit.write_text('llm:\n  backend: replay\n  model: custom\n', encoding='utf-8')
        self.assertEqual(TrinaConfig.load(explicit).llm.model, 'custom')
        self.assertEqual(TrinaConfig.load(overrides={'llm.model': 'override'}).llm.model, 'override')
        with patch.dict(os.environ, {'TRINA_LLM_PROFILE': 'second'}):
            self.assertEqual(OpenAICompatBackend(TrinaConfig.load().llm).api_key(), 'second-key')
        self.assertEqual(TrinaConfig.load(overrides={'llm.profile': 'second'}).llm.profile, 'second')

    def test_catalog_model_namespace_and_complete_endpoint(self):
        self.assertGreaterEqual(len(PRESETS), 25)
        self.assertEqual(len(PRESETS), len({p.id for p in PRESETS}))
        self.assertEqual(normalize_spec('deepseek/deepseek-chat', 'openai', provider='deepseek'), 'deepseek-chat')
        self.assertEqual(normalize_spec('meta-llama/Llama-3.3', 'openai'), 'meta-llama/Llama-3.3')
        self.assertEqual(normalize_spec('openai/gpt-oss-20b', 'openai', provider='openrouter'), 'openai/gpt-oss-20b')
        for suffix, api in (('/chat/completions', 'chat'), ('/messages', 'anthropic'), ('/responses', 'responses')):
            self.assertEqual(normalize_endpoint('https://service.example/v1' + suffix + '/', 'chat'), ('https://service.example/v1', api))

    def test_add_provider_passes_preset_to_single_form_and_commits_all_fields(self):
        seen = []
        def form(item, *, is_new):
            seen.append((item, is_new))
            return replace(item, id='公司 DeepSeek', api_key='test-company', note='company')
        manager, cfg, output = self.manager(['deepseek'], form=form)
        manager.add()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][1])
        self.assertEqual(seen[0][0].base_url, 'https://api.deepseek.com/v1')
        self.assertEqual(cfg.llm.profile, '公司 DeepSeek')
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), 'test-company')
        self.assertEqual(ProfileStore().profiles[cfg.llm.profile].note, 'company')
        self.assertNotIn('test-company', '\n'.join(output))

    def test_edit_current_provider_renames_and_replaces_key_atomically(self):
        manager, cfg, _ = self.manager(form=lambda item, **_: replace(item, id='renamed', base_url='https://new.example/v1', model='vendor/model', api='responses', api_key='test-new'))
        manager._commit(self.profile())
        manager.edit('work')
        self.assertNotIn('work', ProfileStore().profiles)
        self.assertEqual(cfg.llm.profile, 'renamed')
        self.assertEqual((cfg.llm.api, cfg.llm.model, cfg.llm.base_url), ('responses', 'vendor/model', 'https://new.example/v1'))
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), 'test-new')
        self.assertNotIn('test-work', self.path.read_text())

    def test_edit_inactive_provider_does_not_switch_current_provider(self):
        manager, cfg, _ = self.manager(form=lambda item, **_: replace(item, api_key='test-edited'))
        manager._commit(self.profile('first'))
        manager._commit(self.profile('second', 'test-second'), activate=False)
        manager.edit('second')
        self.assertEqual(cfg.llm.profile, 'first')
        self.assertEqual(ProfileStore().active, 'first')
        self.assertEqual(ProfileStore().profiles['second'].api_key, 'test-edited')

    def test_cancel_add_and_edit_do_not_save_draft(self):
        def cancelled(item, **_):
            item.api_key = 'test-unsaved'
            return None
        manager, cfg, _ = self.manager(['deepseek'], form=cancelled)
        manager.add()
        self.assertFalse(self.path.exists())
        manager._commit(self.profile())
        original = self.path.read_bytes()
        manager.edit('work')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(manager.store.profiles['work'].api_key, 'test-work')

    def test_duplicate_name_or_invalid_url_keeps_saved_state(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile('first'))
        manager._commit(self.profile('second'), activate=False)
        original = self.path.read_bytes()
        for draft in (replace(self.profile('first'), base_url='https://user:pass@example.com/v1'), self.profile('second')):
            with self.assertRaises(ValueError):
                manager._commit(draft, previous_id='first')
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(cfg.llm.profile, 'first')

    def test_save_failure_rolls_back_in_memory_selection(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile())
        original = self.path.read_bytes()
        with patch.object(manager.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                manager._commit(self.profile('new'))
        self.assertNotIn('new', manager.store.profiles)
        self.assertEqual(manager.store.active, cfg.llm.profile)
        self.assertEqual(self.path.read_bytes(), original)

    def test_remove_current_provider_deletes_whole_record_and_switches_remaining(self):
        manager, cfg, _ = self.manager(['delete'])
        manager._commit(self.profile('first', 'test-first'))
        manager._commit(self.profile('second', 'test-second'), activate=False)
        manager.remove('first')
        store = ProfileStore()
        self.assertEqual(list(store.profiles), ['second'])
        self.assertEqual(cfg.llm.profile, 'second')
        self.assertEqual(OpenAICompatBackend(cfg.llm).api_key(), 'test-second')
        self.assertNotIn('test-first', self.path.read_text())

    def test_remove_last_provider_returns_to_omp_without_saved_key(self):
        manager, cfg, _ = self.manager(['delete'])
        manager._commit(self.profile())
        manager.remove('work')
        self.assertEqual(ProfileStore().profiles, {})
        self.assertEqual((cfg.llm.backend, cfg.llm.profile), ('omp', ''))
        self.assertNotIn('test-work', self.path.read_text())
        with patch.dict(os.environ, {'TRINA_API': 'deepseek'}):
            self.assertEqual(TrinaConfig.load().llm.backend, 'omp')

    def test_version_one_provider_file_is_rejected_without_rewrite(self):
        raw = {'version': 1, 'active': 'work', 'profiles': [
            {'id': 'work', 'provider': 'deepseek', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'keys': {'company': 'test-company', 'personal': 'test-personal'}, 'active_key': 'company'},
            {'id': 'work · personal', 'provider': 'deepseek', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'keys': {}, 'active_key': ''}]}
        self.path.write_text(json.dumps(raw), encoding='utf-8')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "版本不受支持"):
            ProfileStore()
        self.assertEqual(self.path.read_bytes(), original)

    def test_model_home_has_provider_actions_without_independent_key_menus(self):
        manager, _, _ = self.manager()
        manager._commit(self.profile())
        seen = []
        def choose(_title, choices, **kwargs):
            seen.extend(c.value for c in choices)
            self.assertNotIn('test-work', kwargs['subtitle'])
            return 'back'
        manager.choose = choose
        manager.run()
        self.assertTrue({'add', 'edit', 'remove', 'test', 'providers'} <= set(seen))
        self.assertFalse({'keys', 'addkey', 'removekey', 'reveal'} & set(seen))
        self.assertIn('••••••••', manager.status_text())

    def test_reasoning_persists_and_incompatible_model_resets_it(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile())
        manager.set_reasoning('high')
        self.assertEqual(TrinaConfig.load().llm.reasoning_effort, 'high')
        item = replace(manager.current(), model='ordinary-chat', base_url='https://service.example/v1')
        manager._commit(item, previous_id=item.id)
        self.assertEqual(cfg.llm.reasoning_effort, 'default')

    def test_saved_omp_settings_ignore_removed_launcher_variables(self):
        manager, cfg, _ = self.manager(['provider/model'])
        manager.pick_omp_model()
        manager.set_reasoning('xhigh')
        with patch.dict(os.environ, {'TRINA_API': 'deepseek', 'TRINA_MODEL': 'legacy'}):
            loaded = TrinaConfig.load()
        self.assertEqual((loaded.llm.backend, loaded.llm.model, loaded.llm.reasoning_effort), ('omp', 'provider/model', 'xhigh'))

    def test_invalid_global_reasoning_does_not_replace_file(self):
        store = ProfileStore()
        store.save()
        original = self.path.read_bytes()
        store.reasoning_effort = 'invalid'
        with self.assertRaises(ValueError):
            store.save()
        self.assertEqual(self.path.read_bytes(), original)

    def test_yaml_reasoning_is_kept_without_saved_choice(self):
        explicit = Path(self.temp.name) / 'reasoning.yaml'
        explicit.write_text('llm:\n  backend: openai\n  model: gpt-5.2\n  reasoning_effort: high\n', encoding='utf-8')
        self.assertEqual(TrinaConfig.load(explicit).llm.reasoning_effort, 'high')

    def test_connectivity_accepts_http_error_but_does_not_claim_auth_success(self):
        manager, _, _ = self.manager()
        item = replace(self.profile(), model='')
        error = urllib.error.HTTPError(item.base_url, 401, 'unauthorized', {}, io.BytesIO(b'not consumed'))
        with patch('trina.llm.openai_backend.urllib.request.urlopen', side_effect=error) as send:
            result = manager.test_connection(item)
        self.assertIn('HTTP 401', result)
        self.assertIn('未验证', result)
        self.assertIsNone(send.call_args.args[0].get_header('Authorization'))
        self.assertLessEqual(send.call_args.kwargs['timeout'], 10)
        self.assertFalse(self.path.exists())

    def test_connectivity_reports_network_failure(self):
        manager, _, _ = self.manager()
        with patch('trina.llm.openai_backend.urllib.request.urlopen', side_effect=urllib.error.URLError('timeout')):
            self.assertIn('不可达', manager.test_connection(self.profile()))
        self.assertFalse(self.path.exists())

    def test_model_validation_uses_unsaved_key_and_does_not_change_active_provider(self):
        manager, cfg, _ = self.manager()
        manager._commit(self.profile())
        original = self.path.read_bytes()
        draft = replace(self.profile('draft', 'test-draft'), base_url='https://draft.example/v1')
        body = json.dumps({'choices': [{'message': {'content': 'OK'}}]}).encode()
        with patch('trina.llm.openai_backend.urllib.request.urlopen', return_value=io.BytesIO(body)) as send:
            result = manager.test_model(draft)
        request = send.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-draft')
        self.assertTrue(request.full_url.startswith(draft.base_url))
        self.assertIn('验证通过', result)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(cfg.llm.profile, 'work')

    def test_model_validation_failure_redacts_the_draft_key(self):
        manager, _, _ = self.manager()
        item = self.profile(key='test-private')
        error = urllib.error.HTTPError(item.base_url, 403, 'denied', {}, io.BytesIO(b'test-private rejected'))
        with patch('trina.llm.openai_backend.urllib.request.urlopen', side_effect=error):
            result = manager.test_model(item)
        self.assertIn('验证失败', result)
        self.assertNotIn('test-private', result)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
