"""模型页及以供应商为单位的配置生命周期。"""
from __future__ import annotations

import copy
import os
from typing import Callable

from .console_ui import ChoiceSpec, edit_field, enhanced_input_available, select_option
from .providers import PRESETS, PRESET_BY_ID, ProfileStore, ProviderProfile, normalize_endpoint, validate_address


class ModelManager:
    def __init__(self, config, *, emit: Callable[[str], None], reload: Callable[[], None],
                 ask=None, choose=None, form=None, store: ProfileStore | None = None):
        self.config, self.emit, self.reload = config, emit, reload
        self.store = store or ProfileStore()
        self.ask, self.choose, self.form = ask or self._ask, choose or select_option, form

    @staticmethod
    def _ask(label: str, default: str = "", *, secret: bool = False) -> str:
        if enhanced_input_available():
            return edit_field(label, default, secret=secret)
        if secret:
            import getpass
            return getpass.getpass(label + " › ").strip()
        return input(label + (f" [{default}]" if default else "") + " › ").strip() or default

    def _commit(self, profile: ProviderProfile, *, activate: bool = True, previous_id: str = "") -> None:
        from .llm.reasoning import reasoning_support
        probe = copy.copy(self.config.llm)
        probe.backend, probe.model, probe.base_url, probe.api = "openai", profile.model, profile.base_url, profile.api
        if profile.reasoning_effort not in reasoning_support(probe).levels:
            profile.reasoning_effort = "default"
        profile.validate()
        if previous_id != profile.id and profile.id in self.store.profiles:
            raise ValueError("供应商名称已存在")
        old_profiles, old_active = self.store.profiles, self.store.active
        self.store.profiles = dict(old_profiles)
        if previous_id:
            self.store.profiles.pop(previous_id, None)
            if self.store.active == previous_id:
                self.store.active = profile.id
        self.store.profiles[profile.id] = profile
        if activate:
            self.store.active = profile.id
        try:
            self.store.save()
        except Exception:
            self.store.profiles, self.store.active = old_profiles, old_active
            raise
        if activate or self.config.llm.profile in (profile.id, previous_id):
            self.store.apply(self.config.llm, profile.id)
            self.reload()

    def use(self, name: str) -> None:
        if name not in self.store.profiles:
            raise ValueError(f"供应商 {name} 不存在")
        self._commit(copy.deepcopy(self.store.profiles[name]), previous_id=name)

    def status_text(self) -> str:
        """首屏展示供应商状态，不包含明文凭据。"""
        from .llm.reasoning import reasoning_support
        llm = self.config.llm
        item = self.store.profiles.get(llm.profile)
        name = item.id if item else "当前配置"
        key = (item.api_key or os.environ.get(item.api_key_env, "")) if item else os.environ.get(llm.api_key_env, "")
        key_status = "••••••••" if key else "未配置 / 本地免密"
        address = llm.base_url
        effort = reasoning_support(llm).label(llm.reasoning_effort)
        return f"供应商  {name}\n请求地址 {address}\n模型    {llm.model}\n思考    {effort}\nAPI Key {key_status}"

    def run(self) -> None:
        notice = ""
        while True:
            choices = [ChoiceSpec("models", "切换模型"), ChoiceSpec("providers", "切换供应商"),
                       ChoiceSpec("reasoning", "思考强度"), ChoiceSpec("add", "添加供应商"),
                       ChoiceSpec("edit", "编辑供应商"), ChoiceSpec("remove", "删除供应商"),
                       ChoiceSpec("test", "测试当前供应商"), ChoiceSpec("back", "返回")]
            selected = self.choose("Model", choices, subtitle=self.status_text() + ("\n" + notice if notice else ""))
            notice = ""
            if selected in (None, "back"):
                return
            try:
                if selected == "providers":
                    self.pick_provider()
                elif selected == "reasoning":
                    from .llm.reasoning import reasoning_support
                    support = reasoning_support(self.config.llm)
                    level = self.choose("思考强度", [ChoiceSpec(x, ("● " if x == self.config.llm.reasoning_effort else "  ") + support.label(x)) for x in support.levels], subtitle=self.config.llm.model)
                    if level:
                        self.set_reasoning(level)
                elif selected == "add":
                    self.add()
                elif selected == "edit":
                    self.edit()
                elif selected == "remove":
                    self.remove()
                elif selected == "test":
                    notice = self.test_connection(self.current())
                elif selected == "models":
                    item = self.current()
                    model = self.pick_model(item)
                    if model:
                        item.model = model
                        self._commit(item, previous_id=item.id if item.id in self.store.profiles else "")
            except ValueError as exc:
                notice = str(exc)
            except KeyboardInterrupt:
                continue

    def set_reasoning(self, level: str) -> None:
        from .llm.reasoning import reasoning_support
        llm = self.config.llm
        if level not in reasoning_support(llm).levels:
            raise ValueError("当前模型不支持该思考档位")
        item = self.current()
        item.reasoning_effort = level
        self._commit(item, previous_id=item.id if item.id in self.store.profiles else "")

    def provider_choices(self) -> list[ChoiceSpec]:
        return [ChoiceSpec(p.id, f"{'●' if p.id == self.config.llm.profile else '○'} {p.id} · {p.model}") for p in self.store.profiles.values()]

    def pick_provider(self) -> None:
        selected = self.choose("切换供应商", [ChoiceSpec("use:" + p.value, p.description) for p in self.provider_choices()])
        if selected:
            self.use(selected[4:])

    def current(self) -> ProviderProfile:
        llm = self.config.llm
        if llm.profile in self.store.profiles:
            return copy.deepcopy(self.store.profiles[llm.profile])
        provider = next((p.id for p in PRESETS if p.base_url.rstrip("/") == llm.base_url.rstrip("/")), "custom")
        name, suffix = provider, 2
        while name in self.store.profiles:
            name, suffix = f"{provider}-{suffix}", suffix + 1
        return ProviderProfile(name, provider, llm.base_url, llm.model, llm.api_key_env, llm.api,
                               reasoning_effort=llm.reasoning_effort, cache_mode=llm.cache_mode,
                               cache_ttl=llm.cache_ttl, cache_features=list(llm.cache_features))

    @staticmethod
    def protocol_choices() -> list[ChoiceSpec]:
        return [ChoiceSpec("chat", "OpenAI Chat"), ChoiceSpec("anthropic", "Anthropic Messages"), ChoiceSpec("responses", "OpenAI Responses")]

    def _edit_form(self, item: ProviderProfile, *, is_new: bool) -> ProviderProfile | None:
        if self.form:
            return self.form(copy.deepcopy(item), is_new=is_new)
        if enhanced_input_available():
            from .provider_form import ProviderEditor
            def validate_name(draft):
                if draft.id in self.store.profiles and (is_new or draft.id != item.id):
                    raise ValueError("供应商名称已存在")
            return ProviderEditor(item, is_new=is_new, test_connection=self.test_connection, test_model=self.test_model, validate=validate_name).run()
        # 管道 / 注入输入沿用普通问答，提交仍以供应商整条配置为单位。
        draft = copy.deepcopy(item)
        while True:
            draft.id = self.ask("供应商名称", draft.id)
            draft.base_url = self.ask("请求地址", draft.base_url)
            key = self.ask("API Key（留空保留；输入 - 清空）", secret=True)
            if key:
                draft.api_key = "" if key == "-" else key
            draft.model = self.ask("模型", draft.model)
            api = self.choose("接口协议", self.protocol_choices())
            if not api:
                return None
            draft.base_url, draft.api = normalize_endpoint(draft.base_url, api)
            selected = self.choose("供应商配置", [ChoiceSpec("save", "保存"), ChoiceSpec("test", "测试连通性"), ChoiceSpec("model", "验证 Key 和模型"), ChoiceSpec("back", "取消")])
            if selected == "save":
                return draft
            if selected in (None, "back"):
                return None
            self.emit(self.test_model(draft) if selected == "model" else self.test_connection(draft))

    def add(self, provider_id: str = "") -> None:
        selected = provider_id or self.choose("供应商预设", [ChoiceSpec("custom", "自定义请求地址")] + [ChoiceSpec(p.id, f"{p.name} · {p.group}") for p in PRESETS])
        if not selected:
            return
        preset = PRESET_BY_ID.get(selected)
        base_name = preset.name if preset else "自定义供应商"
        name, suffix = base_name, 2
        while name in self.store.profiles:
            name, suffix = f"{base_name}-{suffix}", suffix + 1
        item = ProviderProfile(name, selected, preset.base_url if preset else "", preset.models[0] if preset and preset.models else "", preset.api_key_env if preset else "TRINA_LLM_API_KEY", preset.api if preset else "chat")
        draft = self._edit_form(item, is_new=True)
        if draft:
            self._commit(draft)

    def edit(self, name: str = "") -> None:
        if not name:
            if self.store.profiles:
                name = self.choose("编辑供应商", self.provider_choices())
                if not name:
                    return
            else:
                item = self.current()
        if name:
            if name not in self.store.profiles:
                raise ValueError("供应商不存在")
            item = copy.deepcopy(self.store.profiles[name])
        draft = self._edit_form(item, is_new=False)
        if draft:
            self._commit(draft, activate=not name or self.config.llm.profile == name, previous_id=name or "")

    def _backend_for(self, item: ProviderProfile):
        from .config import LLMConfig
        from .llm.openai_backend import OpenAICompatBackend
        from .llm.reasoning import reasoning_support
        cfg = LLMConfig(backend="openai", base_url=item.base_url, model=item.model, api=item.api,
                        api_key_env=item.api_key_env, timeout=10, reasoning_effort=item.reasoning_effort,
                        cache_mode=item.cache_mode, cache_ttl=item.cache_ttl, cache_features=list(item.cache_features))
        if cfg.reasoning_effort not in reasoning_support(cfg).levels:
            cfg.reasoning_effort = "default"
        return OpenAICompatBackend(cfg, key_override=item.api_key or os.environ.get(item.api_key_env, ""))

    def test_connection(self, item: ProviderProfile) -> str:
        validate_address(item.base_url)
        return self._backend_for(item).check_connectivity()[1]

    def test_model(self, item: ProviderProfile) -> str:
        item.validate()
        response = self._backend_for(item).complete("", "Reply with OK.", max_tokens=128)
        return f"模型验证通过 · {item.model} · {response.duration:.2f}s" if response.ok else f"模型验证失败：{response.error}"

    def pick_model(self, item: ProviderProfile) -> str | None:
        preset = PRESET_BY_ID.get(item.provider)
        models = list(dict.fromkeys(([item.model] if item.model else []) + list(preset.models if preset else ())))
        while True:
            selected = self.choose(f"选择模型 · {item.id}", [ChoiceSpec("model:" + m, m) for m in models] + [ChoiceSpec("refresh", "从供应商刷新模型列表"), ChoiceSpec("manual", "输入模型 ID")], subtitle=item.base_url)
            if selected is None:
                return None
            if selected.startswith("model:"):
                return selected[6:]
            if selected == "manual":
                return self.ask("模型 ID", item.model) or None
            if selected == "refresh":
                try:
                    models = self._backend_for(item).list_remote_models()
                except ValueError as exc:
                    self.emit(str(exc))

    def remove(self, name: str = "") -> None:
        name = name or self.choose("删除供应商", self.provider_choices())
        if not name:
            return
        if name not in self.store.profiles:
            raise ValueError("供应商不存在")
        if self.choose("删除供应商及其请求地址、Key 和模型设置", [ChoiceSpec("back", "取消"), ChoiceSpec("delete", f"删除 {name}")]) != "delete":
            return
        remaining = {key: value for key, value in self.store.profiles.items() if key != name}
        old_profiles, old_active = self.store.profiles, self.store.active
        self.store.profiles = remaining
        active_deleted = self.config.llm.profile == name
        if self.store.active == name or active_deleted:
            self.store.active = next(iter(remaining), "")
        try:
            self.store.save()
        except Exception:
            self.store.profiles, self.store.active = old_profiles, old_active
            raise
        if active_deleted:
            if self.store.active:
                self.store.apply(self.config.llm)
            else:
                from .config import LLMConfig
                fresh = LLMConfig()
                self.config.llm.__dict__.update(fresh.__dict__)
            self.reload()
