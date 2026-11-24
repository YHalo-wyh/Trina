"""交互控制台的命令目录、斜杠补全与增强输入框。

本模块只负责展示和输入，不执行命令。命令最终仍由 ``trina.interactive``
分发到原有控制器、护栏与审计链。
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable

from .ui_preferences import PREFERENCES
from .worker_roles import WORKER_ROLES

if TYPE_CHECKING:
    from .terminal import ResponsiveText
    from .transcript import StreamingAnswer

try:
    from prompt_toolkit.completion import Completer as _Completer
except ImportError:  # pragma: no cover - 缺少可选输入库时仍允许普通 input 启动
    class _Completer:  # type: ignore[no-redef]
        pass


_KEY_SEQUENCE_TIMEOUT = .05
_KEY_BINDING_TIMEOUT = .1
_REDRAW_INTERVAL = 1 / 120


def _low_latency_keys(app):
    """缩短 VT 转义序列与多键绑定判定等待，避免方向键/选项确认显得迟钝。"""
    app.ttimeoutlen = _KEY_SEQUENCE_TIMEOUT
    app.timeoutlen = _KEY_BINDING_TIMEOUT
    return app


class _ModalInput:
    """主界面挂起后拒绝迟到的输入回调，由弹窗独占同一终端输入。"""

    def __init__(self, source, app):
        self.source, self.app = source, app

    def __getattr__(self, name):
        return getattr(self.source, name)

    def read_keys(self):
        return [] if self.app._running_in_terminal else self.source.read_keys()

    def flush_keys(self):
        return [] if self.app._running_in_terminal else self.source.flush_keys()


def run_panel(app):
    """嵌套面板复用主 UI 事件循环，避免每次弹窗销毁 Windows 输入执行器。"""
    from prompt_toolkit.application.current import get_app
    parent = get_app()
    loop = getattr(parent, "loop", None)
    if (loop is not None and parent._running_in_terminal
            and threading.current_thread() is not getattr(parent, "_loop_thread", None)):
        return asyncio.run_coroutine_threadsafe(app.run_async(handle_sigint=False), loop).result()
    return app.run()


@dataclass(frozen=True)
class ChoiceSpec:
    """一个可补全的参数值。"""

    value: str
    description: str = ""


SETUP_SECTIONS = (ChoiceSpec("model", "模型与供应商"),
                  *(ChoiceSpec(name, spec.label) for name, spec in PREFERENCES.items()))
COMMAND_ICONS = {
    "设置": ("⚙", "gold"),
    "任务": ("◎", "cyan"),
    "工具": ("⌘", "blue"),
    "研究": ("◈", "cyan"),
    "会话与模型": ("◇", "lilac"),
    "并行任务": ("▦", "pink"),
    "工作区": ("▣", "blue"),
    "运行控制": ("↔", "gold"),
    "交付": ("≡", "mint"),
    "系统": ("·", "neutral"),
}
COMMAND_SYMBOLS = {
    "model": "✧", "status": "◉", "help": "?", "resume": "↶", "new": "+",
    "play": "▷", "run": "→", "plan": "☑", "tasks": "≡", "schedule": "◷", "jobs": "▤",
    "board": "▦", "queue": "≡", "pause": "Ⅱ", "stop": "□",
    "history": "≣", "save": "↓", "quit": "↗",
}
COMMAND_ICON_COLORS = {
    "gold": "#e8bf83", "cyan": "#88c8d9", "blue": "#9eb7e7", "lilac": "#cfb0e9",
    "pink": "#e0a7c9", "mint": "#a3d7be", "neutral": "#b9adbf",
}


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
            '/play --goal "检查测试服务是否正常响应" --max-steps 8',
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
        examples=('/run http://127.0.0.1:9099 --goal "检查首页响应状态"',),
    ),
    CommandSpec("goal", "任务", "保存或修改当前项目的任务目标", "goal [set <目标>|show|clear]",
                positional_choices=(ChoiceSpec("show", "查看目标"), ChoiceSpec("set", "设置目标"), ChoiceSpec("clear", "清除目标")),
                examples=('/goal 检查登录流程并记录证据',), availability="always"),
    CommandSpec("plan", "任务", "执行前澄清目标和验收，保留待发计划", "plan <任务> | choose <编号> | reply <答案> | queue | cancel",
                positional_choices=(ChoiceSpec("show", "查看计划"), ChoiceSpec("choose", "选择编号"),
                                    ChoiceSpec("reply", "补充答案"), ChoiceSpec("queue", "计划加入待发队列"),
                                    ChoiceSpec("cancel", "取消计划")), availability="always"),
    CommandSpec("tasks", "任务", "查看当前会话的实时任务清单", "tasks [show|clear]", aliases=("todo",),
                positional_choices=(ChoiceSpec("show", "查看完整清单"), ChoiceSpec("clear", "清空清单")),
                availability="always"),
    CommandSpec("schedule", "任务", "定时启动任务", "schedule <add|list|cancel> [选项]",
                aliases=("scheduled", "schedualed"), availability="always",
                positional_choices=(ChoiceSpec("add", "安排任务"), ChoiceSpec("list", "查看任务"), ChoiceSpec("cancel", "取消任务")),
                options=(OptionSpec(("--at",), "开始时间", "<HH:MM|日期时间|+10m>"),
                         OptionSpec(("--target",), "执行目标", "<地址>"), OptionSpec(("--goal",), "任务目标", "<文本>"),
                         OptionSpec(("--max-steps",), "决策步数", "<整数>"), OptionSpec(("--approve",), "审批模式", "<模式>", APPROVAL_CHOICES)),
                examples=('/schedule add --at +10m --target http://127.0.0.1:9099',)),
    CommandSpec("status", "任务", "查看当前任务或上一轮结果", "status", availability="always"),
    CommandSpec("jobs", "任务", "查看或等待当前后台任务", "jobs [list|wait <any|all> [秒数] [ID …]|unwait|logs <ID>|stop <ID>|message <ID> <文本>]",
                positional_choices=(ChoiceSpec("list", "查看任务"), ChoiceSpec("wait", "等待任务结束"),
                                    ChoiceSpec("unwait", "退出等待"), ChoiceSpec("logs", "查看日志"),
                                    ChoiceSpec("stop", "停止任务"), ChoiceSpec("message", "给 Worker 发送补充")),
                examples=("/jobs wait any 30", "/jobs logs terminal:job-123", "/jobs message worker:tests 请汇总测试结果"),
                availability="always"),
    CommandSpec("board", "任务", "打开状态、证据、覆盖和成本工作台", "board", availability="always"),
    CommandSpec("hypotheses", "研究", "按优先级查看假设台账", "hypotheses"),
    CommandSpec("graph", "研究", "输出证据关系图（Mermaid）", "graph"),
    CommandSpec(
        "tools", "工具", "搜索工具并解释当前 Agent 的可见性", "tools [--verbose|--json|--all] [--route 路由]",
        options=(
            OptionSpec(("--verbose", "-v"), "显示完整参数表"),
            OptionSpec(("--json",), "输出 JSON"),
            OptionSpec(("--all",), "显示完整注册表"),
            OptionSpec(("--route",), "查看指定任务路由", "<路由>", tuple(
                ChoiceSpec(name, label) for name, label in (
                    ("general", "通用 CTF"), ("ctf", "本地题目文件"), ("web", "Web/API"),
                    ("binary", "Pwn/Reverse"), ("code", "代码仓库"),
                )
            )),
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
        "model", "会话与模型", "搜索和切换模型（Alt+P）", "model [settings|add|edit|remove|test|use <供应商>|context auto|500K|1M|list|模型规格]",
        positional_choices=(ChoiceSpec("settings", "管理供应商与模型选项"), ChoiceSpec("add", "添加供应商配置"), ChoiceSpec("edit", "编辑供应商整条配置"),
                            ChoiceSpec("remove", "删除供应商配置"), ChoiceSpec("test", "测试供应商地址连通性"),
                            ChoiceSpec("use", "切换供应商"), ChoiceSpec("context", "上下文窗口：auto / 500K / 1M / Token 整数"), ChoiceSpec("list", "列出模型与供应商")),
        examples=("/model", "/model add", "/model edit", "/model context 500K", "/model test"),
        aliases=("models",),
    ),
    CommandSpec(
        "approve", "会话与模型", "调整动作审批闸门", "approve <off|exploit|verify|all>",
        positional_choices=APPROVAL_CHOICES,
    ),
    CommandSpec("usage", "会话与模型", "查看模型调用、Token 和成本", "usage"),
    CommandSpec("worker", "会话与模型", "切换独立 Worker 会话和当前角色", "worker [commander|general|code|web|binary|review]",
                positional_choices=tuple(ChoiceSpec(key, value[0]) for key, value in WORKER_ROLES.items())),
    CommandSpec("subworker", "并行任务", "子 Worker 面板、模型与思考强度、并发和任务管理", "subworker [操作] [参数]",
                positional_choices=tuple(ChoiceSpec(k, v) for k, v in (("start", "名称 目标 任务：启动"), ("run", "从 YAML 任务图启动"),
                    ("add", "追加并行任务"), ("grid", "九宫格实时对话"), ("list", "查看任务"), ("message", "立即补充指定任务"),
                    ("queue", "预存给指定任务的补充"), ("cancel", "取消指定任务"), ("inspect", "查看结果与证据"), ("model", "设置子模型 ID"),
                    ("thinking", "设置思考强度"), ("provider", "选择已保存供应商"), ("settings", "并发、步数、超时"),
                    ("pause", "暂停"), ("continue", "继续"), ("stop", "停止与回收"), ("resume", "恢复 Swarm 检查点"))),
                examples=("/subworker", "/subworker start audit localhost 检查服务", "/subworker thinking high", "/subworker model deepseek-flash", "/subworker run tasks.yaml")),
    CommandSpec("branch", "工作区", "查看、切换或创建本地 Git 分支", "branch [分支名|new <名称>]",
                positional_choices=(ChoiceSpec("new", "创建并切换分支"), ChoiceSpec("list", "列出分支"),
                                    ChoiceSpec("switch", "切换分支"), ChoiceSpec("delete", "删除已合并分支"))),
    CommandSpec("repo", "工作区", "仓库状态、Diff、历史、暂存、提交与远端同步", "repo [操作] [参数] [--repo <目录>]",
                positional_choices=tuple(ChoiceSpec(k, v) for k, v in (("status", "查看文件状态"), ("diff", "查看差异"),
                    ("log", "查看提交历史"), ("remotes", "查看远端名称"), ("stage", "暂存指定文件"), ("unstage", "撤销指定文件的暂存"),
                    ("commit", "提交暂存区"), ("branches", "查看分支"), ("worktrees", "查看 worktree"),
                    ("fetch", "获取远端引用"), ("pull", "仅快进拉取"), ("push", "推送当前 HEAD"))),
                options=(OptionSpec(("--repo",), "操作指定仓库目录", "<目录>"), OptionSpec(("--staged",), "查看暂存区差异"),
                         OptionSpec(("--limit",), "历史条数", "<1–100>"), OptionSpec(("--remote",), "已配置远端", "<名称>")),
                examples=("/repo", "/repo diff --staged", '/repo commit "fix: repair parser"', "/repo stage src/main.py")),
    CommandSpec("worktree", "工作区", "列出、创建或移除干净 worktree", "worktree [list|add <路径> [新分支]|remove <路径>] [--base <起点>]",
                aliases=("wt",), positional_choices=(ChoiceSpec("list", "查看工作树"), ChoiceSpec("add", "创建独立目录"),
                                                        ChoiceSpec("remove", "移除干净工作树")),
                options=(OptionSpec(("--base",), "新工作树的提交起点", "<引用>"), OptionSpec(("--repo",), "源仓库", "<目录>"),
                         OptionSpec(("--existing",), "使用已有分支"))),
    CommandSpec("theme", "会话与模型", "切换输出语义配色", "theme [trina|contrast|mono]", aliases=("colors",),
                positional_choices=(ChoiceSpec("trina", "粉紫主题，绿成功/红失败/蓝运行"), ChoiceSpec("contrast", "高对比输出"),
                                    ChoiceSpec("mono", "单色输出"))),
    CommandSpec("notify", "会话与模型", "切换或测试 Agent 完成提醒", "notify [on|off|test]",
                positional_choices=(ChoiceSpec("on", "启用完成提醒"), ChoiceSpec("off", "关闭完成提醒"),
                                    ChoiceSpec("test", "立即显示测试提醒"))),
    CommandSpec("footer", "会话与模型", "切换底栏信息密度", "footer [default|compact|full]",
                positional_choices=(ChoiceSpec("default", "均衡"), ChoiceSpec("compact", "精简"), ChoiceSpec("full", "完整"))),
    CommandSpec("context", "会话与模型", "查看上下文上限、前缀变化和缓存诊断", "context"),
    CommandSpec("compact", "会话与模型", "创建继承检查点并保存完整历史", "compact"),
    CommandSpec(
        "history", "会话与模型", "查看最近的对话与工具摘要", "history [条数]",
        examples=("/history 12",),
    ),
    CommandSpec("new", "会话与模型", "清空上下文并开启新会话", "new", aliases=("clear",)),
    CommandSpec("save", "会话与模型", "持久化当前供应商与模型设置", "save"),
    CommandSpec("setup", "设置", "打开模型、界面和通知设置", "setup [项目 [值]]",
                positional_choices=SETUP_SECTIONS,
                examples=("/setup", "/setup model", "/setup theme contrast", "/setup icons off")),
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
    CommandSpec("queue", "运行控制", "查看、发送或移除待发草稿", "queue [list|add <文本>|send [编号]|drop <编号>]",
                positional_choices=(ChoiceSpec("list", "查看草稿"), ChoiceSpec("add", "留待发送"),
                                    ChoiceSpec("send", "发送草稿"), ChoiceSpec("drop", "移除草稿")),
                availability="always"),
    CommandSpec("pause", "运行控制", "暂停当前任务", "pause", availability="running"),
    CommandSpec("resume", "会话与模型", "查看、搜索和切换历史对话", "resume [list|last|编号|ID]",
                positional_choices=(ChoiceSpec("list", "列出已保存对话"), ChoiceSpec("last", "切换最近对话")),
                examples=("/resume", "/resume list", "/resume last")),
    CommandSpec("unpause", "运行控制", "继续已暂停任务", "unpause", availability="running"),
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
        return "继续输入筛选命令 · ↑↓ 选择 · Tab 补全 · Enter 确认"
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
        self.recent: list[str] = []

    def _visible_commands(self) -> list[CommandSpec]:
        running = self.running()
        wanted = "running" if running else "idle"
        return [item for item in COMMANDS if item.availability in (wanted, "always")]

    def remember(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        spec = find_command(parts[0]) if parts and parts[0].startswith("/") else None
        if spec is not None:
            self.recent = [spec.name, *(item for item in self.recent if item != spec.name)][:5]

    def _matching_commands(self, query: str) -> list[CommandSpec]:
        ranked = []
        defaults = ("model", "status", "setup", "help", "resume", "new")
        priority = tuple(dict.fromkeys((*self.recent, *defaults)))
        for spec in self._visible_commands():
            names = (spec.name, *spec.aliases)
            if not query:
                score = priority.index(spec.name) if spec.name in priority else len(priority)
            elif query in names:
                score = 0
            elif any(name.startswith(query) for name in names):
                score = 1
            elif any(query in name for name in names):
                score = 2
            elif query in (spec.description + " " + spec.category).casefold():
                score = 3
            else:
                if not any(self._matches_in_order(query, name) for name in names):
                    continue
                score = 4
            ranked.append((score, spec))
        return [spec for _, spec in sorted(ranked, key=lambda entry: (entry[0], len(entry[1].name) if query else 0))]

    @staticmethod
    def _matches_in_order(query: str, value: str) -> bool:
        chars = iter(value)
        return all(char in chars for char in query)

    @classmethod
    def _matching_values(cls, query: str, choices: Iterable[ChoiceSpec]) -> list[ChoiceSpec]:
        needle = query.casefold()
        ranked = []
        seen = set()
        for choice in choices:
            value, description = choice.value.casefold(), choice.description.casefold()
            if choice.value in seen:
                continue
            seen.add(choice.value)
            if not needle or value == needle:
                score = 0
            elif value.startswith(needle):
                score = 1
            elif needle in value or needle in description:
                score = 2
            elif cls._matches_in_order(needle, value):
                score = 3
            else:
                continue
            ranked.append((score, choice))
        return [choice for _, choice in sorted(ranked, key=lambda entry: entry[0])]

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
        import shlex

        text = document.text_before_cursor
        if (not text.startswith("/") or "\n" in document.text or
                document.text_after_cursor and not document.text_after_cursor[0].isspace()):
            return
        body = text[1:]
        if not any(ch.isspace() for ch in body):
            for spec in self._matching_commands(body.casefold()):
                yield Completion(spec.name, start_position=-len(body), display=f"/{spec.name}",
                                 display_meta=spec.description)
            return

        # 只拆已经输入的 token，允许最后一个参数的引号尚未闭合。
        tokens = list(re.finditer(r"""(?:[^\s'"\\]|\\.|'[^']*'?|"(?:[^"\\]|\\.)*"?)+""", body))
        if not tokens:
            return
        command = tokens[0][0]
        spec = find_command(command)
        if spec is None or spec.availability not in ("always", "running" if self.running() else "idle"):
            return
        tail = tokens[1:]
        pending = tail.pop()[0] if tail and tail[-1].end() == len(body) else ""
        try:
            completed_args = shlex.split(body[tokens[0].end():tail[-1].end()]) if tail else []
        except ValueError:
            return
        try:
            prefix = shlex.split(pending)[0] if pending else ""
        except ValueError:
            if not pending.startswith(("'", '"')):
                return
            prefix = pending[1:]

        # 支持 --approve=v 这类等号形式。
        if prefix.startswith("--") and "=" in prefix:
            flag, value_prefix = prefix.split("=", 1)
            value_token = pending.split("=", 1)[1]
            option = next((item for item in spec.options if flag in item.flags), None)
            if option:
                for choice in option.choices:
                    if choice.value.startswith(value_prefix):
                        yield Completion(
                            choice.value,
                            start_position=-len(value_token),
                            display=choice.value,
                            display_meta=choice.description,
                        )
            return

        previous = completed_args[-1] if completed_args else ""
        value_option = next((item for item in spec.options if previous in item.flags), None)
        if value_option is not None:
            choices = list(value_option.choices) or list(self.dynamic_values(spec.name, completed_args))
            for choice in self._matching_values(prefix, choices):
                yield self._choice_completion(choice, pending)
            return

        if not prefix.startswith("-"):
            dynamic = list(self.dynamic_values(spec.name, completed_args))
            positional = list(spec.positional_choices) if not completed_args else []
            if spec.name == "help" and not completed_args:
                positional.extend(ChoiceSpec(item.name, item.description) for item in COMMANDS)
            choices = positional + dynamic
            if choices:
                for choice in self._matching_values(prefix, choices):
                    yield self._choice_completion(choice, pending)
                return

        if prefix and not prefix.startswith("-"):
            return
        used = {arg.split("=", 1)[0] for arg in completed_args}
        for option in spec.options:
            if not option.repeatable and any(flag in used for flag in option.flags):
                continue
            if prefix and not any(flag.startswith(prefix) for flag in option.flags):
                continue
            yield Completion(
                option.primary,
                start_position=-len(pending),
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

    def __init__(self, *, status, running, dynamic_values, plain: bool = False, usage=None, activity=None, context=None,
                 motion: str = "full", output_palette="trina", activity_detail=None, cancel_wait=None,
                 command_icons: Callable[[], bool] | None = None, tasks=None) -> None:
        from prompt_toolkit import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, ConditionalContainer, FloatContainer, Float
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.styles import Style
        from prompt_toolkit.output import ColorDepth
        from prompt_toolkit.keys import Keys
        from .terminal import THEME

        self.status = status
        self.context = context
        self._frame_context = None
        self._frame_tasks = None
        self._task_rows_cache = (None, [])
        self.usage = usage or (lambda _width, _compact=False: "")
        self.activity = activity or (lambda: ("processing", "运行中", 0) if running() else ("idle", "就绪", 0))
        self.activity_detail = activity_detail or (lambda: "")
        self.tasks = tasks or (lambda: {})
        self._handler = None
        self._dispatching = False
        self._queue = None
        self._chunks: list[str | StreamingAnswer] = []
        self._stream_index: int | None = None
        self._stream_format_lock = asyncio.Lock()
        self._tool_indices: dict[str, int] = {}
        self._thinking_indices: dict[str, int] = {}
        self._plain = plain
        self._output_palette = output_palette
        self._command_icons = command_icons or (lambda: True)
        self._home_animation = False
        self._home_frame = -1
        self._toast_message = ""
        self._toast_level = "success"
        self._toast_token = 0
        self._motion = motion
        self._mouse_enabled = True
        self._char_count = 0
        self._content_lock = threading.RLock()
        self._fragments = None
        self._fragment_width = 0
        self._line_count = 1
        #: 最近一次渲染出的真实行数；光标越界会直接把 prompt_toolkit 的滚动逻辑打崩。
        self._rendered_lines = 1
        self._scrollback = 0
        self._wheel_lines = 0
        self._wheel_timer: asyncio.TimerHandle | None = None
        # 只有用户主动上翻历史时才暂停尾随；不能用残留选区猜测用户意图。
        self._follow_latest = True
        self.running = running
        self.completer = SlashCommandCompleter(running=running, dynamic_values=dynamic_values)
        self.dismissed = False
        self._menu_cache = (None, [])
        self._menu_selected = 0
        self._menu_start = 0
        self._menu_navigated = False
        self.buffer = Buffer(history=InMemoryHistory(), completer=self.completer,
                             complete_while_typing=False, multiline=False)
        self.buffer.on_text_changed += lambda _: self._reopen()
        bindings = KeyBindings()

        @bindings.add("c-space", filter=Condition(lambda: not self.buffer.text or self.buffer.text.startswith("/")))
        def open_commands(event):
            if not self.buffer.text:
                self.buffer.insert_text("/")
            else:
                self.dismissed = False

        @bindings.add("tab")
        def complete(event):
            self._accept_menu(add_space=True)

        @bindings.add("up")
        @bindings.add("s-tab")
        def previous(event):
            self._move(-1)

        @bindings.add("down")
        def next_item(event):
            self._move(1)

        @bindings.add("escape")
        def dismiss(event):
            if cancel_wait is not None and cancel_wait():
                return
            self.buffer.cancel_completion()
            self.dismissed = True
            self.transcript_control.clear_selection()

        @bindings.add("enter")
        def submit(event):
            if self._complete_before_submit():
                return
            self._home_animation = False
            value = self.buffer.text
            self.completer.remember(value)
            if self._handler is None:
                event.app.exit(result=value)
            else:
                self.buffer.append_to_history()
                self.buffer.reset()
                self._scroll_to_latest()
                self._queue.put_nowait(value)

        @bindings.add(Keys.BracketedPaste)
        def bracketed_paste(event):
            """把整段粘贴作为一次输入插入，避免逐字符触发 Enter/补全。"""
            data = str(event.data).replace("\r\n", "\n").replace("\r", "\n")
            event.current_buffer.insert_text(data)

        @bindings.add("c-c")
        def cancel(event):
            if not self.transcript_control.copy_selection():
                event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("f2")
        def native_selection(event):
            self._mouse_enabled = not self._mouse_enabled
            self.scrollbar_control.cancel_drag()
            self.transcript_control.clear_selection()
            self.app.invalidate()

        @bindings.add("escape", "p", filter=Condition(lambda: self._queue is not None and not self._dispatching and not self.running()))
        def open_model_picker(event):
            self._queue.put_nowait("/model")

        @bindings.add("c-d")
        def eof(event):
            if not self.buffer.text:
                event.app.exit(exception=EOFError())
            else:
                self.buffer.delete()

        @bindings.add("pageup")
        def page_up(event):
            if self._items():
                self._move(-self._menu_limit())
            else:
                self._scroll_by(self._page_size())

        @bindings.add("pagedown")
        def page_down(event):
            if self._items():
                self._move(self._menu_limit())
            else:
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

        style = Style.from_dict({"selection": "reverse", "toast": "reverse"} if plain else {
            "prompt": "bold " + THEME["rose"], "index": THEME["muted"],
            "selected": "bg:#33283c " + THEME["text"], "hint": THEME["muted"],
            "command.rule": "#675471", "command.key": "bold " + THEME["lilac"],
            "command.description": THEME["muted"],
            **{f"command.icon.{tone}": color for tone, color in COMMAND_ICON_COLORS.items()},
            "status": THEME["lilac"], "title": "bold " + THEME["rose"],
            "thinking": THEME["rose"], "thinking.dim": THEME["violet"],
            "border": THEME["violet"], "rail": THEME["violet"],
            "usage": THEME["muted"], "usage.value": THEME["lilac"],
            "context.model": THEME["rose"], "context.path": THEME["lilac"],
            "context.role": THEME["lilac"],
            "context.branch": THEME["muted"],
            "context.cache": "#a9d9d1", "context.tokens": THEME["lilac"],
            "context.duration": THEME["lilac"],
            "context.workers": "bold " + THEME["rose"],
            "context.model.icon": "bold " + THEME["rose"],
            "context.state": "bold " + THEME["lilac"],
            "context.progress": THEME["lilac"], "context.progress.icon": "bold " + THEME["violet"],
            "context.meter": "bold " + THEME["rose"],
            "context.warn": "bold #e5bb86", "context.hot": "bold #fa8eae",
            "context.label": THEME["muted"], "context.task": "italic " + THEME["lilac"],
            "toast": "bg:#3b2444 #f4d7ec bold", "toast.success": "bg:#244036 #d9f5e7 bold",
            "toast.error": "bg:#542c38 #ffdce3 bold",
            "selection": "bg:" + THEME["selected"] + " " + THEME["text"],
            "scrollbar.thumb": THEME["rose"], "scrollbar.track": THEME["muted"],
            "todo.header": "bold " + THEME["text"], "todo.active": THEME["rose"],
            "todo.pending": THEME["text"], "todo.blocked": "#e5bb86",
            "todo.closed": THEME["muted"], "todo.more": THEME["muted"],
        })
        control = BufferControl(buffer=self.buffer, input_processors=[BeforeInput(
            lambda: [("class:prompt", "补充  › " if self.running() else "Trina  › ")])])
        from .transcript import TranscriptControl

        self.transcript_control = TranscriptControl(self)

        owner = self

        # transcript 已按脏块缓存成物理行；关闭 Window 二次折行并直接给出视口偏移。
        from .scrollbar import ScrollbarControl

        self.output_window = Window(
            self.transcript_control,
            wrap_lines=False,
            always_hide_cursor=True,
            get_vertical_scroll=lambda _window: self.transcript_control.viewport_start(),
        )
        self.scrollbar_control = ScrollbarControl(
            self._scroll_viewport, self._seek_viewport, lambda direction: self._wheel_scroll(direction * 3),
            thumb_style="class:scrollbar.thumb", track_style="class:scrollbar.track",
            on_press=self._cancel_wheel_scroll,
        )
        self.scrollbar_window = Window(self.scrollbar_control, width=1, wrap_lines=False, always_hide_cursor=True)
        self.activity_window = Window(FormattedTextControl(self._activity), height=1, wrap_lines=False)
        self.tasks_window = Window(FormattedTextControl(self._tasks), wrap_lines=False,
                                   dont_extend_height=True, height=self._tasks_height)
        self.input_window = Window(control, wrap_lines=True, height=Dimension(min=1, max=3), dont_extend_height=True)
        self.usage_window = Window(
            FormattedTextControl(self._usage), wrap_lines=False, dont_extend_height=True,
            height=lambda: self._usage_height(),
        )
        content = HSplit([
                VSplit([self.output_window, self.scrollbar_window]),
                ConditionalContainer(self.tasks_window, Condition(lambda: self._tasks_height() > 0)),
                self.activity_window,
                Window(FormattedTextControl(self._rail), height=1, wrap_lines=False),
                self.input_window,
                ConditionalContainer(
                    Window(FormattedTextControl(self._menu), wrap_lines=False, dont_extend_height=True,
                           height=lambda: self._menu_height()),
                    Condition(lambda: self._menu_height() > 0)),
                Window(FormattedTextControl(self._status), height=self._status_height, wrap_lines=False),
                ConditionalContainer(self.usage_window, Condition(lambda: self.context is None)),
            ])
        toast = ConditionalContainer(
            Window(FormattedTextControl(self._toast), height=1, wrap_lines=False,
                   width=Dimension(min=12, preferred=42, max=60)),
            Condition(lambda: bool(self._toast_message)),
        )
        self.app = Application(
            layout=Layout(FloatContainer(content=content, floats=[Float(top=1, right=1, content=toast)]),
                          focused_element=control),
            key_bindings=bindings, style=style, full_screen=True,
            paste_mode=True,
            refresh_interval=0.2 if motion == "full" else None,
            min_redraw_interval=_REDRAW_INTERVAL, max_render_postpone_time=0,
            mouse_support=Condition(lambda: self._mouse_enabled),
            color_depth=None if plain else ColorDepth.DEPTH_24_BIT,
        )
        _low_latency_keys(self.app)
        # Windows 的句柄通知可能已排队，detach 之后才送达旧应用。
        # 包装仅属于主界面；嵌套表单继续使用 AppSession 的原始输入。
        self.app.input = _ModalInput(self.app.input, self.app)
        self.app.before_render += self._before_render
        self.app.after_render += self._after_render

    def _before_render(self, _app) -> None:
        self.scrollbar_control.begin_frame()
        self._animate_home()
        if self.context is not None:
            self._frame_context = self.context()
        self._frame_tasks = self.tasks()

    def _after_render(self, _app) -> None:
        self.transcript_control.remember_viewport()
        self.scrollbar_control.finish_frame(_app, self.scrollbar_window, _REDRAW_INTERVAL)
        self._frame_context = None
        self._frame_tasks = None

    def _context_view(self):
        """一帧内共享底栏快照，避免重复读取用量和 Worker 状态。"""
        return self._frame_context if self._frame_context is not None else self.context()

    def _toast(self):
        from .console_state import _fit_cells
        from .terminal import clean_text
        width = max(12, min(60, self.app.output.get_size().columns - 2))
        marker = "!" if self._toast_level == "error" else "✦"
        style = "class:toast.error" if self._toast_level == "error" else "class:toast.success"
        return [(style, _fit_cells(f" {marker} {clean_text(self._toast_message).replace(chr(10), ' ')} ", width))]

    def show_notification(self, title: str, body: str = "", *, level: str = "success", seconds: float = 5) -> None:
        """线程安全地显示短时通知条，并向支持通知协议的终端发送桌面提醒。"""
        from .notifications import notification_sequence
        from .terminal import clean_text

        message = " · ".join(part for part in (clean_text(title), clean_text(body)) if part)

        def apply() -> None:
            self._toast_token += 1
            token = self._toast_token
            self._toast_message = message[:240]
            self._toast_level = "error" if level == "error" else "success"
            if self.app.is_running:
                self.app.output.write_raw(notification_sequence(title, body))
                self.app.output.flush()
            self.app.invalidate()
            loop = getattr(self.app, "loop", None)
            if loop is not None and loop.is_running():
                loop.call_later(max(.5, float(seconds)), lambda: self._clear_notification(token))

        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(apply)
        else:
            apply()

    def _clear_notification(self, token: int) -> None:
        if token != self._toast_token:
            return
        self._toast_message = ""
        self.app.invalidate()

    def show_home(self) -> None:
        from .terminal import render_home
        self.write(render_home(color=not self._plain))
        self._home_animation = not self._plain and self._motion == "full"

    def _animate_home(self) -> None:
        """只在首页等待输入时更新唯一字标块；发送后冻结，历史滚动不重排。"""
        frame = int(time.monotonic() * 3)
        if not self._home_animation or frame == self._home_frame or self._scrollback or len(self._chunks) != 1:
            return
        from .terminal import render_home
        self._home_frame = frame
        with self._content_lock:
            chunk = render_home(color=True, phase=frame * .3)
            self._chunks[0] = chunk
            self._char_count = len(chunk)
            self.transcript_control.invalidate_from(0)

    def _page_size(self) -> int:
        """按内容视口翻页；首次渲染前退回到半屏。"""
        info = getattr(getattr(self, "output_window", None), "render_info", None)
        if info is not None:
            return max(1, info.window_height - 1)
        return max(1, self.app.output.get_size().rows // 2)

    def _wheel_scroll(self, lines: int) -> None:
        """把同一重绘间隔内的鼠标刻度合并，保留逐刻度钳制到边界的顺序。"""
        self.scrollbar_control.cancel_drag()
        loop = getattr(self.app, "loop", None)
        if loop is None or not loop.is_running():
            self._scroll_by(lines)
            return
        maximum = self.transcript_control.max_scrollback()
        target = min(maximum, max(0, self._scrollback + self._wheel_lines + lines))
        self._wheel_lines = target - self._scrollback
        if self._wheel_timer is None:
            if self._wheel_lines:
                self._wheel_timer = loop.call_later(_REDRAW_INTERVAL, self._scroll_by, 0)
            elif lines < 0:
                self._scroll_by(lines)

    def _cancel_wheel_scroll(self) -> int:
        if self._wheel_timer is not None:
            self._wheel_timer.cancel()
            self._wheel_timer = None
        lines = self._wheel_lines
        self._wheel_lines = 0
        return lines

    def _scroll_by(self, lines: int) -> None:
        """正数向历史滚动，负数向最新输出滚动。"""
        old_start = self.transcript_control.viewport_start()
        maximum = self.transcript_control.max_scrollback()
        pending = min(maximum, max(0, self._scrollback + self._cancel_wheel_scroll()))
        updated = min(maximum, max(0, pending + lines))
        if updated == self._scrollback:
            if lines < 0 and updated == 0:
                self._follow_latest = True
                self.transcript_control.clear_selection()
            return
        self._scrollback = updated
        self._follow_latest = updated == 0
        if self._follow_latest:
            self.transcript_control.clear_selection()
        self.transcript_control.precondition_scroll(old_start, self.transcript_control.viewport_start())
        self.app.invalidate()

    def _scroll_to_oldest(self) -> None:
        self.scrollbar_control.cancel_drag()
        self._cancel_wheel_scroll()
        old_start = self.transcript_control.viewport_start()
        updated = self.transcript_control.max_scrollback()
        if updated == self._scrollback:
            return
        self._scrollback = updated
        self._follow_latest = False
        self.transcript_control.precondition_scroll(old_start, self.transcript_control.viewport_start())
        self.app.invalidate()

    def _scroll_to_latest(self) -> None:
        self.scrollbar_control.cancel_drag()
        self._cancel_wheel_scroll()
        selected = self.transcript_control.anchor is not None
        if not self._scrollback and not selected:
            return
        old_start = self.transcript_control.viewport_start()
        self._scrollback = 0
        self._follow_latest = True
        self.transcript_control.clear_selection()
        self.transcript_control.precondition_scroll(old_start, self.transcript_control.viewport_start())
        self.app.invalidate()

    def _scroll_viewport(self) -> tuple[int, int, int]:
        control = self.transcript_control
        return control.viewport_height, control.max_scrollback(), control.viewport_start()

    def _seek_viewport(self, offset: int, drag_maximum: int) -> None:
        maximum = self.transcript_control.max_scrollback()
        updated = 0 if offset == drag_maximum else max(0, maximum - offset)
        if updated != self._scrollback:
            self.transcript_control.mark_drag_seek()
        self._scrollback = updated
        self._follow_latest = self._scrollback == 0
        if self._follow_latest:
            self.transcript_control.clear_selection()

    def _tasks_snapshot(self) -> dict:
        value = self._frame_tasks if self._frame_tasks is not None else self.tasks()
        return value if isinstance(value, dict) else {}

    def _task_rows(self) -> list[tuple[str, str]]:
        """生成固定任务卡；任务多时折叠，完整清单由 ``/tasks`` 展示。"""
        snapshot = self._tasks_snapshot()
        tasks = snapshot.get("tasks", [])
        size = self.app.output.get_size()
        cache_key = (snapshot.get("revision"), size.rows, size.columns)
        if cache_key == self._task_rows_cache[0]:
            return self._task_rows_cache[1]
        if not isinstance(tasks, list) or not tasks:
            self._task_rows_cache = (cache_key, [])
            return []
        counts = snapshot.get("counts", {}) if isinstance(snapshot.get("counts"), dict) else {}
        active = int(counts.get("in_progress", 0) or 0)
        pending = int(counts.get("pending", 0) or 0)
        blocked = int(counts.get("blocked", 0) or 0)
        completed = int(counts.get("completed", 0) or 0)
        abandoned = int(counts.get("abandoned", 0) or 0)
        parts = [f"{active} 进行中", f"{pending} 待处理"]
        if blocked:
            parts.append(f"{blocked} 阻塞")
        if not (active or pending or blocked):
            parts = [f"{completed} 已完成"]
            if abandoned:
                parts.append(f"{abandoned} 已取消")
        rows: list[tuple[str, str]] = [("class:todo.header", "≡ 任务  " + " · ".join(parts) + "  ▾")]
        open_tasks = [row for row in tasks if isinstance(row, dict) and row.get("status") in
                      {"in_progress", "blocked", "pending"}]
        visible = open_tasks if open_tasks else [row for row in tasks if isinstance(row, dict)][-1:]
        order = {"in_progress": 0, "blocked": 1, "pending": 2, "completed": 3, "abandoned": 4}
        ordered = sorted(enumerate(visible), key=lambda pair: (order.get(str(pair[1].get("status")), 9), pair[0]))
        limit = max(2, min(6, size.rows // 4))
        shown = [row for _, row in ordered[:limit]]
        markers = {"in_progress": "◉", "pending": "○", "blocked": "!", "completed": "✓", "abandoned": "–"}
        styles = {"in_progress": "class:todo.active", "pending": "class:todo.pending",
                  "blocked": "class:todo.blocked", "completed": "class:todo.closed",
                  "abandoned": "class:todo.closed"}
        for row in shown:
            status = str(row.get("status") or "pending")
            phase = str(row.get("phase") or "").strip()
            label = f"{phase} · " if phase else ""
            rows.append((styles.get(status, "class:todo.pending"),
                         f"  {markers.get(status, '○')} {label}{str(row.get('text') or '').strip()}"))
        hidden = len(visible) - len(shown)
        if hidden:
            rows.append(("class:todo.more", f"    … 另有 {hidden} 项，输入 /tasks 查看完整清单"))
        self._task_rows_cache = (cache_key, rows)
        return rows

    def _tasks(self):
        from .console_state import _fit_cells
        width = max(1, self.app.output.get_size().columns)
        fragments = []
        for index, (style, value) in enumerate(self._task_rows()):
            fragments.append((style, ("\n" if index else "") + _fit_cells(value, width)))
        return fragments

    def _tasks_height(self) -> int:
        return len(self._task_rows())

    def _status(self):
        from .terminal import clean_text
        if self.context is not None:
            fragments = self._context_view().footer(self.app.output.get_size().columns)
            if not self._mouse_enabled:
                from .console_state import _fit_cells
                fragments += [("class:context.label", "\n" + _fit_cells("原生选取模式 · F2 恢复滚动", self.app.output.get_size().columns))]
            return fragments
        native = " · 原生选取模式 · F2 恢复滚动" if not self._mouse_enabled else ""
        return [("class:status", clean_text(self.status()).replace("\n", " ") + native)]

    def _status_height(self) -> int:
        return "".join(text for _, text in self._status()).count("\n") + 1

    def _rail(self):
        if self.context is None:
            return [("class:rail", "─" * max(1, self.app.output.get_size().columns))]
        return self._context_view().rail(self.app.output.get_size().columns)

    def _activity_height(self) -> int:
        return 1

    def _usage(self):
        size = self.app.output.get_size()
        import re
        return [("class:usage.value" if index % 2 else "class:usage", part)
                for index, part in enumerate(re.split(r"([≥]?\d[\d.,]*(?:[kM%])?)", self.usage(size.columns, size.rows < 16)))
                if part]

    def _usage_height(self) -> int:
        """没有实际用量字段时不为底栏保留空白行。"""
        if self.context is not None:
            return 0
        size = self.app.output.get_size()
        text = self.usage(size.columns, size.rows < 16)
        return 1 if text else 0

    def _activity(self):
        from .terminal import clean_text
        phase, label, seconds = self.activity()
        if phase == "idle" and self._dispatching:
            phase, label = "processing", "准备中"
        if phase == "idle":
            return self._with_task([])
        if phase == "thinking":
            frame = int(time.monotonic() * 4) % 4
            marker = ("·", "✧", "✦", "✧")[frame]
            style = "class:thinking" if frame == 2 else "class:thinking.dim"
        else:
            marker = {"waiting": "◌", "background_wait": "◌", "responding": "▸", "tool": "◆", "background": "↻", "retry": "↻",
                      "processing": "◇", "approval": "?", "paused": "Ⅱ", "stopping": "■",
                      "error": "!"}.get(phase, "·")
            style = "class:status" if phase in {"tool", "background", "responding"} else "class:hint"
        if phase == "waiting" and seconds >= 15:
            label = "等待供应商响应"
        elapsed = f"  {seconds:.1f}s" if phase not in ("idle", "paused", "approval") else ""
        fragments = [(style, f" {marker} {clean_text(label).replace(chr(10), ' ')}{elapsed}")]
        if phase == "tool" and (detail := self.activity_detail()):
            fragments.append(("class:context.progress", f"  ↳ {clean_text(detail).replace(chr(10), ' ')}"))
        return self._with_task(fragments)

    def _with_task(self, fragments: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """活动和任务始终共用一行；窄窗口省略标题及较长状态。"""
        from .console_state import _cells, _fit_cells
        width = max(1, self.app.output.get_size().columns)
        right = ""
        if self.context is not None and width >= 12:
            right = _fit_cells("▣ " + self._context_view().task, width * 2 // 5 if fragments else width)
        remaining = width - _cells(right) - (2 if right and fragments else 0)
        shown: list[tuple[str, str]] = []
        for style, text in fragments:
            if remaining <= 0:
                break
            size = _cells(text)
            if size > remaining:
                prefix = text[:len(text) - len(text.lstrip())]
                if len(prefix) >= remaining:
                    break
                text = prefix + _fit_cells(text[len(prefix):], remaining - len(prefix))
                size = _cells(text)
            shown.append((style, text))
            remaining -= size
        if right:
            shown.extend([("", " " * (remaining + (2 if fragments else 0))), ("class:context.task", right)])
        return shown

    def write(self, text: str) -> None:
        """后台只追加内容并请求重绘，不打印到终端或覆盖输入缓冲。"""
        if not text:
            return
        from .terminal import ResponsiveText
        source = text
        initial = str(source).rstrip("\n") + "\n\n"
        if hasattr(source, "at_width"):
            chunk = ResponsiveText(initial, lambda width, item=source:
                                  item.at_width(width).rstrip("\n") + "\n\n",
                                  lambda width, item=source: (fragments + [("", "\n\n")]
                                  if hasattr(item, "fragments_at_width") and
                                  (fragments := item.fragments_at_width(width)) is not None else None))
        else:
            chunk = initial
        with self._content_lock:
            self._chunks.append(chunk)
            self._char_count += len(chunk)
            pruned = False
            while len(self._chunks) > 1 and self._char_count > 250_000:
                self._char_count -= len(self._chunks.pop(0))
                self._tool_indices = {key: index - 1 for key, index in self._tool_indices.items() if index > 0}
                self._thinking_indices = {key: index - 1 for key, index in self._thinking_indices.items() if index > 0}
                if self._stream_index is not None:
                    self._stream_index = self._stream_index - 1 if self._stream_index else None
                pruned = True
            if len(self._chunks[0]) > 250_000:
                self._chunks[0] = str(self._chunks[0])[-250_000:]
                self._char_count = len(self._chunks[0])
                pruned = True
            self.transcript_control.invalidate_from(0 if pruned else len(self._chunks) - 1)
            if pruned:
                self.transcript_control.clear_selection()
        self.app.invalidate()

    def clear_transcript(self) -> None:
        """切换对话时替换内容，保留输入区与应用生命周期。"""
        with self._content_lock:
            self.scrollbar_control.cancel_drag()
            self._cancel_wheel_scroll()
            self._chunks.clear()
            self._tool_indices.clear()
            self._thinking_indices.clear()
            self._stream_index = None
            self._char_count = self._scrollback = 0
            self._follow_latest = True
            self._rendered_lines = self._line_count = 1
            self.transcript_control.clear_selection()
            self.transcript_control.invalidate_from(0)
        self.app.invalidate()

    def update_tool(self, call_id: str, card: str) -> None:
        """工具生命周期更新同一个块；保留输入草稿、其他窗口和滚动位置。"""
        from .terminal import ResponsiveText
        with self._content_lock:
            index = self._tool_indices.get(call_id)
            if index is None:
                self.write(card)
                self._tool_indices[call_id] = len(self._chunks) - 1
                return
            chunk = ResponsiveText(str(card).rstrip("\n") + "\n\n",
                                   lambda width: card.at_width(width).rstrip("\n") + "\n\n",
                                   lambda width: (fragments + [("", "\n\n")]
                                   if hasattr(card, "fragments_at_width") and
                                   (fragments := card.fragments_at_width(width)) is not None else None))
            self._char_count += len(chunk) - len(self._chunks[index])
            self._chunks[index] = chunk
            self.transcript_control.invalidate_from(index)
        self.app.invalidate()

    def update_thinking(self, request_id: str, text: str, *, final: bool = False) -> None:
        """思考和正文使用独立块，只在显示帧排版当前变化部分。"""
        from .terminal import ResponsiveText, render_thinking
        chunk = ResponsiveText(text, lambda width: str(render_thinking(
            text, final=final, width=width, color=not self._plain, palette=self._output_palette)) + "\n\n")
        with self._content_lock:
            index = self._thinking_indices.get(request_id)
            if index is None:
                if not text:
                    return
                index = len(self._chunks)
                self._thinking_indices[request_id] = index
                self._chunks.append("")
            self._char_count += len(chunk) - len(self._chunks[index])
            self._chunks[index] = chunk
            self.transcript_control.invalidate_from(index)
        self.app.invalidate()

    def update_stream(self, text: str, *, final: bool = False) -> None:
        """接收正文更新，长草稿复用已排版行，长终稿在后台排版。"""
        from .terminal import ResponsiveText, streaming_answer
        from .transcript import StreamingAnswer

        loop = getattr(self.app, "loop", None)
        defer_format = final and len(text) > StreamingAnswer.MARKDOWN_PREVIEW_CHARS and loop is not None and loop.is_running()
        with self._content_lock:
            if self._stream_index is None:
                if final and not text:
                    return
                self._stream_index = len(self._chunks)
                self._chunks.append("")
            index = self._stream_index
            previous = self._chunks[index]
            old_length = len(previous)
            if final and not text:
                self._chunks.pop(index)
                self._tool_indices = {key: (value - 1 if value > index else value)
                                      for key, value in self._tool_indices.items() if value != index}
                self._thinking_indices = {key: (value - 1 if value > index else value)
                                          for key, value in self._thinking_indices.items() if value != index}
                self._char_count -= old_length
            else:
                if final and not defer_format:
                    source = streaming_answer(text, color=not self._plain, palette=self._output_palette)
                    chunk = ResponsiveText(text, lambda width: source.at_width(width).rstrip("\n") + "\n\n",
                                           lambda width: source.fragments_at_width(width) + [("", "\n\n")])
                elif isinstance(previous, StreamingAnswer):
                    previous.replace(text)
                    chunk = previous
                else:
                    chunk = StreamingAnswer(text, color=not self._plain, palette=self._output_palette)
                self._chunks[index] = chunk
                self._char_count += len(chunk) - old_length
            self.transcript_control.invalidate_from(index)
            if final:
                self._stream_index = None
        if defer_format:
            ready_source = streaming_answer(text, color=not self._plain, palette=self._output_palette)
            loop.call_soon_threadsafe(self._schedule_final_markdown, chunk, ready_source)
        self.app.invalidate()

    def _schedule_final_markdown(self, preview: StreamingAnswer, source: ResponsiveText) -> None:
        if self.app.is_running:
            self.app.create_background_task(self._finish_final_markdown(preview, source))

    async def _finish_final_markdown(self, preview: StreamingAnswer, source: ResponsiveText) -> None:
        from .terminal import ResponsiveText

        async with self._stream_format_lock:
            with self._content_lock:
                if preview not in self._chunks:
                    return
            try:
                fragments = await asyncio.to_thread(source.fragments_at_width, 1)
            except Exception:
                return  # 排版失败时保持已显示的字面正文。
        with self._content_lock:
            try:
                index = self._chunks.index(preview)
            except ValueError:  # 会话切换或历史裁剪已移除此块。
                return
            self._chunks[index] = ResponsiveText(preview.text, lambda width: source.at_width(width).rstrip("\n") + "\n\n",
                                                 lambda _width: fragments + [("", "\n\n")])
            self.transcript_control.invalidate_from(index)
        self.app.invalidate()
    @staticmethod
    def _chunk_text(chunk, width: int) -> str:
        """每块至少以换行收尾，避免相邻两条输出粘在同一行。"""
        text = chunk.at_width(width) if hasattr(chunk, "at_width") else str(chunk)
        return text if not text or text.endswith("\n") else text + "\n"

    def _cursor_line(self) -> int:
        """行数在内容变动时缓存；滚动和活动动画无需扫描历史。"""
        return max(0, self._rendered_lines - 1 - self._scrollback)

    def _transcript(self):
        """兼容文本检查入口；实际 UI 通过 create_content 按需读取缓存行。"""
        self.transcript_control.sync(max(1, self.app.output.get_size().columns))
        if self._fragments is None:
            fragments = []
            for index, row in enumerate(self.transcript_control.rows):
                if index:
                    fragments.append(("", "\n"))
                fragments.extend(row)
            self._fragments = fragments or [("", "")]
        return self._fragments

    def pick_option(self, title: str, choices: list[ChoiceSpec], *, subtitle: str) -> str | None:
        """命令线程只在选择器打开时挂起主界面，随后继续后台读取历史。"""
        from prompt_toolkit.application import run_in_terminal

        async def choose():
            return await run_in_terminal(lambda: select_option(title, choices, subtitle=subtitle), in_executor=True)

        return asyncio.run_coroutine_threadsafe(choose(), self.app.loop).result()

    async def _consume(self) -> None:
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.eventloop import run_in_executor_with_context
        while True:
            text = await self._queue.get()
            self._dispatching = True
            try:
                call = lambda: self._handler(text)
                words = text.strip().lstrip("/").split(maxsplit=2)
                command = words[0] if words else ""
                # 模型表单暂时接管终端，结束后恢复同一内容区和未提交输入。
                modal = command in {"model", "models", "tools", "setup", "worker", "subworker", "branch", "repo", "worktree", "wt", "theme", "colors", "footer"} and (not self.running() or command == "subworker")
                modal = modal or (words == ["plan", "choose"] and not self.running())
                keep = await run_in_terminal(call, in_executor=True) if modal else await run_in_executor_with_context(call)
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
            self.scrollbar_control.cancel_drag()
            self._cancel_wheel_scroll()
            self._handler = None


    def _reopen(self) -> None:
        self.dismissed = False
        self._menu_selected = self._menu_start = 0
        self._menu_navigated = False

    def _items(self):
        if self.dismissed or not self.buffer.text.startswith("/"):
            return []
        key = (self.buffer.text, self.buffer.cursor_position, self.running(), tuple(self.completer.recent))
        if key != self._menu_cache[0]:
            self._menu_cache = key, list(self.completer.get_completions(self.buffer.document, None))
            self._menu_selected = self._menu_start = 0
            self._menu_navigated = False
        return self._menu_cache[1]

    def _menu_limit(self) -> int:
        return max(1, min(8, self.app.output.get_size().rows - 10))

    def _menu_height(self) -> int:
        items = self._items()
        return min(len(items), self._menu_limit()) + 1 if items else int(self._unmatched_command())

    def _unmatched_command(self) -> bool:
        text = self.buffer.text
        return (not self.dismissed and self.buffer.cursor_position == len(text) and text.startswith("/")
                and len(text) > 1 and not any(ch.isspace() for ch in text))

    def _accept_menu(self, *, add_space: bool = False) -> None:
        items = self._items()
        if not items:
            return
        item = items[self._menu_selected]
        self.buffer.apply_completion(item)
        if add_space and not self.buffer.document.text_after_cursor:
            self.buffer.insert_text(" ")
        self.dismissed = not add_space

    def _complete_before_submit(self) -> bool:
        items = self._items()
        if not items:
            return False
        text = self.buffer.document.text_before_cursor
        if self.buffer.document.text_after_cursor:
            return False
        has_args = any(ch.isspace() for ch in text)
        if not self._menu_navigated and (has_args or find_command(text)):
            return False
        item = items[self._menu_selected]
        if text[len(text) + item.start_position:] == item.text:
            return False
        self._accept_menu()
        return True

    def _move(self, direction: int) -> None:
        items = self._items()
        if items:
            self._menu_selected = (self._menu_selected + direction) % len(items)
            self._menu_navigated = True
        else:
            (self.buffer.auto_down if direction > 0 else self.buffer.auto_up)()

    def _menu(self):
        from prompt_toolkit.mouse_events import MouseEventType
        from .console_state import _cells, _fit_cells
        from .model_picker_view import padded

        items = self._items()
        if not items:
            return [("class:hint", " 没有匹配命令 · /help 查看命令列表")] if self._unmatched_command() else []
        selected, limit = self._menu_selected, self._menu_limit()
        self._menu_start = max(0, selected - limit + 1, min(self._menu_start, selected))
        width = min(92, self.app.output.get_size().columns)
        arguments = any(ch.isspace() for ch in self.buffer.document.text_before_cursor)
        icons = not arguments and width >= 24 and self._command_icons()
        icon_width = 2 if icons else 0
        name_width = min(max(_cells(item.display_text) for item in items) + 1,
                         width // 2 if width >= 42 else width - 4 - icon_width)

        def mouse(position):
            def handle(event):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self._menu_selected = position
                    self._accept_menu(add_space=True)
                elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                    self._move(-1 if event.event_type == MouseEventType.SCROLL_UP else 1)
                else:
                    return NotImplemented
            return handle

        summary = f" {selected + 1}/{len(items)} "
        keys = "↑↓ 浏览  Tab 补全  Esc 收起"
        heading = " 参数 " if arguments else " 命令 "
        spec = find_command(items[selected].display_text) if not arguments else None
        if spec and width >= 52:
            heading = f" 命令 · {spec.category} "
        spare = width - _cells(heading + summary) - 2
        hint = keys if _cells(keys) <= spare else ""
        result = [("class:command.rule", "╭─"), ("class:command.key", heading),
                  ("class:command.rule", "─" * max(0, spare - _cells(hint))),
                  ("class:hint", hint), ("class:command.key", summary)]
        for pos in range(self._menu_start, min(self._menu_start + limit, len(items))):
            item = items[pos]
            focus = pos == selected
            style = ("reverse" if self._plain else "class:selected") if focus else ""
            description = _fit_cells(item.display_meta_text, width - name_width - 5 - icon_width) if width >= 42 else ""
            marker = "▸ " if focus else "  "
            name = padded(item.display_text, name_width)
            meta = " " + description if description else ""
            spacing = " " * max(0, width - _cells(marker + name + meta) - 2 - icon_width)
            handle = mouse(pos)
            result.extend([("", "\n"), ("class:command.rule", "│ ", handle), (style, marker, handle)])
            if icons:
                command = find_command(item.display_text)
                glyph, tone = COMMAND_ICONS.get(command.category, ("·", "neutral")) if command else ("·", "neutral")
                if command:
                    glyph = COMMAND_SYMBOLS.get(command.name, glyph)
                icon_style = style if self._plain else f"{style} class:command.icon.{tone}"
                result.append((icon_style, padded(glyph, icon_width), handle))
            result.extend([(style, name, handle),
                           (style if focus else "class:command.description", meta + spacing, handle)])
        return result

    def read(self) -> str:
        from prompt_toolkit.patch_stdout import patch_stdout
        self.buffer.reset()
        with patch_stdout(raw=True):
            result = self.app.run()
        self.buffer.append_to_history()
        return result


def select_option(title: str, choices: list[ChoiceSpec], *, subtitle: str = "", plain: bool = False) -> str | None:
    """方框选择页；只返回选项标识，不执行任何操作。"""
    from prompt_toolkit.application.current import get_app
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
        page = max(1, min(10, get_app().output.get_size().rows - 6 - len(subtitle.splitlines())))
        start = max(0, selection[0] - page + 1)
        result = []
        for i, c in enumerate(items[start:start + page], start):
            focused = i == selection[0]
            style = ("reverse" if plain else "class:chosen") if focused else ""
            result.extend([(style, f" {'›' if focused else ' '} {clean_text(c.description or c.value)}"), ("", "\n")])
        if not items:
            result.append(("class:dim", "  没有匹配项\n"))
        result.append(("class:dim", "  ↑↓ 选择 · Enter 确认 · Esc 返回"))
        return result
    search.on_text_changed += lambda _: selection.__setitem__(0, 0)
    bindings = KeyBindings()
    @bindings.add("up")
    def up(event):
        selection[0] = max(0, selection[0] - 1)
        event.app.invalidate()
    @bindings.add("down")
    def down(event):
        selection[0] = min(max(0, len(matches()) - 1), selection[0] + 1)
        event.app.invalidate()
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
    heading = [Window(FormattedTextControl([("class:dim", clean_text(subtitle))]), dont_extend_height=True, wrap_lines=True)] if subtitle else []
    body = HSplit([
        *heading,
        Window(control, height=1),
        Window(FormattedTextControl(rows), dont_extend_height=True),
    ])
    return run_panel(_panel_app(body, title, bindings, control, plain=plain))


def _panel_app(body, title: str, bindings, focused, *, completion_menu: bool = False, plain: bool = False, expanded: bool = False):
    """所有模型配置页共用方角边框；退出后恢复主控制台。"""
    from prompt_toolkit import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.layout import Float, FloatContainer, Layout, Window
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.widgets import Frame, Box
    from .terminal import clean_text, use_color, THEME

    color = use_color(plain=plain)
    if expanded:
        panel = Box(body, padding=0, padding_left=2, padding_right=2)
        floats = [Float(content=panel, left=0, right=0, top=0, bottom=0)]
    else:
        panel = Frame(Box(body, padding=1), title=clean_text(title), style="class:panel")
        floats = [Float(content=panel, width=lambda: max(4, min(96, get_app().output.get_size().columns - 2)))]
    if completion_menu:
        floats.append(Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=5)))
    layout = Layout(FloatContainer(Window(), floats), focused_element=focused)
    app = Application(layout=layout, key_bindings=bindings, full_screen=True, erase_when_done=True,
                      mouse_support=expanded,
                      min_redraw_interval=1 / 120, max_render_postpone_time=0,
                      color_depth=ColorDepth.DEPTH_24_BIT if color else None,
                      style=Style.from_dict({"panel.border": THEME["violet"], "panel.label": "bold " + THEME["rose"],
                                             "model.accent": THEME["lilac"] + " bold",
                                             "model.live": "#99d7bc", "model.rule": "#504957",
                                             "model.key": "#d5c2e3 bold", "model.search": "bg:#24202b " + THEME["text"],
                                             "model.row": "bg:#33283c " + THEME["text"],
                                             "model.tab": "bg:#cbb2dc #221b29 bold",
                                             "dim": THEME["muted"], "chosen": "bg:" + THEME["selected"] + " " + THEME["text"] + " bold",
                                             "field": "bg:" + THEME["ink"] + " " + THEME["text"],
                                             "button.focused": "bg:" + THEME["selected"] + " " + THEME["text"] + " bold"} if color else {}))
    return _low_latency_keys(app)


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
    return run_panel(_panel_app(HSplit([Window(control, height=1), Window(FormattedTextControl(
        "Enter 保存 · Esc 取消"), height=1)]), title, bindings, control))


__all__ = [
    "COMMANDS", "ChoiceSpec", "CommandSpec", "EnhancedConsoleInput", "OptionSpec",
    "SlashCommandCompleter", "command_detail_sections", "command_help_sections",
    "command_hint", "command_names", "enhanced_input_available", "find_command",
]
