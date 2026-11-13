"""供应商整条配置的单页草稿编辑；测试与取消不写入存储。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Callable

from .console_ui import _panel_app
from .providers import PRESET_BY_ID, ProviderProfile, normalize_endpoint
from .terminal import clean_text


class ProviderEditor:
    def __init__(self, profile: ProviderProfile, *, is_new: bool,
                 test_connection: Callable[[ProviderProfile], str], test_model: Callable[[ProviderProfile], str],
                 validate: Callable[[ProviderProfile], None] | None = None):
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.filters import Condition, has_focus
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import Button, TextArea

        self.original = replace(profile)
        self.visible, self.busy, self.revision = False, False, 0
        self.message = ""
        self.api = profile.api
        self._test_connection, self._test_model = test_connection, test_model
        self.validate = validate or (lambda _: None)
        self.fields = {
            name: TextArea(text=value, multiline=False, height=1, wrap_lines=False,
                           password=Condition(lambda: not self.visible) if name == "api_key" else False,
                           style="class:field", name=name)
            for name, value in (("id", profile.id), ("base_url", profile.base_url),
                                ("api_key", profile.api_key), ("model", profile.model), ("note", profile.note),
                                ("context_window", str(profile.context_window) if profile.context_window else "auto"))
        }
        preset = PRESET_BY_ID.get(profile.provider)
        self.fields["model"].buffer.completer = WordCompleter(
            list(dict.fromkeys(([profile.model] if profile.model else []) + list(preset.models if preset else ()))),
            ignore_case=True, sentence=True, match_middle=True)
        for field in self.fields.values():
            field.buffer.on_text_changed += self._changed

        def toggle_key():
            self.visible = not self.visible
            reveal.text = "隐藏 Key" if self.visible else "显示 Key"

        def cycle_protocol():
            protocols = ("chat", "responses", "anthropic")
            self.api = protocols[(protocols.index(self.api) + 1) % len(protocols)]
            protocol.text = protocol_label()
            self._changed()

        def protocol_label():
            return {"chat": "协议：OpenAI Chat", "responses": "协议：OpenAI Responses", "anthropic": "协议：Anthropic Messages"}[self.api]

        protocol = Button(protocol_label(), handler=cycle_protocol, width=32)
        reveal = Button("显示 Key", handler=toggle_key, width=10)
        probe = Button("连通测试", handler=lambda: self._start_test(False), width=10)
        model = Button("模型验证", handler=lambda: self._start_test(True), width=10)
        save = Button("保存", handler=self._save, width=10)
        cancel = Button("取消", handler=lambda: self.app.exit(result=None), width=10)
        bindings = KeyBindings()
        @bindings.add("tab")
        def next_field(event):
            event.app.layout.focus_next()
        @bindings.add("s-tab")
        def previous_field(event):
            event.app.layout.focus_previous()
        @bindings.add("enter", filter=has_focus(self.fields["id"]) | has_focus(self.fields["base_url"]) | has_focus(self.fields["api_key"]) | has_focus(self.fields["model"]) | has_focus(self.fields["note"]) | has_focus(self.fields["context_window"]))
        def enter_field(event):
            buffer = event.app.current_buffer
            if buffer.complete_state and buffer.complete_state.current_completion:
                buffer.apply_completion(buffer.complete_state.current_completion)
            else:
                buffer.cancel_completion()
                event.app.layout.focus_next()
        @bindings.add("c-s")
        def accept(event):
            self._save()
        @bindings.add("f2")
        def reveal_key(event):
            toggle_key()
        @bindings.add("f5")
        def test_address(event):
            self._start_test(False)
        @bindings.add("f6")
        def test_model_action(event):
            self._start_test(True)
        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-d")
        def close(event):
            event.app.exit(result=None)

        body = [Window(FormattedTextControl("预设 · " + (preset.name if preset else "自定义")), height=1)]
        for name, label in (("id", "供应商名称"), ("base_url", "请求地址"),
                            ("api_key", "API Key（可留空）"),
                            ("model", "模型"), ("note", "备注（可选）"), ("context_window", "上下文窗口（auto / 500K / 1M）")):
            body.extend([Window(FormattedTextControl(label), height=1), self.fields[name]])
        body += [VSplit([protocol, Window()]), VSplit([reveal, probe, model, Window()], padding=1), VSplit([save, cancel, Window()], padding=1),
                 Window(FormattedTextControl(lambda: clean_text(self.message)), height=2, wrap_lines=True),
                 Window(FormattedTextControl("Tab 切换 · Ctrl+S 保存 · Esc 取消"), height=1)]
        self.app = _panel_app(HSplit(body), "添加供应商" if is_new else "编辑供应商", bindings, self.fields["id"], completion_menu=True)

    def _changed(self, _=None) -> None:
        self.revision += 1
        self.message = "配置已修改，请重新测试" if self.busy else ""

    def draft(self) -> ProviderProfile:
        values = {name: field.text.strip() for name, field in self.fields.items()}
        from .model_context import parse_context_window
        values["context_window"] = parse_context_window(values["context_window"])
        values["base_url"], api = normalize_endpoint(values["base_url"], self.api)
        return replace(self.original, **values, api=api)

    def _save(self) -> None:
        try:
            item = self.draft()
            item.validate()
            self.validate(item)
        except ValueError as exc:
            self.message = str(exc)
        else:
            self.app.exit(result=item)

    def _start_test(self, model: bool) -> None:
        if self.busy:
            return
        try:
            item = self.draft()
        except ValueError as exc:
            self.message = str(exc)
            return
        self.busy = True
        revision = self.revision
        self.message = "正在验证 Key 和模型…" if model else "正在测试地址连通性…"
        callback = self._test_model if model else self._test_connection
        async def perform():
            try:
                result = await asyncio.to_thread(callback, item)
            except Exception as exc:
                result = f"测试失败：{exc}"
            finally:
                self.busy = False
            if item.api_key:
                result = result.replace(item.api_key, "<redacted>")
            self.message = result if revision == self.revision else "配置已修改，请重新测试"
            self.app.invalidate()
        self.app.create_background_task(perform())

    def run(self) -> ProviderProfile | None:
        return self.app.run()
