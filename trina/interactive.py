"""交互式控制台：人工可介入的全自动渗透。

    python3 -m trina console        # 或直接 `python3 -m trina`

三条交互路径
------------
1. **一条命令开打**：`play`（自动起 lab 靶机 → 跑 → 出报告）
2. **运行中实时介入**：`hint <文本>` 注入提示、`pause`/`unpause`、`stop`、`status`
3. **可选审批闸门**：默认关闭；显式指定 `--approve` 后，超时未答复按拒绝处理。

命令都只是薄封装：能委托给 CLI 的（tools/guard/ranges/report/audit）直接复用同一实现，避免两套行为。
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import shlex
import sys
import threading
import time
from typing import Any, Callable

from .config import TrinaConfig
from .console_state import ConsoleContext, ConsoleState, TrackedBackend
from .model_names import display_model as _display_model
from .console_ui import (
    ChoiceSpec, EnhancedConsoleInput, command_detail_sections, command_help_sections,
    command_names, enhanced_input_available, find_command,
)
from .session import APPROVAL_MODES, Session
from .tasks import TaskStore, TaskScheduler
from .terminal import (
    PHASES, clean_text, render_approval, render_message,
    render_panel, render_step, render_answer, render_process, show_home, terminal_width, use_color,
)

#: 已知命令名（用于区分"命令"与"对 Agent 说话"）
COMMAND_NAMES = command_names() | {"y", "yes", "n", "no", "deny"}


class Console:
    """对话式控制台：自然语言对话 + 命令两种入口，共用同一套护栏/控制器/审计。"""

    def __init__(
        self,
        config: TrinaConfig | None = None,
        *,
        input_fn: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
        backend: Any = None,
        registry: Any = None,
        approval_mode: str = "off",
        autostart_lab: bool = True,
        plain: bool = False,
    ):
        self.config = config or TrinaConfig.load()
        self._input = input_fn
        self._enhanced_input: EnhancedConsoleInput | None = None
        self._plain = plain
        self.output = output or (lambda line: print(line, flush=True))
        self.color = use_color(plain=plain or output is not None)
        self._live_step: Any = None
        #: 注入的后端（测试用）或按当前会话配置构建的缓存
        self._backend_obj = backend
        self._tracked_backend = None
        self.view = ConsoleState()
        self._custom_output = output is not None
        #: 测试可注入的后端工厂（形如 make_backend(llm_config) -> LLMBackend）
        self._backend_factory = None
        self.registry = registry
        self.autostart_lab = autostart_lab
        self._lock = threading.RLock()
        self._last_state: Any = None
        self._plan: Any = None
        self._controller: Any = None
        self._controller_config: TrinaConfig | None = None
        self.chat: Any = None
        self.session = Session(approval_mode=approval_mode, emit=self._session_emit)
        self.exit_requested = False
        self.task_store = TaskStore()
        self._goal_text = self.task_store.goal()
        self._launch_lock = threading.RLock()
        self._scheduler = None
        self._scheduler_thread = None
        self._task_config = None
        self._event_transcript = None
        self._task_title = ""
        self._task_max_steps = 0

    # ------------------------------------------------------------------ 输出
    def _display(self, text: str) -> None:
        with self._lock:
            self.output(text)

    def _emit(self, message: str, prefix: str = "[trina]") -> None:
        self._display(render_message(message, label=prefix, color=self.color) if message else "")

    def _session_emit(self, message: str) -> None:
        pending = self.session.pending
        if pending is not None and message.startswith("[审批] 请求执行"):
            self._display(render_approval(pending, self.session.approval_timeout, color=self.color))
        else:
            self._emit(message)

    def _agent_log(self, message: str) -> None:
        import re
        if message.startswith(("[用户] ", "[动作 ")) or re.match(r"^\[\d+/\d+\]", message) or message.lstrip().startswith("→"):
            return  # 已由结构化事件渲染，避免日志重复成为另一条消息。
        if "想法：" in message:
            self._display(render_process(message.split("想法：", 1)[1].strip(), label="思考摘要", color=self.color))
        else:
            self._display(render_process(message.strip(), label="过程", color=self.color))

    def _on_step(self, step: Any) -> None:
        self._live_step = step

    def _on_runtime_event(self, event: Any) -> None:
        from .event_view import EventTranscript
        if self._event_transcript is None:
            self._event_transcript = EventTranscript(self._display, editor=self._enhanced_input, color=self.color)
        if event.kind == "tool.started":
            self.view.set_activity("tool", str(event.payload.get("tool", "工具处理中")))
        elif event.kind == "model.retry":
            self.view.set_activity("retry", f"等待重试 · 第 {event.payload.get('attempt')} 次")
        elif event.kind == "model.error":
            self.view.set_activity("error", "模型请求失败")
        self._event_transcript.consume(event)

    def _show_help(self, args: list[str] | None = None) -> None:
        name = (args or [""])[0].lstrip("/")
        if name:
            spec = find_command(name)
            if spec is None:
                self._emit(f"没有找到命令 /{name}；输入 /help 查看完整索引。")
                return
            self._display(render_panel(
                f"命令 /{spec.name}", sections=command_detail_sections(spec),
                footer="在输入框键入 / 可实时筛选；Tab 补全，↑↓ 选择。", color=self.color,
            ))
            return
        self._display(render_panel(
            "命令索引", sections=command_help_sections(),
            footer="输入 / 打开实时命令索引 · 继续输入可筛选 · Tab 补全 · /help <命令> 查看选项",
            color=self.color,
        ))

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        if self._input is None and not self._custom_output and enhanced_input_available():
            self._enhanced_input = EnhancedConsoleInput(
                status=self._prompt_status, running=lambda: self.session.running,
                dynamic_values=self._completion_values, plain=self._plain or not self.color,
                usage=self.view.footer, activity=self._activity,
                context=self._footer_context,
            )
            original_output = self.output
            self.output = self._enhanced_input.write
            self._start_scheduler()
            show_home(output=self._display, color=self.color)
            try:
                self._enhanced_input.run(self.handle)
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                self._cleanup()
                self.output = original_output
            return 0
        show_home(output=self._display, color=self.color)
        while not self.exit_requested:
            try:
                raw = self._read_input()
            except (EOFError, KeyboardInterrupt):
                if self.session.running and self.session.thread is not None:
                    self._emit("输入已结束：等待当前对话/任务收尾（最多 20 秒）…")
                    self.session.thread.join(timeout=20)
                self._emit("再见。")
                break
            except StopIteration:
                break
            if not self.handle(raw):
                break
        self._cleanup()
        return 0

    def _read_input(self) -> str:
        """测试和重定向使用普通输入；真实终端由持久界面接管。"""
        if self._input is not None:
            return self._input(self._prompt())
        return input(self._prompt())

    def _activity(self) -> tuple[str, str, float]:
        if self.session.pending:
            return "approval", "等待审批 · y 允许 / n 拒绝", 0
        if self.session.stop_event.is_set() and self.session.running:
            return "stopping", "停止中", 0
        if self.session.pause_event.is_set() and self.session.running:
            return "paused", "已暂停", 0
        if not self.session.running:
            if self.chat is not None:
                active = [job for job in self.chat.ctx.extra["background_jobs"].snapshot() if job["status"] == "running"]
                if active:
                    return "tool", f"后台终端 · {len(active)} 项运行中", max(job["elapsed_seconds"] for job in active)
            return "idle", "就绪", 0
        return self.view.activity()

    def _footer_context(self) -> ConsoleContext:
        """直接读取当前任务配置，模型切换、恢复和新会话在下一帧生效。"""
        llm = (self._task_config or self.config).llm
        status = "就绪"
        if self.session.running:
            status = "待审批 y/n" if self.session.pending else (
                "暂停" if self.session.pause_event.is_set() else (
                    "停止中" if self.session.stop_event.is_set() else "运行中"
                )
            )
        pending = self.session.steers.qsize()
        queued = f" · 补充 {pending} 条待处理" if pending else ""
        state = self.session.state or self._last_state
        is_chat = self.chat is not None and state is self.chat.state
        context_tokens = self.view.context_tokens
        progress = ""
        if is_chat:
            estimated = self.chat.context.stats.get("estimated_input_tokens")
            if isinstance(estimated, int):
                context_tokens = estimated
            tools = int(self.chat.state.counters.get("tool_calls", 0))
            progress = f"轮次 {self.chat.turns}" + (f" · 工具 {tools}" if tools else "")
        else:
            stats = getattr(state, "context_stats", {}) if state is not None else {}
            estimated = stats.get("estimated_input_tokens") if isinstance(stats, dict) else None
            if isinstance(estimated, int):
                context_tokens = estimated
            step = int(getattr(self._live_step, "index", 0) or
                       (getattr(state, "counters", {}) or {}).get("steps", 0))
            if self._task_max_steps:
                progress = f"步骤 {step}/{self._task_max_steps}"
        return ConsoleContext(status=status + queued, model=_display_model(llm.model, llm.reasoning_effort),
                              directory=str(Path.cwd()), task=self._task_title or "新对话",
                              progress=progress, context_tokens=context_tokens,
                              context_window=llm.effective_context_window)

    def _prompt_status(self) -> str:
        context = self._footer_context()
        return f"{context.status} · {context.model} · 目录 {context.directory} · 任务 {context.task}"

    def _prompt(self) -> str:
        short = clean_text(_display_model(self.config.llm.model, self.config.llm.reasoning_effort) or "?").replace("\n", " ")[:28]
        status = self._prompt_status().split(" · ", 1)[0]
        # 状态在下一次读取输入时刷新；后台进展单独输出，不清空输入缓冲。
        return f"trina · {status} · {short} > " if terminal_width() >= 60 else f"trina · {status} > "

    def _completion_values(self, command: str, completed_args: list[str]) -> list[ChoiceSpec]:
        """补全运行时才能知道的历史会话、模型、工具、靶场和产物路径。"""
        if command == "resume" and not completed_args:
            from .chat_store import ChatStore
            try:
                return [ChoiceSpec(row["id"], row["title"]) for row in ChatStore(self.config.workspace_dir).list()[:40]]
            except Exception:
                return []
        from pathlib import Path

        if command == "model" and not completed_args:
            try:
                from .providers import PRESETS, ProfileStore

                store = ProfileStore()
                return ([ChoiceSpec(p.id, f"已保存 · {p.model}") for p in store.profiles.values()]
                        + [ChoiceSpec(p.id, f"{p.name} · 添加连接") for p in PRESETS])
            except Exception:  # noqa: BLE001 - 补全失败不能影响输入
                return []
        if command == "model" and completed_args in (["use"], ["edit"], ["remove"], ["test"], ["keys"]):
            from .providers import ProfileStore
            return [ChoiceSpec(p.id, p.model) for p in ProfileStore().profiles.values()]
        if command == "model" and completed_args == ["add"]:
            from .providers import PRESETS
            return [ChoiceSpec(p.id, p.name) for p in PRESETS]
        if command == "model" and completed_args == ["context"]:
            return [ChoiceSpec("auto", "按当前模型自动选择"), ChoiceSpec("500K", "500,000 Token"),
                    ChoiceSpec("1M", "1,000,000 Token")]
        if command == "tool" and not completed_args:
            try:
                registry = self.registry
                if registry is None:
                    from .tools import build_registry

                    registry = build_registry()
                descriptions = {item["name"]: item.get("description", "") for item in registry.describe()}
                return [ChoiceSpec(name, descriptions.get(name, "")) for name in registry.names()]
            except Exception:  # noqa: BLE001
                return []
        range_names = []
        try:
            from .adapters import available_range_names
            range_names = available_range_names(self.config)
        except OSError:
            pass
        if command in ("play",) and completed_args[-1:] in (["--range"], ["-r"]):
            return [ChoiceSpec(name, "已登记靶场") for name in range_names]
        if command == "ranges" and completed_args == ["status"]:
            return [ChoiceSpec(name, "已登记靶场") for name in range_names]

        state_flags = {"--state"}
        if command in ("report", "review") and completed_args[-1:] and completed_args[-1] in state_flags:
            paths = sorted(self.config.workspace_dir.glob("*/state.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            return [ChoiceSpec(str(path), "运行状态") for path in paths[:12]]
        if command == "audit" and completed_args and completed_args[0] in ("verify", "show", "tail"):
            if len(completed_args) == 1:
                paths = sorted(self.config.audit_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
                return [ChoiceSpec(str(path), "审计日志") for path in paths[:12]]
        return []

    # ------------------------------------------------------------------ 命令
    def handle(self, raw: str) -> bool:
        line = (raw or "").strip()
        if not line:
            self._show_status()
            return True
        if line.lstrip("/").strip() in ("quit", "exit", "q"):
            if self.session.running:
                self._emit("任务仍在运行：先 stop，或再次输入 quit 强制退出。")
                if not getattr(self, "_quit_warned", False):
                    self._quit_warned = True
                    return True
            return False
        self._quit_warned = False

        # 以 / 开头的一律当命令；否则先按命令名匹配，匹配不到就交给对话
        forced = line.startswith("/")
        if forced:
            line = line[1:].strip()
        if not line:
            self._show_help()
            return True
        try:
            argv = shlex.split(line)
        except ValueError:
            if not forced and self.session.running:
                self.session.steer(line)
                return True
            if forced:
                self._emit("命令中的引号未闭合；补齐引号后重试，或输入 /help 查看示例。", prefix="错误")
                return True
            self._emit(f"命令解析失败，按对话处理: {line[:60]}")
            self._start_chat_turn(line)
            return True
        command, args = argv[0], argv[1:]

        if self.session.running:
            return self._handle_running(command, args, line, forced=forced)

        if command in ("quit", "exit", "q"):
            self._emit("\n再见。")
            self.running = False
            self.exit_requested = True
            return True

        handler = {
            "help": lambda: self._show_help(args),
            "?": lambda: self._show_help(args),
            "play": lambda: self._cmd_play(args),
            "run": lambda: self._cmd_run(args),
            "status": self._show_status,
            "approve": lambda: self._cmd_approve(args),
            "hint": lambda: self.session.steer(" ".join(args)),
            "pause": lambda: self._emit("当前没有运行中的任务"),
            "resume": lambda: self._cmd_resume(args),
            "unpause": lambda: self._emit("当前没有运行中的任务"),
            "stop": lambda: self._emit("当前没有运行中的任务"),
            "revoke": lambda: self._cmd_revoke(args, True),
            "grant": lambda: self._cmd_revoke(args, False),
            "y": lambda: self._emit("当前没有待审批的动作"),
            "n": lambda: self._emit("当前没有待审批的动作"),
            "tools": lambda: self._delegate(["tools", *args]),
            "tool": lambda: self._cmd_tool(args),
            "guard": lambda: self._delegate(["guard", *args]),
            "ranges": lambda: self._delegate(["ranges", *args]),
            "report": lambda: self._delegate(["report", *args]),
            "review": lambda: self._delegate(["review", *args]),
            "audit": lambda: self._delegate(["audit", *args]),
            "lab": lambda: self._delegate(["lab", *args]),
            "model": lambda: self._cmd_model(args),
            "history": lambda: self._cmd_history(args),
            "new": self._cmd_new,
            "clear": self._cmd_new,
            "usage": self._cmd_usage,
            "context": self._cmd_context,
            "compact": self._cmd_compact,
            "save": self._cmd_save,
            "goal": lambda: self._cmd_goal(args),
            "schedule": lambda: self._cmd_schedule(args),
            "scheduled": lambda: self._cmd_schedule(args),
            "schedualed": lambda: self._cmd_schedule(args),
            "board": self._cmd_board,
            "hypotheses": self._cmd_hypotheses,
            "graph": self._cmd_graph,
        }.get(command)
        if handler is None:
            if forced:
                matches = difflib.get_close_matches(command, sorted(COMMAND_NAMES), n=1, cutoff=0.6)
                suggestion = f"；是否想输入 /{matches[0]}" if matches else ""
                self._emit(f"未知命令 {command!r}{suggestion}。输入 /help 查看命令。")
                return True
            self._start_chat_turn(line)
            return True
        try:
            handler()
        except (KeyboardInterrupt, EOFError):
            self._emit("已取消当前命令")
        except Exception as exc:  # noqa: BLE001 - 控制台不能因为单条命令崩掉
            self._emit(f"命令执行失败: {type(exc).__name__}: {exc}")
        return True

    def _handle_running(self, command: str, args: list[str], line: str, *, forced: bool = False) -> bool:
        if command == "goal":
            self._cmd_goal(args)
            return True
        if command in ("schedule", "scheduled", "schedualed"):
            self._cmd_schedule(args)
            return True
        if command in ("y", "yes", "approve") and (command != "approve" or not args or forced):
            if not self.session.answer(True):
                self._emit("当前没有待审批的动作")
            return True
        if command in ("n", "no", "deny"):
            if not self.session.answer(False):
                self._emit("当前没有待审批的动作")
            return True
        if command == "hint" or line.startswith("!"):
            text = " ".join(args) if command == "hint" else line[1:].strip()
            self.session.steer(text)
            return True
        if command == "pause":
            self.session.toggle_pause()
            return True
        if command == "unpause":
            if self.session.pause_event.is_set():
                self.session.toggle_pause()
            else:
                self._emit("任务未处于暂停状态")
            return True
        if command == "resume":
            if args == ["list"]:
                self._cmd_resume(args)
                return True
            self._emit("任务运行中不能切换对话；/unpause 继续暂停的任务，/stop 停止后再 /resume")
            return True
        if command == "stop":
            self.session.request_stop()
            if self.chat is not None and self.session.state is self.chat.state:
                self.chat.ctx.extra["background_jobs"].cancel_running()
            return True
        if command == "status":
            self._show_status()
            return True
        if command == "board":
            self._cmd_board()
            return True
        if command == "context":
            self._cmd_context()
            return True
        if command == "compact":
            self._cmd_compact()
            return True
        if command == "quit":
            self._emit("任务运行中：先 stop 再退出。")
            return True
        if command in ("help", "?"):
            self._show_help(args)
            return True
        if not forced and command not in COMMAND_NAMES:
            # 运行中说人话 = 人工介入提示（最自然的交互）；已知命令名仍按命令处理
            self.session.steer(line)
            return True
        self._emit("任务运行中，可用命令：y / n · hint <文本> · status · pause · unpause · stop（也可直接说人话＝介入提示）")
        return True

    # ------------------------------------------------------------------ 工作台视图
    def _current_state(self) -> Any:
        """当前可展示的状态：运行中的 → 本会话最近一轮 → 磁盘上最近一次运行。"""
        if self.session.state is not None:
            return self.session.state
        if self._last_state is not None:
            return self._last_state
        if self.chat is not None and self.chat.state is not None:
            return self.chat.state
        # 会话里还没有状态：读磁盘上最近一次运行，让 /board /graph 开箱可用
        try:
            from .board import latest_state_file, load_run_state

            config = getattr(self, "config", None)
            workspace = getattr(config, "workspace_dir", None) or "runs"
            path = latest_state_file(workspace)
            if path is not None:
                state = load_run_state(path)
                if state is not None:
                    self.output(f"（已载入最近一次运行 {getattr(state, 'run_id', '') or path.parent.name}；用 /play 开始新一轮）")
                    return state
        except Exception:  # noqa: BLE001 - 载入失败不影响后续命令
            return None
        return None

    def _cmd_board(self) -> None:
        from .board import render_board

        if self.session.running and self.session.state is None:
            self._show_status()
            return
        state = self._current_state()
        if state is None:
            self._display(render_panel("工作台", sections=[("尚无运行记录", "直接输入问题开始对话，或使用 /play 启动任务。")], color=self.color))
            return
        hypotheses = self.chat.ledger.to_dict() if self.chat is not None and state is self.chat.state else None
        self._display(render_board(state, hypotheses=hypotheses, color=self.color))

    def _cmd_hypotheses(self) -> None:
        if self.chat is not None:
            ledger = self.chat.ledger
            items = ledger.to_dict()
            summary = ledger.summary()
        else:
            state = self._current_state()
            items = (getattr(state, "hypotheses", None) or []) if state is not None else []
            summary = {
                "total": len(items),
                "open": len([h for h in items if h.get("status") in ("open", "verifying")]),
                "confirmed": len([h for h in items if h.get("status") == "confirmed"]),
                "refuted": len([h for h in items if h.get("status") == "refuted"]),
                "blocked": len([h for h in items if h.get("status") == "blocked"]),
            }
        if not items:
            self._emit("假设台账为空（模型可以在动作里带 hypothesis 字段提出假设，我会按优先级排序）")
            return
        self._emit(
            f"假设台账：共 {summary['total']} 条（待验证 {summary['open']}、已确认 {summary['confirmed']}、"
            f"已否定 {summary['refuted']}、被阻断 {summary['blocked']}）"
        )
        ordered = sorted(items, key=lambda h: h.get("priority", 0), reverse=True)
        for item in ordered[:15]:
            self.output(
                f"    {item.get('id')} [{item.get('status_cn', item.get('status'))}] "
                f"优先级={item.get('priority')} P={item.get('confidence')} — {str(item.get('statement', ''))[:70]}"
            )

    def _cmd_graph(self) -> None:
        """证据关系图（mermaid）：结论 → 证据 → 检查项/假设 可反向追溯。"""
        state = self._current_state()
        if state is None:
            self._emit("还没有可绘制的证据：先 /play 跑一轮，或说句话让我开工")
            return
        from .evidence_graph import build as build_graph

        registry = self._controller.registry if self._controller is not None else None
        try:
            graph = build_graph(state, registry=registry, hypotheses=getattr(state, "hypotheses", None))
        except Exception as exc:  # noqa: BLE001
            self._emit(f"绘图失败：{type(exc).__name__}: {exc}")
            return
        self._emit(graph.summary())
        self.output(graph.to_mermaid())

    # ------------------------------------------------------------------ 会话/模型
    def _backend(self) -> Any:
        """按当前（会话级）配置取后端；切换模型/后端后会被重建。"""
        if self._backend_obj is None:
            from .llm import make_backend

            self._backend_obj = self._backend_factory(self.config.llm) if self._backend_factory else make_backend(self.config.llm)
        if self._tracked_backend is None or self._tracked_backend.backend is not self._backend_obj:
            self._tracked_backend = TrackedBackend(self._backend_obj, self.view)
        return self._tracked_backend

    def _reload_backend(self) -> None:
        self._backend_obj = self._tracked_backend = None
        if self.chat is not None:
            self.chat.backend = self._backend()
            self.chat.ctx.extra["llm_backend"] = self.chat.backend

    def _cmd_model(self, args: list[str]) -> None:
        llm = self.config.llm
        if not args:
            if self._input is not None or enhanced_input_available():
                self._model_manager().run()
            else:
                self._emit(f"当前 {llm.profile or llm.backend} / {_display_model(llm.model, llm.reasoning_effort)} · /model add 添加供应商 · /model edit 编辑供应商 · /model list 查看预设")
            return
        if args[0] == "context":
            if len(args) != 2:
                self._emit(f"当前上下文 {llm.effective_context_window:,} · /model context auto|500K|1M|Token数")
            else:
                self._model_manager().set_context_window(args[1])
                self._emit(f"上下文窗口已设为 {llm.effective_context_window:,}")
            return
        if args[0] in ("add", "edit", "remove", "test", "keys", "use"):
            manager = self._model_manager()
            if args[0] == "add":
                manager.add(args[1] if len(args) > 1 else "")
            elif args[0] in ("keys", "edit"):
                manager.edit(args[1] if len(args) > 1 else "")
            elif args[0] == "remove":
                manager.remove(args[1] if len(args) > 1 else "")
            elif args[0] == "test":
                if len(args) > 1 and args[1] not in manager.store.profiles:
                    raise ValueError("供应商不存在")
                item = manager.store.profiles[args[1]] if len(args) > 1 else manager.current()
                self._emit(manager.test_connection(item))
            elif len(args) > 1:
                manager.use(args[1])
            else:
                manager.run()
            return
        from .providers import PRESETS, PRESET_BY_ID, ProfileStore
        store = ProfileStore()
        if args[0] in store.profiles:
            self._model_manager().use(args[0])
            return
        if args[0] in PRESET_BY_ID:
            self._model_manager().add(args[0])
            return
        if args[0] in ("list", "ls", "?"):
            self._emit(f"当前后端 {llm.backend} · 模型 {_display_model(llm.model, llm.reasoning_effort)}" + (f" · base_url {llm.base_url}" if llm.backend == "openai" else ""))
            from .models import list_models

            entries = list_models(limit=25)
            if not entries:
                self._emit("（没能列出可选模型：可手动 /model provider/model）")
                return
            self._emit("可选模型（/model <spec> 切换；加 /save 持久化）:")
            for entry in entries:
                marker = " ←当前" if entry.spec.endswith(llm.model) else ""
                self.output(f"    {entry.render()}{marker}")
            self._emit(f"内置 {len(PRESETS)} 个供应商预设 · /model 打开管理页 · /model add 添加连接")
            for preset in PRESETS:
                self.output(f"    {preset.id:<16} {preset.name}")
            return
        spec = args[0]
        from .models import normalize_spec

        provider = (store.profiles[llm.profile].provider if llm.profile in store.profiles else
                    next((p.id for p in PRESETS if p.base_url == llm.base_url), ""))
        llm.model = normalize_spec(spec, llm.backend, provider=provider)
        from .llm.reasoning import reasoning_support
        if llm.reasoning_effort not in reasoning_support(llm).levels:
            llm.reasoning_effort = "default"
        if llm.profile:
            profile = store.profiles[llm.profile]
            profile.model = llm.model
            profile.reasoning_effort = llm.reasoning_effort
            store.save()
        self._reload_backend()
        self._emit(f"已切换模型{'并保存' if llm.profile else '（本会话）'}：{_display_model(llm.model, llm.reasoning_effort)}（后端 {llm.backend}）"
                   + ("" if llm.profile else " · /save 持久化"))

    def _model_manager(self):
        from .model_ui import ModelManager
        from .console_ui import select_option

        def ask(label, default="", *, secret=False):
            if self._input is not None:
                return self._input(label + " › ").strip() or default
            if secret and not enhanced_input_available():
                raise ValueError("密钥录入需要交互终端；也可通过供应商环境变量配置")
            return ModelManager._ask(label, default, secret=secret)

        def choose(title, choices, *, subtitle=""):
            if self._input is None and enhanced_input_available():
                return select_option(title, choices, subtitle=subtitle)
            self._emit(title + (" · " + subtitle if subtitle else ""))
            for i, item in enumerate(choices, 1):
                self.output(f"  {i:02d} {item.description or item.value}")
            value = ask("输入编号或名称，留空返回")
            if value.isdigit() and 1 <= int(value) <= len(choices):
                return choices[int(value) - 1].value
            return next((c.value for c in choices if c.value == value), None)

        return ModelManager(self.config, emit=self._emit, reload=self._reload_backend, ask=ask, choose=choose)

    def _cmd_history(self, args: list[str]) -> None:
        if self.chat is None or not self.chat.history:
            self._emit("还没有对话历史（先说句话，或 /play 跑一轮任务）")
            return
        limit = 8
        if args:
            try:
                limit = max(1, int(args[0]))
            except ValueError:
                pass
        items = self.chat.history[-limit:]
        self._emit(f"最近 {len(items)} 条对话（共 {len(self.chat.history)} 条）：")
        for item in items:
            role = {"user": "你", "assistant": "Trina"}.get(item.get("role", ""), item.get("role", "?"))
            text = str(item.get("content", "")).replace("\n", " ")
            if text.startswith("[工具结果]"):
                role = "工具"
                text = text[len("[工具结果]"):].strip()
            self.output(f"    {role}: {text[:220]}")

    def _cmd_new(self) -> None:
        if self.session.running:
            self._emit("有任务在跑：先 /stop 再开新会话")
            return
        if self.chat is not None:
            self.chat.close()
        self.chat = None
        self._task_title = ""
        self.session.state = self._last_state = None
        self.view.reset()
        self._emit("已开启新对话会话；上一次对话已保存，可用 /resume 切换。")

    def _cmd_tool(self, args: list[str]) -> None:
        """二进制手工调用与当前聊天共用生命周期，其他工具保持 CLI 入口。"""
        tool = self.registry.get(args[0]) if args else None
        if tool is None or tool.spec.category != "binary":
            self._delegate(["tool", *args])
            return
        from .cli import build_parser, _literal
        parsed = build_parser().parse_args(["tool", *args])
        params = json.loads(parsed.params or "{}")
        if not isinstance(params, dict):
            raise ValueError("--params 必须是 JSON 对象")
        for item in parsed.param or []:
            key, _, value = item.partition("=")
            params[key] = _literal(value)
        if parsed.target and "path" in tool.spec.params:
            params.setdefault("path", parsed.target)
        chat = self._ensure_chat()
        chat._initialize_messages()
        chat.title = chat.title or f"手动工具 {tool.spec.name}"
        self._task_title = chat.title
        instruction = {"role": "user", "content": "[手动工具] " + json.dumps({"action": tool.spec.name, "params": params}, ensure_ascii=False)}
        action = {"action": tool.spec.name, "params": params}
        assistant = {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}
        chat.history.extend([instruction, assistant])
        chat._messages.extend([instruction, assistant])
        chat.save()
        try:
            observation = chat._run_action(action, 0)
            message = {"role": "user", "content": "[工具结果] " + observation}
            chat.history.append(message)
            chat._messages.append(message)
        finally:
            chat.save()
        self.session.state = chat.state
        self._display(render_process(observation, label="工具结果", color=self.color))

    def _cmd_resume(self, args: list[str]) -> None:
        from .chat_store import ChatStore
        from .brain.chat import ChatAgent
        from .console_ui import select_option
        if len(args) > 1:
            raise ValueError("用法：/resume [list|last|编号|ID]")
        store = ChatStore(self.config.workspace_dir)
        rows = store.list()
        if not rows:
            self._emit("暂无已保存的历史对话；新对话会自动保存。旧审计日志不含完整回复，无法还原未保存的会话。")
            return
        current = self.chat.run_id if self.chat is not None else ""
        selector = args[0] if args else ""
        if selector in {"", "list"}:
            choices = [ChoiceSpec(row["id"],
                f"{'● ' if row['id'] == current else ''}{row['title']} · {time.strftime('%m-%d %H:%M', time.localtime(row['updated']))} · {row['turns']} 轮 · {row['id']}") for row in rows]
            if not selector and self._input is None and not self._custom_output and enhanced_input_available():
                selected = select_option("历史对话", choices, subtitle="搜索标题或 ID · Enter 切换 · Esc 返回")
                if selected is None:
                    return
                selector = selected
            else:
                self._display(render_panel("历史对话", sections=[("", "\n".join(f"{i:02d}  {item.description}" for i, item in enumerate(choices, 1)))],
                                           footer="/resume <编号或ID> 切换 · /resume last 最近一条", color=self.color))
                return
        if selector == "last":
            selector = next((row["id"] for row in rows if row["id"] != current), rows[0]["id"])
        elif selector.isdigit():
            index = int(selector) - 1
            if not 0 <= index < len(rows):
                raise ValueError("对话编号超出范围")
            selector = rows[index]["id"]
        matches = [row["id"] for row in rows if row["id"] == selector] or [row["id"] for row in rows if row["id"].startswith(selector)]
        if len(matches) != 1:
            raise ValueError("对话 ID 不存在或前缀不唯一；使用 /resume list 查看")
        if matches[0] == current:
            self._emit("已经是当前对话")
            return
        restored = ChatAgent.resume(self.config, matches[0], registry=self.registry, backend=self._backend(),
            approval=self.session.request_approval, steer_queue=self.session.steers, stop_event=self.session.stop_event,
            pause_event=self.session.pause_event, logger=self._agent_log, on_tool=self._on_tool_result)
        try:
            if self.chat is not None:
                self.chat.close()
        except Exception:
            restored.close()
            raise
        self.chat = restored
        self._task_title = restored.title
        restored.save()
        self.session.reset_for_run()
        self.session.state = self._last_state = restored.state
        self.view.reset()
        self.view.usage = restored.usage
        for name, value in restored.usage_gaps.items():
            setattr(self.view, name, value)
        if self._enhanced_input is not None:
            self._enhanced_input.clear_transcript()
        self._event_transcript = None
        self._emit(f"已恢复：{restored.title} · {restored.turns} 轮 · {restored.run_id}")
        restored.on_event = self._on_runtime_event
        history_actions = 0
        for item in restored.history:
            if item.get("role") == "assistant":
                try:
                    value = json.loads(item.get("content", ""))
                    history_actions += int(isinstance(value, dict) and "action" in value)
                except (ValueError, TypeError):
                    pass
        recorded_actions = sum(1 for _ in restored.events.replay(kinds=("tool.requested",), limit=history_actions + 1))
        if restored.events.last_seq and recorded_actions >= history_actions:
            after = 0
            while batch := restored.events.replay(after):
                for event in batch:
                    self._on_runtime_event(event)
                after = batch[-1].seq
            restored._event_subscription = restored.events.subscribe(self._on_runtime_event, after=after)
            return
        restored._event_subscription = restored.events.subscribe(self._on_runtime_event, after=restored.events.last_seq)
        for item in restored.history:
            role, text = item.get("role"), item.get("content", "")
            if role == "assistant":
                try:
                    action = json.loads(text)
                except (ValueError, TypeError):
                    action = None
                if isinstance(action, dict) and "action" in action:
                    self._display(render_process(f"{action['action']} {json.dumps(action.get('params', {}), ensure_ascii=False)}", label="执行记录", color=self.color))
                else:
                    self._display(render_answer(text, color=self.color))
            else:
                self._display(render_message(text, label="工具" if text.startswith("[工具结果]") else "你", color=self.color))

    def _cmd_usage(self) -> None:
        data = self.view.usage_data()
        rate = data["cache_hit_rate"]
        rows: list[tuple[str, Any]] = [("模型调用", data["calls"])] if data["calls"] else []
        if data["tokens_reported"]:
            rows.append(("会话 Token", f"{'≥' if data['missing_tokens'] else ''}{data['total_tokens']}"))
        if data["input_reported"]:
            rows.append(("输入 Token", f"{'≥' if data['missing_input'] else ''}{data['input_tokens']}"))
        if data["output_reported"]:
            rows.append(("输出 Token", f"{'≥' if data['missing_output'] else ''}{data['output_tokens']}"))
        if rate is not None:
            rows.append(("缓存命中率", f"{rate:.1%}" + ("（部分请求）" if data["missing_cache"] else "")))
        if data["cache_read_reported"]:
            rows.append(("缓存读取", f"{'≥' if data['missing_cache_read'] else ''}{data['cache_read_tokens']}"))
        if data["cache_write_reported"]:
            rows.append(("缓存写入", f"{'≥' if data['missing_cache_write'] else ''}{data['cache_write_tokens']}"))
        if data["cost_reported"]:
            rows.append(("费用", f"{'≥' if data['missing_cost'] else ''}${data['cost_usd']:.6f}"))
        self._display(render_panel("会话用量", rows=rows, sections=[] if rows else [("", "暂无可显示的用量数据")], color=self.color))

    def _cmd_context(self) -> None:
        state = self._current_state()
        data = getattr(state, "context_stats", {})
        if not data:
            self._emit("当前会话尚无上下文诊断")
            return
        last = data.get("last_request", {})
        rows = [("输入估算 / 预算", f"{data.get('estimated_input_tokens', 0)} / {data.get('input_budget', 0)}"),
                ("继承检查点", data.get("compactions", 0)), ("前缀变化", last.get("reason", "")),
                ("公共前缀字节", last.get("visible_prefix_bytes", 0)),
                ("新增输入字节" if last.get("reason") == "append" else "变化后缀字节", last.get("suffix_visible_bytes", 0))]
        if last.get("uncached_input_tokens") is not None:
            rows.append(("供应商未命中输入 Token", last["uncached_input_tokens"]))
        policy = last.get("cache_policy") or {}
        if policy:
            rows.extend([("缓存策略", f"{policy.get('mode')} / {policy.get('source')}"),
                         ("前缀标识", policy.get("stable_prefix_id", ""))])
        if data.get("last_archive"):
            rows.append(("完整历史归档", data["last_archive"]))
        self._display(render_panel("上下文与缓存", rows=rows, sections=[("计量说明", "本地字节与 Token 估算不能代替服务端缓存判定。")], color=self.color))

    def _cmd_compact(self) -> None:
        if self.session.running:
            self._emit("任务运行中由运行时在请求边界自动创建检查点")
            return
        if self.chat is None:
            self._emit("当前没有可压缩的对话")
            return
        from .conversation import ContextBudgetError
        before = self.chat.context.epoch
        try:
            self.chat.context.prepare(self.chat._messages, force=True)
            self.chat.save()
        except ContextBudgetError as exc:
            self._emit(str(exc))
            return
        self._emit("已创建继承检查点，完整历史已落盘" if self.chat.context.epoch > before else "当前上下文无需压缩")

    def _cmd_save(self) -> None:
        """把当前供应商与模型写入唯一的 v2 供应商配置。"""
        if self.config.llm.profile:
            from .providers import ProfileStore
            store = ProfileStore()
            profile = store.profiles[self.config.llm.profile]
            profile.model = self.config.llm.model
            profile.reasoning_effort = self.config.llm.reasoning_effort
            profile.context_window = self.config.llm.context_window
            store.active = profile.id
            store.save()
            self._emit(f"已保存到 {store.path}：连接 {profile.id} · 模型 {profile.model}")
            return
        from .providers import ProfileStore
        store = ProfileStore()
        if self.config.llm.backend == "openai":
            from .providers import PRESETS, ProviderProfile
            llm = self.config.llm
            name = next((p.id for p in PRESETS if p.base_url == llm.base_url), "custom")
            base_name, suffix = name, 2
            while name in store.profiles:
                name, suffix = f"{base_name}-{suffix}", suffix + 1
            profile = ProviderProfile(name, base_name, llm.base_url, llm.model, llm.api_key_env, llm.api)
            profile.reasoning_effort = llm.reasoning_effort
            profile.cache_mode, profile.cache_ttl = llm.cache_mode, llm.cache_ttl
            profile.cache_features = list(llm.cache_features)
            profile.context_window = llm.context_window
            profile.validate()
            store.profiles[name], store.active = profile, name
            store.save()
            store.apply(llm)
            self._emit(f"已保存到 {store.path}：连接 {name} · 模型 {profile.model}")
            return
        if store.active:
            store.active = ""
            store.save()

        self._emit(f"已保存到 {store.path}：模型 {self.config.llm.model}")

    def _cmd_goal(self, args: list[str]) -> None:
        if args and args[0] not in ("show",):
            value = "" if args[0] == "clear" else " ".join(args[1:] if args[0] == "set" else args).strip()
            if args[0] != "clear" and not value:
                raise ValueError("goal set 需要目标文本")
            self.task_store.set_goal(value)
            self._goal_text = value
            if self.session.running:
                self.session.update_goal(value)
                if self._task_config is not None:
                    self._task_title = value or f"扫描 {self._plan.target}"
        self._display(render_panel("Goal", sections=[("当前目标", self._goal_text or "未设置")], color=self.color))

    def _cmd_schedule(self, args: list[str]) -> None:
        command = args[0] if args else "list"
        if command == "add":
            import argparse
            class ScheduleParser(argparse.ArgumentParser):
                def error(self, message):
                    raise ValueError(f"/schedule add 参数错误: {message}")
            parser = ScheduleParser(prog="/schedule add", exit_on_error=False)
            parser.add_argument("--at", required=True)
            parser.add_argument("--target", required=True)
            parser.add_argument("--goal")
            parser.add_argument("--max-steps", type=int, default=12)
            parser.add_argument("--approve", choices=APPROVAL_MODES, default=self.session.approval_mode)
            values = parser.parse_args(args[1:])
            task = self.task_store.add(values.target, values.at, goal=values.goal, max_steps=values.max_steps,
                                       approve=values.approve, config=self.config)
            self._display(render_panel("定时任务已创建", rows=[("ID", task.id), ("开始时间", task.run_at),
                          ("目标", task.target), ("Goal", task.goal or "未设置")], color=self.color))
        elif command == "cancel" and len(args) == 2:
            self.task_store.cancel(args[1])
            self._emit(f"已取消定时任务 {args[1]}")
        elif command == "list":
            tasks = self.task_store.list()
            self._display(render_panel("定时任务", sections=[("", "\n".join(
                f"{task.id} · {task.run_at} · {task.status} · {task.target}" + (f" · {task.outcome}" if task.outcome else "")
                for task in tasks) or "暂无定时任务")], color=self.color))
        else:
            raise ValueError("用法: /schedule add --at +10m --target <地址>；/schedule list；/schedule cancel <ID>")

    def _start_scheduler(self) -> None:
        self._scheduler = TaskScheduler(self.task_store, execute=self._execute_scheduled,
                                        ready=lambda: not self.session.running, log=self._agent_log)
        def loop():
            while not self._scheduler.stop_event.is_set():
                try:
                    with self._launch_lock:
                        self._scheduler.tick()
                except Exception as exc:
                    self._emit(f"定时队列读取失败: {exc}", prefix="错误")
                self._scheduler.stop_event.wait(1)
        self._scheduler_thread = threading.Thread(target=loop, name="trina-scheduler", daemon=True)
        self._scheduler_thread.start()

    def _execute_scheduled(self, task):
        from .scope import ScopeGuard
        config = TrinaConfig.load(task.config_path or None)
        decision = ScopeGuard(config.scope).check_target(task.target)
        if decision.denied:
            raise ValueError(decision.reason)
        if not self._start_task(target=task.target, goal=task.goal, max_steps=task.max_steps,
                                approval=task.approve, config=config):
            raise ValueError("定时任务未能启动")
        self.session.thread.join()
        if self._last_state is None:
            raise ValueError("任务未产生运行结果")
        return self._last_state

    # ------------------------------------------------------------------ 子命令
    # ------------------------------------------------------------------ 对话
    def _ensure_chat(self) -> Any:
        if self.chat is None:
            from .brain.chat import ChatAgent

            self.chat = ChatAgent(
                self.config,
                registry=self.registry,
                backend=self._backend(),
                approval=self.session.request_approval,
                steer_queue=self.session.steers,
                stop_event=self.session.stop_event,
                pause_event=self.session.pause_event,
                logger=self._agent_log,
                on_tool=self._on_tool_result,
                on_event=self._on_runtime_event,
            )
        return self.chat

    def _on_chat_stream(self, text: str, final: bool) -> None:
        """流式正文只更新增强终端中的同一张答复卡片。"""
        if self._enhanced_input is not None:
            self._enhanced_input.update_stream(text, final=final)

    def _on_tool_result(self, result: Any) -> None:
        """把工具产出的关键事实回显（发现/凭据/flag/路径），让用户看得见"它在干什么"。"""
        facts = result.facts or {}
        shown: set[tuple[str, str, str]] = set()
        for finding in facts.get("findings", []) or []:
            sev = finding.get("severity", "?")
            target = str(finding.get("url") or finding.get("target") or "")
            title = str(finding.get("title") or finding.get("vuln_class") or "发现")
            parameter = str(finding.get("parameter") or "")
            key = (title, target, parameter)
            if key in shown:
                continue
            shown.add(key)
            detail = f" · {parameter}" if parameter and parameter not in title else ""
            self._emit(f"[发现] [{sev}] {title}{detail} · {target}".rstrip(" ·"), prefix="[Trina]")
        for flag in facts.get("flags", []) or []:
            self._emit(f"[flag] {str(flag.get('value'))[:60]}", prefix="[Trina]")
        for cred in facts.get("credentials", []) or []:
            self._emit(f"[凭据] {cred.get('service')}@{cred.get('target')} 用户 {cred.get('username')}", prefix="[Trina]")

    def _start_chat_turn(self, text: str) -> None:
        if self.session.running:
            self._emit("有任务在跑：先 stop，或直接把这句话作为介入提示（无需命令前缀）")
            return
        try:
            chat = self._ensure_chat()
        except Exception as exc:  # noqa: BLE001 - 工作目录不可写等启动失败必须回显，不能把控制台打崩
            self._emit(f"对话无法启动：{type(exc).__name__}: {exc}", prefix="错误")
            return
        if not isinstance(chat.backend, TrackedBackend):
            chat.backend = TrackedBackend(chat.backend, self.view)
            chat.ctx.extra["llm_backend"] = chat.backend
        self.session.reset_for_run()
        self.view.set_activity("processing", "准备回复")
        self._task_title = chat.title or " ".join(text.split())[:80]
        self._live_step = None
        chat.state.goal = self._goal_text
        self.session.state = chat.state
        self.session.started_at = time.monotonic()

        def _worker() -> None:
            try:
                reply = chat.ask(text)
                self._task_title = chat.title or self._task_title
                if not chat.last_reply_streamed:
                    self._display(render_answer(reply or "(空回复)", color=self.color))
                self._last_state = chat.state
            except Exception as exc:  # noqa: BLE001 - 对话异常不能杀掉控制台
                chat._cancel_stream()
                self._emit(f"对话出错: {type(exc).__name__}: {exc}", prefix="[错误]")
            finally:
                self.view.set_activity("idle", "就绪")

        self.session.thread = threading.Thread(target=_worker, name="trina-chat", daemon=True)
        if self._enhanced_input is None:
            self._emit("思考中…", prefix="过程")
        self.session.thread.start()

    def _show_status(self) -> None:
        from .board import render_board

        if self.session.running:
            step = self._live_step
            phase = PHASES.get(getattr(step, "phase", ""), "准备 / 等待模型")
            state = self.session.state
            if state is not None:
                phase = PHASES.get(state.phase, state.phase)
            control = "待审批" if self.session.pending else ("已暂停" if self.session.pause_event.is_set() else ("停止中" if self.session.stop_event.is_set() else "运行中"))
            rows = [("状态", control), ("阶段", phase),
                    ("目标", (getattr(state, "target", "") if state is not None else getattr(self._plan, "target", "")) or "未指定"),
                    ("耗时", f"{max(0, time.monotonic() - self.session.started_at):.0f}s")]
            if step is not None:
                rows.append(("最近一步", f"第 {step.index} 步 · {step.action} · {step.summary}"))
            self._display(render_panel("当前任务", rows=rows, footer="/board 查看 · /pause 暂停 · /unpause 继续 · /stop 停止；直接输入文字介入。", color=self.color))
            if self.session.pending:
                self._display(render_approval(self.session.pending, self.session.approval_timeout, color=self.color))
        elif self._last_state is not None:
            self._display(render_board(self._last_state, color=self.color))
        else:
            self._display(render_panel("就绪", rows=[("状态", "空闲"), ("模型", _display_model(self.config.llm.model, self.config.llm.reasoning_effort))],
                                       footer="输入问题开始对话 · /play 开始任务 · /help 查看命令", color=self.color))

    def _cmd_approve(self, args: list[str]) -> None:
        mode = (args[0] if args else "").strip().lower()
        if mode not in APPROVAL_MODES:
            self._emit(f"用法: approve {'|'.join(APPROVAL_MODES)}（当前 {self.session.approval_mode}）")
            return
        self.session.approval_mode = mode
        self._emit(f"审批模式已切换为 {mode}")

    def _cmd_revoke(self, args: list[str], revoke: bool) -> None:
        if not args:
            self._emit("用法: revoke <target_id> / grant <target_id>")
            return
        target_id = args[0]
        self._ensure_controller()
        if revoke:
            self._controller.revoke(target_id, reason="控制台人工撤销")
            self._emit(f"已撤销 {target_id} 的授权：后续对该目标的检查会被控制器拒绝（减权，不绕路）")
        else:
            self._controller.grant(target_id)
            self._emit(f"已恢复 {target_id} 的授权")

    def _ensure_controller(self, config: TrinaConfig | None = None) -> Any:
        owner_config = config or self.config
        if self._controller is None or self._controller_config is not owner_config:
            from .policy import AuthorizationController, Budget, PolicyRegistry

            policy = owner_config.policy
            self._controller = AuthorizationController(
                PolicyRegistry.load(owner_config, extra_dirs=list(policy.registry_dirs)),
                mode=policy.mode,
                budget=Budget(policy.max_checks, policy.logic_seconds, policy.stop_on_no_new_evidence),
            )
            self._controller_config = owner_config
        return self._controller

    def _cmd_play(self, args: list[str]) -> None:
        parsed = _parse_play_args(args)
        self._start_task(target=parsed.get("target"), range_name=parsed.get("range"), goal=parsed.get("goal", self._goal_text),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _cmd_run(self, args: list[str]) -> None:
        if not args or args[0].startswith("-"):
            self._emit("用法: run <target> [--goal G] [--max-steps N] [--approve MODE]")
            return
        target = args[0]
        parsed = _parse_play_args(args[1:])
        self._start_task(target=target, range_name=None, goal=parsed.get("goal", self._goal_text),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _delegate(self, argv: list[str]) -> None:
        """把无状态命令交给 CLI 同一实现（保证行为一致）。"""
        from . import cli

        if self._enhanced_input is not None:
            import contextlib
            import io
            stream = io.StringIO()
            try:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    code = cli.main(argv)
            finally:
                self._display(stream.getvalue())
        else:
            code = cli.main(argv)
        if code not in (0, None):
            self._emit(f"（命令退出码 {code}）")

    # ------------------------------------------------------------------ 任务
    def _start_task(self, **kwargs) -> bool:
        with self._launch_lock:
            return self._start_task_locked(**kwargs)

    def _start_task_locked(
        self,
        *,
        target: str | None,
        range_name: str | None,
        goal: str,
        config: TrinaConfig | None = None,
        max_steps: int | None,
        approval: str | None,
    ) -> bool:
        task_config = config or self.config
        if self.session.running:
            self._emit("已有任务在运行：先 stop 或等它结束")
            return False
        if approval:
            if approval.lower() in APPROVAL_MODES:
                self.session.approval_mode = approval.lower()
            else:
                self._emit(f"未知审批模式 {approval}，沿用 {self.session.approval_mode}")
        self.session.reset_for_run()
        self._live_step = None
        # 调度任务会重新载入入队时记录的配置；控制器也必须跟随该配置，
        # 不能复用交互会话里可能已经过期的范围、模式或预算。
        self._ensure_controller(task_config)
        self._controller.checks_used = 0
        self._controller.started_at = time.monotonic()
        try:
            from .play import build_plan
        except ImportError as exc:  # pragma: no cover
            self._emit(f"无法加载 play 模块: {exc}")
            return False
        try:
            plan = build_plan(
                task_config,
                target=target,
                range_name=range_name,
                goal=goal,
                autostart_lab=self.autostart_lab,
                log=lambda msg: self._emit(msg, prefix="[靶场]"),
            )
        except Exception as exc:  # noqa: BLE001 - 靶场准备失败要给出可读原因
            self._emit(f"靶场准备失败: {type(exc).__name__}: {exc}")
            return False
        self._task_config = task_config
        self._last_state = None
        self._plan = plan
        self._task_title = plan.goal or f"扫描 {plan.target}"
        self._task_max_steps = max_steps or task_config.llm.max_steps
        self.session.state = None
        self.session.started_at = time.monotonic()
        teardown_after = bool(plan.started_fixtures)
        self.view.set_activity("processing", "准备任务")

        def _worker() -> None:
            from .play import run_plan
            from .llm import make_backend

            self._emit(
                f"开始任务：{plan.target}"
                + (f" · 目标要求：{goal}" if goal else ""),
                prefix="[任务]",
            )
            try:
                state = run_plan(
                    task_config,
                    plan,
                    backend=(TrackedBackend(make_backend(task_config.llm), self.view) if config is not None else self._backend()),
                    registry=self.registry,
                    approval=self.session.request_approval,
                    steer_queue=self.session.steers,
                    stop_event=self.session.stop_event,
                    pause_event=self.session.pause_event,
                    controller=self._controller,
                    max_steps=max_steps,
                    log=self._agent_log,
                    on_step=self._on_step,
                    on_event=self._on_runtime_event,
                )
                self._last_state = state
                self.session.state = state
                self._report_summary(state)
            except Exception as exc:  # noqa: BLE001 - 任务异常必须回显而不是杀掉控制台
                self._emit(f"任务异常: {type(exc).__name__}: {exc}", prefix="[错误]")
            finally:
                self._task_config = None
                self.view.set_activity("idle", "就绪")
                if teardown_after:
                    plan.teardown()
                    self._emit("已回收本次自动拉起的靶机", prefix="[靶场]")

        self.session.thread = threading.Thread(target=_worker, name="trina-task", daemon=True)
        self.session.thread.start()
        self._emit("任务已在后台运行：直接输入可补充 · status 查看 · stop 停止")

        return True

    def _report_summary(self, state: Any) -> None:
        from .board import render_board
        self._display(render_board(state, color=self.color))
        for finding in state.findings[:8]:
            self._emit(f"[{finding.severity}] {finding.vuln_class} {finding.url or finding.target} ({finding.status})", prefix="发现")

    def _cleanup(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop_event.set()
        if self.session.running:
            self.session.request_stop()
            assert self.session.thread is not None
            self.session.thread.join(timeout=5)
        if self._scheduler_thread is not None and self._scheduler_thread is not threading.current_thread():
            self._scheduler_thread.join(timeout=2)
        if self.chat is not None:
            self.chat.close()
        if self._plan is not None and self._plan.started_fixtures:
            self._plan.teardown()


def _parse_play_args(args: list[str]) -> dict[str, Any]:
    """极简参数解析（play/run 通用）：--target/--range/--goal/--max-steps/--approve。

    `--goal` 会把后续非 `--` 开头的词全部吃进去：控制台里用户习惯不加引号（`play --goal 拿到 flag`）。
    """
    parsed: dict[str, Any] = {}
    index = 0
    value_flags = {"--target": "target", "--range": "range", "--approve": "approve"}
    while index < len(args):
        token = args[index]
        if token == "--goal":
            words: list[str] = []
            index += 1
            while index < len(args) and not args[index].startswith("--"):
                words.append(args[index])
                index += 1
            parsed["goal"] = " ".join(words)
            continue
        if token in value_flags:
            parsed[value_flags[token]] = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if token == "--max-steps":
            try:
                parsed["max_steps"] = int(args[index + 1])
            except (IndexError, ValueError):
                pass
            index += 2
            continue
        index += 1
    return parsed


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`python3 -m trina console`。"""
    import argparse

    parser = argparse.ArgumentParser(prog="trina console", description="Trina 交互控制台（人工可介入）")
    parser.add_argument("--approve", choices=APPROVAL_MODES, default="off",
                        help="可选审批模式：off 全自动 / exploit / verify / all（默认 off）")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--set", action="append", metavar="section.field=value", help="临时覆盖配置，可重复")
    parser.add_argument("--plain", action="store_true", help="关闭终端颜色，适合日志和兼容终端")
    parser.add_argument("--no-lab-autostart", action="store_true", help="play 时不自动起本地 lab 靶机")
    args = parser.parse_args(argv)

    from .cli import _literal  # 复用 CLI 的字面量解析

    overrides = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        overrides[key] = _literal(value)
    config = TrinaConfig.load(args.config, overrides)
    console = Console(config, approval_mode=args.approve, autostart_lab=not args.no_lab_autostart, plain=args.plain)
    return console.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
