"""交互控制台的命令目录、斜杠补全与增强输入框。

本模块只负责展示和输入，不执行命令。命令最终仍由 ``trina.interactive``
分发到原有控制器、护栏与审计链。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

try:
    from prompt_toolkit.completion import Completer as _Completer
except ImportError:  # pragma: no cover - 缺少可选输入库时仍允许普通 input 启动
    class _Completer:  # type: ignore[no-redef]
        pass


@dataclass(frozen=True)
class ChoiceSpec:
    """一个可补全的参数值。"""

    value: str
    description: str = ""


@dataclass(frozen=True)
class OptionSpec:
    """命令选项及其值提示。"""

    flags: tuple[str, ...]
    description: str
    value: str = ""
    choices: tuple[ChoiceSpec, ...] = ()
    repeatable: bool = False

    @property
    def primary(self) -> str:
        return self.flags[0]

    def render(self) -> str:
        label = ", ".join(self.flags)
        if self.value:
            label += f" {self.value}"
        return f"{label:<28} {self.description}"


@dataclass(frozen=True)
class CommandSpec:
    """斜杠命令的唯一展示元数据。"""

    name: str
    category: str
    description: str
    usage: str
    options: tuple[OptionSpec, ...] = ()
    positional_choices: tuple[ChoiceSpec, ...] = ()
    examples: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    availability: str = "idle"  # idle | running | always


APPROVAL_CHOICES = tuple(
    ChoiceSpec(value, description)
    for value, description in (
        ("off", "关闭逐步审批"),
        ("exploit", "仅利用级动作需要审批"),
        ("verify", "验证及利用级动作需要审批"),
        ("all", "每个工具动作都需要审批"),
    )
)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "play", "任务", "自动准备靶场并完成一次完整任务", "play [选项]",
        options=(
            OptionSpec(("--target", "-t"), "指定 URL 或 host:port", "<目标>"),
            OptionSpec(("--range", "-r"), "使用已登记的靶场", "<名称>"),
            OptionSpec(("--goal", "-g"), "描述本轮验收目标", "<目标>"),
            OptionSpec(("--max-steps",), "限制决策步数", "<整数>"),
            OptionSpec(("--approve",), "设置审批闸门", "<模式>", APPROVAL_CHOICES),
        ),
        examples=(
            '/play --goal "拿到 flag 并验证 SQL 注入" --max-steps 8',
            "/play --range lab --approve exploit",
        ),
    ),
    CommandSpec(
        "run", "任务", "对指定目标运行完整 Agent", "run <目标> [选项]",
        options=(
            OptionSpec(("--goal", "-g"), "描述本轮验收目标", "<目标>"),
            OptionSpec(("--max-steps",), "限制决策步数", "<整数>"),
            OptionSpec(("--approve",), "设置审批闸门", "<模式>", APPROVAL_CHOICES),
        ),
        examples=('/run http://127.0.0.1:9099 --goal "拿到 flag"',),
    ),
    CommandSpec("goal", "任务", "保存或修改当前项目的任务目标", "goal [set <目标>|show|clear]",
                positional_choices=(ChoiceSpec("show", "查看目标"), ChoiceSpec("set", "设置目标"), ChoiceSpec("clear", "清除目标")),
                examples=('/goal 检查登录流程并记录证据',), availability="always"),
    CommandSpec("schedule", "任务", "定时启动任务", "schedule <add|list|cancel> [选项]",
                aliases=("scheduled", "schedualed"), availability="always",
                positional_choices=(ChoiceSpec("add", "安排任务"), ChoiceSpec("list", "查看任务"), ChoiceSpec("cancel", "取消任务")),
                options=(OptionSpec(("--at",), "开始时间", "<HH:MM|日期时间|+10m>"),
                         OptionSpec(("--target",), "执行目标", "<地址>"), OptionSpec(("--goal",), "任务目标", "<文本>"),
                         OptionSpec(("--max-steps",), "决策步数", "<整数>"), OptionSpec(("--approve",), "审批模式", "<模式>", APPROVAL_CHOICES)),
                examples=('/schedule add --at +10m --target http://127.0.0.1:9099',)),
    CommandSpec("status", "任务", "查看当前任务或上一轮结果", "status", availability="always"),
    CommandSpec("board", "任务", "打开状态、证据、覆盖和成本工作台", "board", availability="always"),
    CommandSpec("hypotheses", "研究", "按优先级查看假设台账", "hypotheses"),
    CommandSpec("graph", "研究", "输出证据关系图（Mermaid）", "graph"),
    CommandSpec(
        "tools", "工具", "列出已注册工具及参数", "tools [--verbose|--json]",
        options=(
            OptionSpec(("--verbose", "-v"), "显示完整参数表"),
            OptionSpec(("--json",), "输出 JSON"),
        ),
    ),
    CommandSpec(
        "tool", "工具", "执行单个已注册工具", "tool <工具名> [选项]",
        options=(
            OptionSpec(("--target",), "便捷设置 target/url", "<目标>"),
            OptionSpec(("--params",), "传入 JSON 参数对象", "<JSON>"),
            OptionSpec(("--param",), "传入一个 key=value 参数", "<键=值>", repeatable=True),
        ),
        examples=("/tool port_scan --target 127.0.0.1",),
    ),
    CommandSpec(
        "guard", "工具", "检查目标或外部命令是否满足护栏", "guard <check|summary|argv> ...",
        positional_choices=(
            ChoiceSpec("check", "检查一个或多个目标"),
            ChoiceSpec("summary", "显示当前授权边界"),
            ChoiceSpec("argv", "检查外部命令参数"),
        ),
        examples=("/guard check 127.0.0.1",),
    ),
    CommandSpec(
        "ranges", "工具", "列出靶场或检查靶场状态", "ranges [list|status <名称>]",
        positional_choices=(ChoiceSpec("list", "列出靶场"), ChoiceSpec("status", "检查指定靶场")),
        examples=("/ranges status lab",),
    ),
    CommandSpec(
        "lab", "工具", "启动故意脆弱的本地实验靶机", "lab [选项]",
        options=(
            OptionSpec(("--http",), "启动 HTTP 靶机"),
            OptionSpec(("--ssh",), "启动 SSH 靶机"),
            OptionSpec(("--port",), "HTTP 监听端口", "<端口>"),
            OptionSpec(("--ssh-port",), "SSH 监听端口", "<端口>"),
        ),
    ),
    CommandSpec(
        "model", "会话与模型", "以供应商为单位管理地址、Key 和模型", "model [add|edit|remove|test|use <供应商>|list|模型规格]",
        positional_choices=(ChoiceSpec("add", "添加供应商配置"), ChoiceSpec("edit", "编辑供应商整条配置"),
                            ChoiceSpec("remove", "删除供应商配置"), ChoiceSpec("test", "测试供应商地址连通性"),
                            ChoiceSpec("use", "切换供应商"), ChoiceSpec("list", "列出模型与供应商")),
        examples=("/model", "/model add", "/model edit", "/model test"),
    ),
    CommandSpec(
        "approve", "会话与模型", "调整动作审批闸门", "approve <off|exploit|verify|all>",
        positional_choices=APPROVAL_CHOICES,
    ),
    CommandSpec("usage", "会话与模型", "查看模型调用、Token 和成本", "usage"),
    CommandSpec(
        "history", "会话与模型", "查看最近的对话与工具摘要", "history [条数]",
        examples=("/history 12",),
    ),
    CommandSpec("new", "会话与模型", "清空上下文并开启新会话", "new", aliases=("clear",)),
    CommandSpec("save", "会话与模型", "持久化当前供应商与模型设置", "save"),
    CommandSpec(
        "report", "交付", "由运行状态重建报告", "report rebuild --state <state.json>",
        positional_choices=(ChoiceSpec("rebuild", "从 state.json 重建 Markdown/HTML 报告"),),
        options=(OptionSpec(("--state",), "运行状态文件", "<state.json>"),),
    ),
    CommandSpec(
        "review", "交付", "只读复核运行声明、证据与覆盖", "review [选项]",
        options=(
            OptionSpec(("--state",), "指定运行状态文件", "<state.json>"),
            OptionSpec(("--audit",), "指定审计日志", "<日志>"),
            OptionSpec(("--json",), "输出 JSON"),
            OptionSpec(("--save",), "保存复核记录", "<目录>"),
            OptionSpec(("--no-registry",), "不加载注册表"),
        ),
    ),
    CommandSpec(
        "audit", "交付", "查看或校验审计哈希链", "audit <verify|show|tail> <日志> [--limit N]",
        positional_choices=(
            ChoiceSpec("verify", "校验哈希链完整性"),
            ChoiceSpec("show", "显示审计摘要"),
            ChoiceSpec("tail", "输出最近记录"),
        ),
        options=(OptionSpec(("--limit",), "限制显示条数", "<整数>"),),
    ),
    CommandSpec(
        "hint", "运行控制", "向运行中的 Agent 注入下一步提示", "hint <文本>",
        examples=("/hint 优先验证 /sqli 的 id 参数",), availability="running",
    ),
    CommandSpec("pause", "运行控制", "暂停当前任务", "pause", availability="running"),
    CommandSpec("resume", "运行控制", "继续已暂停任务", "resume", availability="running"),
    CommandSpec("stop", "运行控制", "安全请求停止当前任务", "stop", availability="running"),
    CommandSpec(
        "revoke", "运行控制", "撤销注册目标授权", "revoke <target_id>", availability="always",
    ),
    CommandSpec(
        "grant", "运行控制", "恢复注册目标授权", "grant <target_id>", availability="always",
    ),
    CommandSpec(
        "help", "系统", "显示命令索引或某条命令详情", "help [命令]",
        aliases=("?",), availability="always",
    ),
    CommandSpec(
        "quit", "系统", "退出控制台；运行中第一次会提示先停止", "quit",
        aliases=("exit", "q"), availability="always",
    ),
)

CATEGORY_ORDER = ("任务", "研究", "工具", "会话与模型", "交付", "运行控制", "系统")
COMMAND_BY_NAME = {spec.name: spec for spec in COMMANDS}
for _spec in COMMANDS:
    for _alias in _spec.aliases:
        COMMAND_BY_NAME[_alias] = _spec


def command_names() -> set[str]:
    """返回命令和别名集合，供自然语言/命令分流复用。"""
    return set(COMMAND_BY_NAME)


def find_command(name: str) -> CommandSpec | None:
    return COMMAND_BY_NAME.get((name or "").lstrip("/").strip())


def command_help_sections() -> list[tuple[str, str]]:
    """生成分类帮助页，和补全菜单共享同一目录。"""
    sections: list[tuple[str, str]] = []
    for category in CATEGORY_ORDER:
        specs = [item for item in COMMANDS if item.category == category]
        if not specs:
            continue
        body = "\n".join(f"/{item.name:<13} {item.description}" for item in specs)
        sections.append((category, body))
    return sections


def command_detail_sections(spec: CommandSpec) -> list[tuple[str, str]]:
    """生成单条命令的用法、选项和示例。"""
    sections = [("用途", spec.description), ("用法", f"/{spec.usage}")]
    if spec.positional_choices:
        sections.append(("可选值", "\n".join(f"{item.value:<20} {item.description}" for item in spec.positional_choices)))
    if spec.options:
        sections.append(("选项", "\n".join(item.render() for item in spec.options)))
    if spec.examples:
        sections.append(("示例", "\n".join(spec.examples)))
    return sections


def command_hint(text: str) -> str:
    """根据输入中的命令生成底栏提示；没在输入斜杠命令时不占一行。"""
    line = (text or "").strip()
    if not line.startswith("/"):
        return ""
    body = line[1:].strip()
    if not body:
        return "继续输入筛选命令 · ↑↓ 选择 · Tab 补全 · Enter 执行"
    spec = find_command(body.split(None, 1)[0])
    if spec is None:
        matches = [item for item in COMMANDS if item.name.startswith(body)]
        if matches:
            return "匹配 " + " · ".join(f"/{item.name} {item.description}" for item in matches[:3])
        return "未找到命令；继续输入筛选，或使用 /help"
    options = " ".join(item.primary for item in spec.options[:4])
    option_hint = f" · 选项 {options}" if options else ""
    return f"/{spec.usage}{option_hint}  ·  {spec.description}"


DynamicValues = Callable[[str, list[str]], Iterable[ChoiceSpec]]


class SlashCommandCompleter(_Completer):
    """prompt_toolkit 补全器；库缺失时仍可安全导入本模块。"""

    def __init__(
        self,
        *,
        running: Callable[[], bool] | None = None,
        dynamic_values: DynamicValues | None = None,
    ) -> None:
        self.running = running or (lambda: False)
        self.dynamic_values = dynamic_values or (lambda _command, _args: ())

    def _visible_commands(self) -> list[CommandSpec]:
        running = self.running()
        wanted = "running" if running else "idle"
        return [item for item in COMMANDS if item.availability in (wanted, "always")]

    @staticmethod
    def _choice_completion(choice: ChoiceSpec, prefix: str):
        import shlex
        from prompt_toolkit.completion import Completion

        return Completion(
            shlex.quote(choice.value),
            start_position=-len(prefix),
            display=choice.value,
            display_meta=choice.description,
        )

    def get_completions(self, document, _complete_event):  # prompt_toolkit 协议
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:  # pragma: no cover - 正式依赖缺失时由普通 input 降级
            return
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        if not body or not any(ch.isspace() for ch in body):
            prefix = body.lower()
            for spec in sorted(self._visible_commands(), key=lambda s: (len(s.name), s.name)) if prefix else self._visible_commands():
                candidates = (spec.name, *spec.aliases)
                if prefix and not any(item.startswith(prefix) for item in candidates):
                    continue
                yield Completion(
                    spec.name,
                    start_position=-len(body),
                    display=f"/{spec.name}",
                    display_meta=f"{spec.category} · {spec.description}",
                )
            return

        trailing_space = body[-1:].isspace()
        parts = body.split()
        command = parts[0] if parts else ""
        spec = find_command(command)
        if spec is None:
            return
        if trailing_space:
            completed_args, prefix = parts[1:], ""
        else:
            completed_args, prefix = parts[1:-1], parts[-1]

        # 支持 --approve=v 这类等号形式。
        if prefix.startswith("--") and "=" in prefix:
            flag, value_prefix = prefix.split("=", 1)
            option = next((item for item in spec.options if flag in item.flags), None)
            if option:
                for choice in option.choices:
                    if choice.value.startswith(value_prefix):
                        yield Completion(
                            choice.value,
                            start_position=-len(value_prefix),
                            display=choice.value,
                            display_meta=choice.description,
                        )
            return

        previous = completed_args[-1] if completed_args else ""
        value_option = next((item for item in spec.options if previous in item.flags), None)
        if value_option is not None:
            choices = list(value_option.choices) or list(self.dynamic_values(spec.name, completed_args))
            for choice in choices:
                if not prefix or choice.value.lower().startswith(prefix.lower()):
                    yield self._choice_completion(choice, prefix)
            return

        # 第一位置参数：子命令、模式、模型或工具名。
        if not completed_args and not prefix.startswith("-"):
            choices = [*spec.positional_choices, *self.dynamic_values(spec.name, completed_args)]
            seen: set[str] = set()
            for choice in choices:
                if choice.value in seen or (prefix and not choice.value.lower().startswith(prefix.lower())):
                    continue
                seen.add(choice.value)
                yield self._choice_completion(choice, prefix)
            if choices:
                return

        # 更深层级的动态值，例如 /ranges status <名称>。
        dynamic = list(self.dynamic_values(spec.name, completed_args))
        if dynamic and not prefix.startswith("-"):
            for choice in dynamic:
                if not prefix or choice.value.lower().startswith(prefix.lower()):
                    yield self._choice_completion(choice, prefix)
            return

        if prefix and not prefix.startswith("-"):
            return
        used = set(completed_args)
        for option in spec.options:
            if not option.repeatable and any(flag in used for flag in option.flags):
                continue
            if prefix and not any(flag.startswith(prefix) for flag in option.flags):
                continue
            yield Completion(
                option.primary,
                start_position=-len(prefix),
                display=option.primary + (f" {option.value}" if option.value else ""),
                display_meta=option.description,
            )


def enhanced_input_available() -> bool:
    """只有真实双向 TTY 才启用索引输入，重定向继续使用普通 input。"""
    if not bool(getattr(sys.stdin, "isatty", lambda: False)()):
        return False
    if not bool(getattr(sys.stdout, "isatty", lambda: False)()):
        return False
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        return False
    return True


class EnhancedConsoleInput:
    """可滚动内容区与固定底部的活动、输入、索引和会话用量。"""

    def __init__(self, *, status, running, dynamic_values, plain: bool = False, usage=None, activity=None) -> None:
        from prompt_toolkit import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, Window, ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.styles import Style
        from prompt_toolkit.output import ColorDepth
        from .terminal import THEME

        self.status = status
        self.usage = usage or (lambda _width, _compact=False: "")
        self.activity = activity or (lambda: ("processing", "运行中", 0) if running() else ("idle", "就绪", 0))
        self._handler = None
        self._dispatching = False
        self._queue = None
        self._chunks: list[str] = []
        self._stream_index: int | None = None
        self._plain = plain
        self._char_count = 0
        self._content_lock = threading.RLock()
        self._fragments = None
        self._fragment_width = 0
        self._line_count = 1
        #: 最近一次渲染出的真实行数；光标越界会直接把 prompt_toolkit 的滚动逻辑打崩。
        self._rendered_lines = 1
        self._scrollback = 0
        self.running = running
        self.completer = SlashCommandCompleter(running=running, dynamic_values=dynamic_values)
        self.dismissed = False
        self.buffer = Buffer(history=InMemoryHistory(), completer=self.completer,
                             complete_while_typing=True, multiline=False)
        self.buffer.on_text_changed += lambda _: self._reopen()
        bindings = KeyBindings()

        @bindings.add("tab")
        def complete(event):
            items = self._items()
            if items:
                state = self.buffer.complete_state
                self.buffer.apply_completion(state.current_completion if state and state.current_completion else items[0])

        @bindings.add("up")
        @bindings.add("s-tab")
        def previous(event):
            self._move(-1)

        @bindings.add("down")
        def next_item(event):
            self._move(1)

        @bindings.add("escape")
        def dismiss(event):
            self.buffer.cancel_completion()
            self.dismissed = True

        @bindings.add("enter")
        def submit(event):
            state = self.buffer.complete_state
            text = self.buffer.text.strip()
            if text.startswith("/") and state and state.current_completion:
                self.buffer.apply_completion(state.current_completion)
            elif text.startswith("/") and not any(c.isspace() for c in text) and not find_command(text):
                items = self._items()
                if items:
                    self.buffer.apply_completion(items[0])
            value = self.buffer.text
            if self._handler is None:
                event.app.exit(result=value)
            else:
                self.buffer.append_to_history()
                self.buffer.reset()
                self._queue.put_nowait(value)

        @bindings.add("c-c")
        def cancel(event):
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d")
        def eof(event):
            if not self.buffer.text:
                event.app.exit(exception=EOFError())
            else:
                self.buffer.delete()

        @bindings.add("pageup")
        def page_up(event):
            self._scroll_by(self._page_size())

        @bindings.add("pagedown")
        def page_down(event):
            self._scroll_by(-self._page_size())

        @bindings.add("c-up")
        def line_up(event):
            self._scroll_by(1)

        @bindings.add("c-down")
        def line_down(event):
            self._scroll_by(-1)

        @bindings.add("c-home")
        def oldest(event):
            self._scroll_to_oldest()

        @bindings.add("c-end")
        def follow_latest(event):
            self._scroll_to_latest()

        style = Style.from_dict({} if plain else {
            "prompt": "bold " + THEME["rose"], "index": THEME["muted"],
            "selected": "bg:" + THEME["selected"] + " " + THEME["text"] + " bold", "hint": THEME["muted"],
            "status": THEME["lilac"], "title": "bold " + THEME["rose"],
            "thinking": "bold " + THEME["rose"], "thinking.dim": THEME["violet"],
            "border": THEME["violet"], "usage": THEME["muted"], "usage.value": THEME["lilac"],
        })
        control = BufferControl(buffer=self.buffer, input_processors=[BeforeInput(
            lambda: [("class:prompt", "补充  › " if self.running() else "Trina  › ")])])
        owner = self

        class TranscriptControl(FormattedTextControl):
            """把滚轮事件交给会话视口，而不是输入框或终端原生回滚。"""

            def mouse_handler(control, mouse_event):
                from prompt_toolkit.mouse_events import MouseEventType

                if mouse_event.event_type == MouseEventType.SCROLL_UP:
                    owner._scroll_by(3)
                    return None
                if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                    owner._scroll_by(-3)
                    return None
                return super().mouse_handler(mouse_event)

        self.transcript_control = TranscriptControl(
            self._transcript,
            show_cursor=False,
            get_cursor_position=lambda: Point(x=0, y=max(0, self._cursor_line())),
        )
        self.output_window = Window(self.transcript_control, wrap_lines=True, always_hide_cursor=True)
        self.activity_window = Window(FormattedTextControl(self._activity), height=1)
        self.input_window = Window(control, wrap_lines=True, height=Dimension(min=1, max=3), dont_extend_height=True)
        self.usage_window = Window(
            FormattedTextControl(self._usage), wrap_lines=True, dont_extend_height=True,
            height=lambda: self._usage_height(),
        )
        self.app = Application(
            layout=Layout(HSplit([
                self.output_window,
                self.activity_window,
                Window(height=1, char="─", style="class:border"),
                self.input_window,
                ConditionalContainer(Window(FormattedTextControl(self._menu), dont_extend_height=True,
                                             height=lambda: Dimension(max=max(2, min(8, self.app.output.get_size().rows - 10)))),
                                     Condition(lambda: bool(self._items()) and not self.dismissed)),
                Window(FormattedTextControl(lambda: [("class:hint", self._hint())]),
                       height=lambda: 1 if self._hint() else 0),
                Window(FormattedTextControl(self._status), height=1),
                self.usage_window,
            ]), focused_element=control),
            key_bindings=bindings, style=style, full_screen=True, refresh_interval=0.2,
            mouse_support=True,
            color_depth=None if plain else ColorDepth.DEPTH_24_BIT,
        )

    def _page_size(self) -> int:
        """按内容视口翻页；首次渲染前退回到半屏。"""
        info = getattr(getattr(self, "output_window", None), "render_info", None)
        if info is not None:
            return max(1, info.window_height - 1)
        return max(1, self.app.output.get_size().rows // 2)

    def _scroll_by(self, lines: int) -> None:
        """正数向历史滚动，负数向最新输出滚动。"""
        maximum = max(0, self._rendered_lines - 1)
        self._scrollback = min(maximum, max(0, self._scrollback + lines))
        self.app.invalidate()

    def _scroll_to_oldest(self) -> None:
        self._scrollback = max(0, self._rendered_lines - 1)
        self.app.invalidate()

    def _scroll_to_latest(self) -> None:
        self._scrollback = 0
        self.app.invalidate()

    def _status(self):
        from .terminal import clean_text
        return [("class:status", clean_text(self.status()).replace("\n", " "))]

    def _usage(self):
        size = self.app.output.get_size()
        import re
        return [("class:usage.value" if index % 2 else "class:usage", part)
                for index, part in enumerate(re.split(r"([≥]?\d[\d.,]*(?:[kM%])?)", self.usage(size.columns, size.rows < 16)))
                if part]

    def _usage_height(self) -> int:
        """没有实际用量字段时不为底栏保留空白行。"""
        size = self.app.output.get_size()
        text = self.usage(size.columns, size.rows < 16)
        return min(3, text.count("\n") + 1) if text else 0

    def _activity(self):
        from .terminal import clean_text
        phase, label, seconds = self.activity()
        if phase == "idle" and self._dispatching:
            phase, label = "processing", "准备中"
        history = f"  ·  ↑ 历史 {self._scrollback} 行 · Ctrl+End 最新" if self._scrollback else ""
        if phase == "idle":
            return [("class:status", " ↑ 已上翻历史 · 滚轮/PgUp/PgDn · Ctrl+End 回到最新")] if history else []
        if phase == "thinking":
            frame = int(time.monotonic() * 4) % 4
            marker = ("·", "✧", "✦", "✧")[frame]
            style = "class:thinking" if frame == 2 else "class:thinking.dim"
        else:
            marker, style = ("↳", "class:status") if phase == "tool" else ("·", "class:hint")
        elapsed = f"  {seconds:.1f}s" if phase not in ("idle", "paused", "approval") else ""
        return [(style, f" {marker} {clean_text(label).replace(chr(10), ' ')}{elapsed}{history}")]

    def write(self, text: str) -> None:
        """后台只追加内容并请求重绘，不打印到终端或覆盖输入缓冲。"""
        if not text:
            return
        from .terminal import ResponsiveText
        source = text
        initial = str(source).rstrip("\n") + "\n\n"
        if hasattr(source, "at_width"):
            chunk = ResponsiveText(initial, lambda width, item=source:
                                  item.at_width(width).rstrip("\n") + "\n\n")
        else:
            chunk = initial
        with self._content_lock:
            self._chunks.append(chunk)
            self._char_count += len(chunk)
            pruned = False
            while len(self._chunks) > 1 and self._char_count > 250_000:
                self._char_count -= len(self._chunks.pop(0))
                if self._stream_index is not None:
                    self._stream_index = self._stream_index - 1 if self._stream_index else None
                pruned = True
            if len(self._chunks[0]) > 250_000:
                self._chunks[0] = self._chunks[0][-250_000:]
                self._char_count = len(self._chunks[0])
                pruned = True
            previous_lines = self._line_count
            self._line_count = max(1, "".join(self._chunks).rstrip("\n").count("\n") + 1)
            if self._scrollback:
                self._scrollback = max(0, self._scrollback + self._line_count - previous_lines)
            # 同宽度时只解析新增 ANSI 块；宽度变化时由 _transcript 统一重排。
            if self._fragments is not None and self._fragment_width and not pruned:
                from prompt_toolkit.formatted_text import ANSI, to_formatted_text
                rendered = self._chunk_text(chunk, self._fragment_width)
                self._fragments.extend(to_formatted_text(ANSI(rendered)))
                self._rendered_lines = max(self._rendered_lines, self._line_count)
            else:
                self._fragments = None
        self.app.invalidate()

    def update_stream(self, text: str, *, final: bool = False) -> None:
        """原位更新一条模型答复，避免每个 delta 都新增卡片或最终重复输出。"""
        from .terminal import ResponsiveText, render_answer

        source = render_answer(text or " ", color=not self._plain)
        initial = str(source).rstrip("\n") + "\n\n"
        chunk = (
            ResponsiveText(initial, lambda width, item=source: item.at_width(width).rstrip("\n") + "\n\n")
            if hasattr(source, "at_width") else initial
        )
        with self._content_lock:
            previous_lines = self._line_count
            if self._stream_index is None:
                if final and not text:
                    return
                self._stream_index = len(self._chunks)
                self._chunks.append(chunk)
            elif final and not text:
                self._chunks.pop(self._stream_index)
            else:
                self._chunks[self._stream_index] = chunk
            if final:
                self._stream_index = None
            self._char_count = sum(len(item) for item in self._chunks)
            rendered = "".join(str(item) for item in self._chunks).rstrip("\n")
            self._line_count = max(1, rendered.count("\n") + 1)
            if self._scrollback:
                self._scrollback = max(0, self._scrollback + self._line_count - previous_lines)
            # 被替换的块可能改变所有后续 fragment 偏移，必须按当前宽度重建。
            self._fragments = None
        self.app.invalidate()

    @staticmethod
    def _chunk_text(chunk, width: int) -> str:
        """每块至少以换行收尾，避免相邻两条输出粘在同一行。"""
        text = chunk.at_width(width) if hasattr(chunk, "at_width") else str(chunk)
        return text if not text or text.endswith("\n") else text + "\n"

    def _cursor_line(self) -> int:
        """光标行号：直接数当前 fragments 的真实行数。

        行数缓存（`_line_count`）是按送入文本估算的，窗口宽度变化后可能多于实际渲染行；
        prompt_toolkit 的滚动逻辑拿这个值直接索引 fragment 行，越界就抛 IndexError。
        """
        from prompt_toolkit.formatted_text.utils import split_lines

        with self._content_lock:
            fragments = self._fragments or []
            count = sum(1 for _ in split_lines(fragments))
        return max(0, count - 1 - self._scrollback)

    def _transcript(self):
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text
        with self._content_lock:
            width = max(20, self.app.output.get_size().columns)
            if self._fragments is None or self._fragment_width != width:
                rendered = "".join(
                    self._chunk_text(chunk, width) for chunk in self._chunks
                ).rstrip("\n")
                # 空 transcript 必须留一行占位，否则 split_lines 返回空列表，光标行号越界。
                self._fragments = to_formatted_text(ANSI(rendered)) if rendered else [("", "")]
                self._fragment_width = width
                self._rendered_lines = max(1, rendered.count("\n") + 1)
                self._line_count = self._rendered_lines
                self._scrollback = min(self._scrollback, max(0, self._rendered_lines - 1))
            return self._fragments

    async def _consume(self) -> None:
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.eventloop import run_in_executor_with_context
        while True:
            text = await self._queue.get()
            self._dispatching = True
            try:
                call = lambda: self._handler(text)
                command = text.strip().lstrip("/").split(maxsplit=1)[0] if text.strip() else ""
                # 模型表单暂时接管终端，结束后恢复同一内容区和未提交输入。
                keep = await run_in_terminal(call, in_executor=True) if command == "model" and not self.running() else await run_in_executor_with_context(call)
                if not keep:
                    self.app.exit()
                    return
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    from .terminal import render_message
                    self.write(render_message(f"命令退出码 {exc.code}", label="错误"))
            except Exception as exc:
                from .terminal import render_message
                self.write(render_message(str(exc), label="错误"))
            finally:
                self._dispatching = False
                self._queue.task_done()

    def run(self, handler) -> None:
        """保持同一个应用存活，回车提交后底栏与输入区不退出。"""
        self._handler = handler
        self._queue = asyncio.Queue()
        try:
            self.app.run(pre_run=lambda: self.app.create_background_task(self._consume()))
        finally:
            self._handler = None


    def _reopen(self) -> None:
        self.dismissed = False

    def _hint(self) -> str:
        if self.running() and not self.buffer.text.startswith("/"):
            return "Enter 发送补充 · /stop 停止"
        return command_hint(self.buffer.text)

    def _items(self):
        if not self.buffer.text.startswith("/"):
            return []
        state = self.buffer.complete_state
        return list(state.completions) if state else list(self.completer.get_completions(self.buffer.document, None))

    def _move(self, direction: int) -> None:
        if self._items() and not self.dismissed:
            if self.buffer.complete_state:
                (self.buffer.complete_next if direction > 0 else self.buffer.complete_previous)()
            else:
                self.buffer.start_completion(select_first=direction > 0, select_last=direction < 0)
        else:
            (self.buffer.auto_down if direction > 0 else self.buffer.auto_up)()

    def _menu(self):
        from .terminal import clean_text
        items = self._items()
        state = self.buffer.complete_state
        selected = state.complete_index if state and state.complete_index is not None else 0
        limit = max(1, min(6, self.app.output.get_size().rows - 12))
        start = max(0, selected - limit + 1)
        result = []
        for index, item in list(enumerate(items))[start:start + limit]:
            result.extend([("class:selected" if index == selected else "class:index",
                            f"  {index + 1:02d}  {clean_text(item.display_text):<24} {clean_text(item.display_meta_text)}"), ("", "\n")])
        result.append(("class:hint", f"  {len(items)} 项 · ↑↓ 选择 · Tab 填入 · Enter 确认 · Esc 收起"))
        return result

    def read(self) -> str:
        from prompt_toolkit.patch_stdout import patch_stdout
        self.buffer.reset()
        with patch_stdout(raw=True):
            result = self.app.run()
        self.buffer.append_to_history()
        return result


def select_option(title: str, choices: list[ChoiceSpec], *, subtitle: str = "") -> str | None:
    """方框选择页；只返回选项标识，不执行任何操作。"""
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput
    from .terminal import clean_text

    search = Buffer(multiline=False)
    selection = [0]
    def matches():
        query = search.text.casefold().split()
        return [c for c in choices if all(q in (c.value + " " + c.description).casefold() for q in query)]
    def rows():
        items = matches()
        selection[0] = min(selection[0], max(0, len(items) - 1))
        start = max(0, selection[0] - 6)
        result = []
        for i, c in list(enumerate(items))[start:start + 10]:
            result.extend([("class:chosen" if i == selection[0] else "", f" {'›' if i == selection[0] else ' '} {clean_text(c.description or c.value)}"), ("", "\n")])
        if not items:
            result.append(("class:dim", "  没有匹配项\n"))
        result.append(("class:dim", "  ↑↓ 选择 · Enter 确认 · Esc 返回"))
        return result
    search.on_text_changed += lambda _: selection.__setitem__(0, 0)
    bindings = KeyBindings()
    @bindings.add("up")
    def up(event):
        selection[0] = max(0, selection[0] - 1)
    @bindings.add("down")
    def down(event):
        selection[0] = min(max(0, len(matches()) - 1), selection[0] + 1)
    @bindings.add("enter")
    def enter(event):
        items = matches()
        if items:
            event.app.exit(result=items[selection[0]].value)
    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("c-d")
    def back(event):
        event.app.exit(result=None)
    control = BufferControl(buffer=search, input_processors=[BeforeInput(" 搜索 › ")])
    body = HSplit([
        Window(FormattedTextControl([("class:dim", clean_text(subtitle))]), dont_extend_height=True, wrap_lines=True),
        Window(control, height=1),
        Window(FormattedTextControl(rows), dont_extend_height=True),
    ])
    return _panel_app(body, title, bindings, control).run()


def _panel_app(body, title: str, bindings, focused, *, completion_menu: bool = False):
    """所有模型配置页共用方角边框；退出后恢复主控制台。"""
    from prompt_toolkit import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.layout import Float, FloatContainer, Layout, Window
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.widgets import Frame, Box
    from .terminal import clean_text, use_color, THEME

    panel = Frame(Box(body, padding=1), title=clean_text(title), style="class:panel")
    floats = [Float(
        content=panel, width=lambda: max(4, min(96, get_app().output.get_size().columns - 2)),
    )]
    if completion_menu:
        floats.append(Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=5)))
    layout = Layout(FloatContainer(Window(), floats), focused_element=focused)
    return Application(layout=layout, key_bindings=bindings, full_screen=True, erase_when_done=True,
                       color_depth=ColorDepth.DEPTH_24_BIT if use_color() else None,
                       style=Style.from_dict({"panel.border": THEME["violet"], "panel.label": "bold " + THEME["rose"],
                                              "dim": THEME["muted"], "chosen": "bg:" + THEME["selected"] + " " + THEME["text"] + " bold",
                                              "field": "bg:" + THEME["ink"] + " " + THEME["text"],
                                              "button.focused": "bg:" + THEME["selected"] + " " + THEME["text"] + " bold"} if use_color() else {}))


def edit_field(title: str, default: str = "", *, secret: bool = False) -> str:
    """独立字段输入；密码处理器遮住显示，内容不进入命令历史。"""
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import PasswordProcessor, BeforeInput

    value = Buffer(document=Document(default, cursor_position=len(default)), multiline=False)
    processors = [BeforeInput("› ")]
    if secret:
        processors.append(PasswordProcessor())
    control = BufferControl(buffer=value, input_processors=processors)
    bindings = KeyBindings()
    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=value.text.strip())
    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())
    return _panel_app(HSplit([Window(control, height=1), Window(FormattedTextControl(
        "Enter 保存 · Esc 取消"), height=1)]), title, bindings, control).run()


__all__ = [
    "COMMANDS", "ChoiceSpec", "CommandSpec", "EnhancedConsoleInput", "OptionSpec",
    "SlashCommandCompleter", "command_detail_sections", "command_help_sections",
    "command_hint", "command_names", "enhanced_input_available", "find_command",
]
