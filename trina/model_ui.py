"""模型管理页：供应商、模型与命名密钥的可取消交互流程。"""
from __future__ import annotations

import copy
import os
from typing import Callable

from .console_ui import ChoiceSpec, enhanced_input_available, select_option
from .providers import PRESETS, PRESET_BY_ID, ProfileStore, ProviderProfile


class ModelManager:
    def __init__(self, config, *, emit: Callable[[str], None], reload: Callable[[], None],
                 ask=None, choose=None, store: ProfileStore | None = None):
        self.config, self.emit, self.reload = config, emit, reload
        self.store = store or ProfileStore()
        self.ask = ask or self._ask
        self.choose = choose or select_option

    @staticmethod
    def _ask(label: str, default: str = "", *, secret: bool = False) -> str:
        if enhanced_input_available():
            from prompt_toolkit import prompt
            return prompt(label + " › ", default=default, is_password=secret).strip()
        if secret:
            import getpass
            return getpass.getpass(label + " › ").strip()
        return input(label + (f" [{default}]" if default else "") + " › ").strip() or default

    def _commit(self, profile: ProviderProfile, *, activate: bool = True) -> None:
        profile.validate()
        self.store.profiles[profile.id] = profile
        if activate:
            self.store.active = profile.id
        self.store.save()
        if activate or self.config.llm.profile == profile.id:
            self.store.apply(self.config.llm, profile.id)
            self.reload()

    def use(self, name: str) -> None:
        if name not in self.store.profiles:
            raise ValueError(f"连接 {name} 不存在；使用 /model add 添加")
        self._commit(self.store.profiles[name])
        self.emit(f"已切换连接 {name} · 模型 {self.config.llm.model}")

    def run(self) -> None:
        while True:
            llm = self.config.llm
            choices = []
            for p in self.store.profiles.values():
                key = p.active_key or ((p.api_key_env if os.environ.get(p.api_key_env) else "未配置 " + p.api_key_env) if p.api_key_env else "无需密钥")
                marker = "● 当前" if llm.profile == p.id else "○"
                choices.append(ChoiceSpec("use:" + p.id, f"{marker} {p.id}  /  {p.model}  · 密钥 {key}"))
            choices += [ChoiceSpec("add", "＋ 添加供应商连接（官方预设 / 自定义）"),
                        ChoiceSpec("models", "切换模型 / 从供应商刷新模型列表"),
                        ChoiceSpec("keys", "管理 API Key（添加 / 切换 / 删除）"),
                        ChoiceSpec("edit", "编辑当前连接的地址与协议"),
                        ChoiceSpec("remove", "删除已保存连接"),
                        ChoiceSpec("omp", "切换到 omp 后端"), ChoiceSpec("back", "返回对话")]
            selected = self.choose("模型与供应商", choices, subtitle=f"当前 {llm.profile or llm.backend} / {llm.model} · 配置保存在 ~/.trina/providers.json")
            if selected in (None, "back"):
                return
            try:
                if selected.startswith("use:"):
                    self.use(selected[4:])
                    return
                if selected == "omp":
                    self.store.active = ""
                    self.store.save()
                    llm.backend, llm.profile, llm.api = "omp", "", "chat"
                    if "/" not in llm.model:
                        llm.model = f"deepseek/{llm.model}"
                    self.reload()
                    self.emit("已切到 omp 后端")
                    return
                if selected == "add":
                    self.add()
                elif selected == "remove":
                    self.remove()
                else:
                    item = self.current()
                    if selected == "keys":
                        self.keys(item.id)
                    elif selected == "models":
                        model = self.pick_model(item)
                        if model:
                            item.model = model
                            self._commit(item)
                            self.emit(f"已切换模型：{model}")
                    elif selected == "edit":
                        item.base_url = self.ask("Base URL", item.base_url)
                        api = self.choose("接口协议", self.protocol_choices())
                        if api:
                            item.api = api
                            self._commit(item)
                            self.emit("连接配置已保存")
            except ValueError as exc:
                self.emit(str(exc))

    def current(self) -> ProviderProfile:
        name = self.config.llm.profile
        if name not in self.store.profiles:
            raise ValueError("当前模型还没有保存为连接；使用「添加供应商连接」录入")
        return copy.deepcopy(self.store.profiles[name])

    @staticmethod
    def protocol_choices() -> list[ChoiceSpec]:
        return [ChoiceSpec("chat", "OpenAI Chat Completions"), ChoiceSpec("anthropic", "Anthropic Messages"),
                ChoiceSpec("responses", "OpenAI Responses")]

    def add(self, provider_id: str = "") -> None:
        selected = provider_id or self.choose("选择供应商", [ChoiceSpec(p.id, f"{p.name}  · {p.group}" + (f" · {p.note}" if p.note else "")) for p in PRESETS]
                                              + [ChoiceSpec("custom", "自定义供应商 / 兼容网关")])
        if not selected:
            return
        preset = PRESET_BY_ID.get(selected)
        default_id = selected
        suffix = 2
        while default_id in self.store.profiles:
            default_id = f"{selected}-{suffix}"
            suffix += 1
        name = self.ask("连接名称（同一供应商可添加多个连接）", default_id)
        if name in self.store.profiles:
            raise ValueError("连接名称已存在，请使用其他名称")
        base = self.ask("Base URL", preset.base_url if preset else "")
        api = preset.api if preset else self.choose("接口协议", self.protocol_choices())
        if not api:
            return
        item = ProviderProfile(name, selected, base.rstrip("/"), "", preset.api_key_env if preset else "TRINA_LLM_API_KEY", api)
        if not preset or preset.api_key_env:
            self.emit(f"留空使用环境变量 {item.api_key_env}；输入内容不会显示或进入命令历史")
            key = self.ask("API Key", secret=True)
            if key:
                label = self.ask("给这把 Key 命名", "default")
                item.keys[label] = key
                item.active_key = label
        model = self.pick_model(item)
        if not model:
            return
        item.model = model
        self._commit(item)
        self.emit(f"已添加并切换连接 {item.id} · 模型 {model}")

    def pick_model(self, item: ProviderProfile) -> str | None:
        preset = PRESET_BY_ID.get(item.provider)
        models = list(dict.fromkeys(([item.model] if item.model else []) + list(preset.models if preset else ())))
        while True:
            choices = [ChoiceSpec("model:" + m, m) for m in models]
            choices += [ChoiceSpec("refresh", "↻ 从供应商刷新模型列表"), ChoiceSpec("manual", "手动输入模型 ID / 部署名")]
            selected = self.choose(f"选择模型 · {item.id}", choices, subtitle=item.base_url)
            if selected is None:
                return None
            if selected.startswith("model:"):
                return selected[6:]
            if selected == "manual":
                return self.ask("模型 ID（保留供应商返回的完整名称）", item.model) or None
            if selected == "refresh":
                try:
                    from .config import LLMConfig
                    from .llm.openai_backend import OpenAICompatBackend
                    key = item.keys.get(item.active_key, "") if item.active_key else os.environ.get(item.api_key_env, "")
                    cfg = LLMConfig(backend="openai", base_url=item.base_url, api=item.api, timeout=20)
                    models = OpenAICompatBackend(cfg, key_override=key).list_remote_models()
                    self.emit(f"已获取 {len(models)} 个模型" if models else "供应商没有返回可用模型，可手动输入 ID")
                except ValueError as exc:
                    self.emit(f"刷新失败：{exc}；可继续使用预设或手动输入")

    def keys(self, name: str = "") -> None:
        item = copy.deepcopy(self.store.profiles.get(name)) if name else self.current()
        if item is None:
            raise ValueError("连接不存在")
        choices = [ChoiceSpec("key:" + k, f"{'●' if k == item.active_key else '○'} {k} · 已保存") for k in item.keys]
        choices += [ChoiceSpec("add", "＋ 添加一把 API Key"),
                    ChoiceSpec("env", f"使用环境变量 {item.api_key_env or '（本地免密）'}"),
                    ChoiceSpec("remove", "删除一把已保存的 Key")]
        selected = self.choose(f"API Key · {item.id}", choices, subtitle="仅显示密钥名称；选择后立即生效")
        if selected is None:
            return
        if selected.startswith("key:"):
            item.active_key = selected[4:]
        elif selected == "env":
            item.active_key = ""
        elif selected == "add":
            label = self.ask("Key 名称", "key-" + str(len(item.keys) + 1))
            if label in item.keys:
                raise ValueError("Key 名称已存在；使用新名称添加，原密钥仍可保留")
            key = self.ask("API Key", secret=True)
            if not key:
                return
            item.keys[label], item.active_key = key, label
        elif selected == "remove":
            label = self.choose("删除哪把 Key", [ChoiceSpec(k, k) for k in item.keys])
            if not label:
                return
            del item.keys[label]
            if item.active_key == label:
                item.active_key = ""
        self._commit(item, activate=self.config.llm.profile == item.id)
        self.emit(f"{item.id} 密钥设置已保存 · 当前 {item.active_key or '环境变量 / 免密'}")

    def remove(self) -> None:
        name = self.choose("删除哪个连接", [ChoiceSpec(p.id, p.id) for p in self.store.profiles.values()])
        if not name:
            return
        if self.choose("确认删除连接及其保存的密钥", [ChoiceSpec("back", "返回"), ChoiceSpec("delete", f"删除 {name}")]) != "delete":
            return
        if self.config.llm.profile == name:
            raise ValueError("请先切换到其他连接或 omp，再删除当前连接")
        del self.store.profiles[name]
        if self.store.active == name:
            self.store.active = ""
        self.store.save()
        self.emit(f"已删除连接 {name}")
