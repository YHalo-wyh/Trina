"""模型管理页：供应商、模型与命名密钥的可取消交互流程。"""
from __future__ import annotations

import copy
import os
from typing import Callable

from .console_ui import ChoiceSpec, edit_field, enhanced_input_available, select_option, show_secret
from .providers import PRESETS, PRESET_BY_ID, ProfileStore, ProviderProfile


class ModelManager:
    def __init__(self, config, *, emit: Callable[[str], None], reload: Callable[[], None],
                 ask=None, choose=None, reveal=None, store: ProfileStore | None = None):
        self.config, self.emit, self.reload = config, emit, reload
        self.store = store or ProfileStore()
        self.ask = ask or self._ask
        self.choose = choose or select_option
        self.reveal = reveal or show_secret

    @staticmethod
    def _ask(label: str, default: str = "", *, secret: bool = False) -> str:
        if enhanced_input_available():
            return edit_field(label, default, secret=secret)
        if secret:
            import getpass
            return getpass.getpass(label + " › ").strip()
        return input(label + (f" [{default}]" if default else "") + " › ").strip() or default

    def _commit(self, profile: ProviderProfile, *, activate: bool = True) -> None:
        from .llm.reasoning import reasoning_support
        probe = copy.copy(self.config.llm)
        probe.backend, probe.model, probe.base_url, probe.api = "openai", profile.model, profile.base_url, profile.api
        if profile.reasoning_effort not in reasoning_support(probe).levels:
            profile.reasoning_effort = "default"
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

    def status_text(self) -> str:
        """模型页首屏数据；这里永远不包含明文 Key。"""
        from .llm.reasoning import reasoning_support
        llm = self.config.llm
        item = self.store.profiles.get(llm.profile)
        provider = item.provider if item else (llm.model.split("/", 1)[0] if llm.backend == "omp" else
                                               next((p.id for p in PRESETS if p.base_url.rstrip("/") == llm.base_url.rstrip("/")), "custom"))
        preset = PRESET_BY_ID.get(provider)
        name = preset.name if preset else provider
        if llm.backend == "omp":
            key_status = "由 omp 管理"
        else:
            label, key = self.key_info()
            key_status = f"{label}  ••••••••" if key else "未配置"
        support = reasoning_support(llm)
        effort = support.label(llm.reasoning_effort)
        return f"厂商    {name}\n模型    {llm.model}\n思考    {effort}\nAPI Key {key_status}"

    def key_info(self, item: ProviderProfile | None = None) -> tuple[str, str]:
        item = item or self.store.profiles.get(self.config.llm.profile)
        if item:
            if item.active_key:
                return item.active_key, item.keys[item.active_key]
            return item.api_key_env, os.environ.get(item.api_key_env, "")
        return self.config.llm.api_key_env, os.environ.get(self.config.llm.api_key_env, "")

    def view_key(self) -> None:
        if self.config.llm.backend != "openai":
            self.emit("当前 Key 由 omp 管理")
            return
        label, key = self.key_info()
        if key:
            self.reveal(f"API Key · {label}", key)
        else:
            self.emit("尚未配置 API Key")

    def run(self) -> None:
        notice = ""
        while True:
            choices = [ChoiceSpec("models", "切换模型"), ChoiceSpec("providers", "切换厂商"),
                       ChoiceSpec("reasoning", "思考强度"), ChoiceSpec("reveal", "查看当前 API Key"),
                       ChoiceSpec("keys", "切换 API Key"), ChoiceSpec("addkey", "添加 API Key"),
                       ChoiceSpec("removekey", "删除 API Key"), ChoiceSpec("add", "添加厂商"),
                       ChoiceSpec("edit", "连接设置"), ChoiceSpec("remove", "删除厂商连接"), ChoiceSpec("back", "返回")]
            selected = self.choose("Model", choices, subtitle=self.status_text() + ("\n" + notice if notice else ""))
            notice = ""
            if selected in (None, "back"):
                return
            try:
                if selected == "providers":
                    self.pick_provider()
                elif selected == "reveal":
                    self.view_key()
                elif selected == "reasoning":
                    from .llm.reasoning import reasoning_support
                    support = reasoning_support(self.config.llm)
                    level = self.choose("思考强度", [ChoiceSpec(x, ("● " if x == self.config.llm.reasoning_effort else "  ") + support.label(x)) for x in support.levels],
                                        subtitle="此模型未声明可调思考参数" if support.mode == "none" else self.config.llm.model)
                    if level:
                        self.set_reasoning(level)
                elif selected == "add":
                    self.add()
                elif selected == "remove":
                    self.remove()
                elif selected == "models" and self.config.llm.backend == "omp":
                    self.pick_omp_model()
                else:
                    item = self.current()
                    if selected in ("keys", "addkey", "removekey"):
                        self.keys(item.id if item.id in self.store.profiles else "", action={"addkey": "add", "removekey": "remove"}.get(selected, ""))
                    elif selected == "models":
                        model = self.pick_model(item)
                        if model:
                            item.model = model
                            self._commit(item)
                    elif selected == "edit":
                        item.base_url = self.ask("Base URL", item.base_url)
                        api = self.choose("接口协议", self.protocol_choices())
                        if api:
                            item.api = api
                            self._commit(item)
            except ValueError as exc:
                notice = str(exc)
            except KeyboardInterrupt:
                continue

    def set_reasoning(self, level: str) -> None:
        from .llm.reasoning import reasoning_support
        llm = self.config.llm
        if level not in reasoning_support(llm).levels:
            raise ValueError("当前模型不支持该思考档位")
        if llm.backend == "openai":
            item = self.current()
            item.reasoning_effort = level
            self._commit(item)
        elif llm.backend == "omp":
            self.store.active = ""
            self.store.reasoning_effort = level
            self.store.omp_model = llm.model
            self.store.save()
            llm.reasoning_effort = level
            self.reload()

    def pick_provider(self) -> None:
        choices = [ChoiceSpec("use:" + p.id, f"{p.id} · {p.model}") for p in self.store.profiles.values()]
        choices += [ChoiceSpec("add:" + p.id, p.name) for p in PRESETS]
        choices += [ChoiceSpec("omp", "omp"), ChoiceSpec("add:custom", "自定义厂商")]
        selected = self.choose("厂商", choices)
        if selected and selected.startswith("use:"):
            self.use(selected[4:])
        elif selected and selected.startswith("add:"):
            self.add(selected[4:])
        elif selected == "omp":
            self.pick_omp_model()

    def pick_omp_model(self) -> None:
        from .models import list_models
        selected = self.choose("模型 · omp", [ChoiceSpec(m.spec, m.render()) for m in list_models(limit=500)])
        if selected:
            llm = self.config.llm
            self.store.active, self.store.omp_model = "", selected
            self.store.save()
            llm.backend, llm.profile, llm.model, llm.api = "omp", "", selected, "chat"
            llm.reasoning_effort = self.store.reasoning_effort
            self.reload()

    def current(self) -> ProviderProfile:
        llm = self.config.llm
        if llm.profile in self.store.profiles:
            return copy.deepcopy(self.store.profiles[llm.profile])
        if llm.backend != "openai":
            raise ValueError("选择厂商后可在此管理 API Key")
        provider = next((p.id for p in PRESETS if p.base_url.rstrip("/") == llm.base_url.rstrip("/")), "custom")
        name, suffix = provider, 2
        while name in self.store.profiles:
            name, suffix = f"{provider}-{suffix}", suffix + 1
        return ProviderProfile(name, provider, llm.base_url, llm.model, llm.api_key_env, llm.api,
                               reasoning_effort=llm.reasoning_effort)

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

    def keys(self, name: str = "", *, action: str = "") -> None:
        item = copy.deepcopy(self.store.profiles.get(name)) if name else self.current()
        if item is None:
            raise ValueError("连接不存在")
        choices = [ChoiceSpec("key:" + k, f"{'●' if k == item.active_key else '○'} {k} · 已保存") for k in item.keys]
        choices += [ChoiceSpec("add", "＋ 添加一把 API Key"),
                    ChoiceSpec("env", f"使用环境变量 {item.api_key_env or '（本地免密）'}"),
                    ChoiceSpec("view", "查看一把 Key"), ChoiceSpec("remove", "删除一把已保存的 Key")]
        selected = action or self.choose(f"API Key · {item.id}", choices, subtitle="当前 " + (item.active_key or "环境变量"))
        if selected is None:
            return
        if selected == "view":
            label = self.choose("查看哪把 Key", [ChoiceSpec(k, k) for k in item.keys])
            if label:
                self.reveal(f"API Key · {label}", item.keys[label])
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
        self._commit(item, activate=self.config.llm.profile == item.id or not name)
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
