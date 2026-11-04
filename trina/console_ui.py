"""交互控制台的命令目录、斜杠补全与增强输入框。

本模块只负责展示和输入，不执行命令。命令最终仍由 ``trina.interactive``
分发到原有控制器、护栏与审计链。
"""
from __future__ import annotations

import sys
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
        "model", "会话与模型", "查看或切换当前模型", "model [list|模型规格]",
        positional_choices=(ChoiceSpec("list", "列出本机可选模型"),),
        examples=("/model list", "/model deepseek/deepseek-chat"),
    ),
    CommandSpec(
        "backend", "会话与模型", "查看或切换模型后端", "backend [omp|deepseek]",
        positional_choices=(
            ChoiceSpec("omp", "复用本机 omp 鉴权"),
            ChoiceSpec("deepseek", "直连 DeepSeek 官方 API"),
        ),
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
    CommandSpec("save", "会话与模型", "持久化当前后端与模型设置", "save"),
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
    """根据输入中的命令生成底栏提示。"""
    line = (text or "").strip()
    if not line.startswith("/"):
        return "输入 / 打开命令索引 · Tab 补全 · ↑↓ 选择"
    body = line[1:].strip()
    if not body:
        return "继续输入筛选命令 · ↑↓ 选择 · Tab 补全 · Enter 执行"
    spec = find_command(body.split(None, 1)[0])
    if spec is None:
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
        from prompt_toolkit.completion import Completion

        return Completion(
            choice.value,
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
            for spec in self._visible_commands():
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
    """只有真实双向 TTY 才启用全屏补全，重定向和测试继续使用普通 input。"""
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
    """OMP 风格的单行输入框：斜杠菜单、历史、参数提示和动态状态底栏。"""

    def __init__(
        self,
        *,
        status: Callable[[], str],
        running: Callable[[], bool],
        dynamic_values: DynamicValues,
        plain: bool = False,
    ) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style

        self.status = status
        self.completer = SlashCommandCompleter(running=running, dynamic_values=dynamic_values)
        bindings = KeyBindings()

        @bindings.add("tab")
        def _complete(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state:
                buffer.complete_next()
            else:
                buffer.start_completion(select_first=False)

        @bindings.add("s-tab")
        def _complete_previous(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state:
                buffer.complete_previous()
            else:
                buffer.start_completion(select_first=False)

        @bindings.add("/")
        def _open_command_index(event) -> None:
            """首字符 `/` 立即展开索引；URL 等位置的 `/` 仍只是普通字符。"""
            buffer = event.current_buffer
            at_start = buffer.cursor_position == 0
            buffer.insert_text("/")
            if at_start:
                buffer.start_completion(select_first=False)

        colors = {
            "prompt.brand": "bold ansicyan",
            "prompt.arrow": "bold ansigreen",
            "toolbar": "bg:#1f2937 #d1d5db",
            "toolbar.status": "bold #67e8f9",
            "toolbar.hint": "#d1d5db",
            "completion-menu.completion": "bg:#111827 #e5e7eb",
            "completion-menu.completion.current": "bg:#0891b2 #ffffff bold",
            "completion-menu.meta.completion": "bg:#1f2937 #9ca3af",
            "completion-menu.meta.completion.current": "bg:#0e7490 #ecfeff",
        }
        if plain:
            colors = {key: "" for key in colors}
        self.session = PromptSession(
            history=InMemoryHistory(),
            auto_suggest=AutoSuggestFromHistory(),
            completer=self.completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            key_bindings=bindings,
            enable_history_search=True,
            mouse_support=not plain,
            reserve_space_for_menu=12,
            style=Style.from_dict(colors),
        )

    @staticmethod
    def _message():
        return [("class:prompt.brand", "Trina"), ("class:prompt.arrow", "  › ")]

    def _bottom_toolbar(self):
        text = self.session.default_buffer.text
        return [
            ("class:toolbar.status", f" {self.status()} "),
            ("class:toolbar.hint", f" · {command_hint(text)} "),
        ]

    def read(self) -> str:
        from prompt_toolkit.patch_stdout import patch_stdout

        # 后台 Agent 的进度输出会安全地显示在输入框上方，不破坏用户正在输入的内容。
        with patch_stdout(raw=True):
            return self.session.prompt(message=self._message, bottom_toolbar=self._bottom_toolbar)


__all__ = [
    "COMMANDS", "ChoiceSpec", "CommandSpec", "EnhancedConsoleInput", "OptionSpec",
    "SlashCommandCompleter", "command_detail_sections", "command_help_sections",
    "command_hint", "command_names", "enhanced_input_available", "find_command",
]
